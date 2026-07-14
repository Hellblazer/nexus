# SPDX-License-Identifier: AGPL-3.0-or-later
"""Async claude -p subprocess dispatch for operator tools.

Single responsibility: spawn `claude -p` as a truly async subprocess,
deliver a prompt via stdin, parse JSON output, surface typed errors.

No worker pool. No auth check. No session management.
claude -p inherits Claude Code auth; if it fails, the subprocess error
surfaces naturally.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import structlog

_log = structlog.get_logger()

__all__ = [
    "claude_dispatch",
    "OperatorError",
    "OperatorOutputError",
    "OperatorTimeoutError",
]


class OperatorError(Exception):
    """Raised when claude -p exits non-zero."""


class OperatorTimeoutError(OperatorError):
    """Raised when claude -p exceeds the timeout."""


class OperatorOutputError(OperatorError):
    """Raised when stdout cannot be parsed as JSON."""


def _build_dispatch_env(
    *,
    share_t1: bool = False,
    ephemeral: bool = False,
    parent_session_id: str | None = None,
) -> dict[str, str]:
    """Build the env dict for a dispatched ``claude -p`` subprocess.

    RDR-105 P4 (nexus-jnx7). Three modes:

    Shared T1 (``share_t1=True``)
        Subprocess inherits ``NX_T1_HOST`` / ``NX_T1_PORT`` from the
        parent's ``nexus.mcp._t1_state.T1_ADDR`` so its ``T1Database``
        connects to the parent's chroma. ``NX_T1_ISOLATED`` is
        stripped. Raises ``RuntimeError`` when
        the parent's T1 isn't live; a silent fallback would defeat
        the caller's intent.

    Ephemeral (``ephemeral=True``)
        Sets ``NX_T1_ISOLATED=1`` so the receiving MCP's lifespan
        skips the chroma spawn and the constructor opens a per-process
        ``EphemeralClient``. Strips inherited ``NX_T1_HOST`` /
        ``NX_T1_PORT`` so the subprocess does not silently connect to
        the parent. Mutually exclusive with ``share_t1``.

    Owned (default, neither flag set)
        Strips ``NX_T1_HOST`` / ``NX_T1_PORT`` / ``NX_T1_ISOLATED``
        so the subprocess MCP spawns its own
        chroma (lifespan Branch 3). The subprocess gets a
        sealed-from-parent T1 session of its own. Internal Bash tools
        and sub-agents within the subprocess see consistent state.

    nexus-5daww (defense in depth): both ``ephemeral`` and ``owned`` also
    strip ``NX_T1_SESSION`` / ``NX_T1_SESSION_ID`` -- the SERVICE-backed T1
    session-token pair minted by the top-level MCP's
    ``_t1_chroma_lifespan`` Branch 0. Pre-fix, ``base = dict(os.environ)``
    carried the parent's already-minted, LIVE token straight through to a
    nested ``nx-mcp`` (spawned by a subsequent tool-granting dispatch, e.g.
    ``nx_plan_audit`` / ``nx_enrich_beads``), whose own Branch 0 would
    resolve the SAME session id via the still-passed-through
    ``NX_SESSION_ID`` and either reuse or re-mint against it. Stripping the
    token pair here means the child never even sees the parent's secret
    directly in its env (reduced exposure surface); it is not sufficient
    on its own to prevent a same-session re-mint since ``NX_SESSION_ID``
    is deliberately still forwarded below for attribution -- the
    session-level fix (a lease-file consult before mint) lives in
    ``mcp.core._t1_chroma_lifespan`` Branch 0 (nexus-5daww) and is the
    layer that actually prevents rotation.
    """
    if share_t1 and ephemeral:
        raise ValueError(
            "share_t1 and ephemeral are mutually exclusive: a subprocess "
            "cannot both inherit the parent's T1 and skip T1 entirely."
        )

    base = dict(os.environ)

    if share_t1:
        from nexus.mcp import _t1_state  # noqa: PLC0415 - deferred to avoid circular import at module load

        if _t1_state.T1_ADDR is None:
            raise RuntimeError(
                "share_t1=True requires the top-level MCP's T1 to be "
                "live (nexus.mcp._t1_state.T1_ADDR is None; the "
                "lifespan publish path did not run)."
            )
        host, port = _t1_state.T1_ADDR
        base["NX_T1_HOST"] = host
        base["NX_T1_PORT"] = str(port)
        base.pop("NX_T1_ISOLATED", None)
    elif ephemeral:
        base["NX_T1_ISOLATED"] = "1"
        base.pop("NX_T1_HOST", None)
        base.pop("NX_T1_PORT", None)
        # nexus-5daww: never forward the parent's live SERVICE-backed T1
        # session-token pair to a nested MCP subprocess.
        base.pop("NX_T1_SESSION", None)
        base.pop("NX_T1_SESSION_ID", None)
    else:
        # Owned: subprocess spawns its own T1. Strip any parent T1
        # signals so the lifespan's Branch 3 fires.
        base.pop("NX_T1_HOST", None)
        base.pop("NX_T1_PORT", None)
        base.pop("NX_T1_ISOLATED", None)
        # nexus-5daww: never forward the parent's live SERVICE-backed T1
        # session-token pair to a nested MCP subprocess.
        base.pop("NX_T1_SESSION", None)
        base.pop("NX_T1_SESSION_ID", None)

    if parent_session_id:
        base["NX_SESSION_ID"] = parent_session_id
    return base


async def _drain_pipe(pipe: asyncio.StreamReader | None) -> bytes:
    """Read whatever bytes are currently buffered in *pipe*.

    Used by the timeout path (nexus-1at5) AFTER the subprocess has
    been killed and reaped. The writer is dead, so ``read()`` returns
    EOF immediately for whatever was buffered without blocking.
    Returns an empty ``bytes`` on any error so the caller can still
    raise the timeout exception cleanly.
    """
    if pipe is None:
        return b""
    try:
        return await pipe.read()
    except Exception:  # noqa: BLE001 - subprocess pipe failure; logged DEBUG with exc_info, returns empty bytes
        # nexus-8g79.8: empty bytes is the right return shape (caller
        # treats it as "no output"), but the silent swallow hides
        # subprocess pipe failures (OOM kill, fd exhaustion, broken
        # pipe). DEBUG-with-exc_info preserves the API contract while
        # making the cause discoverable.
        import structlog  # noqa: PLC0415 - deferred to call time
        structlog.get_logger(__name__).debug(
            "operator_pipe_read_failed", exc_info=True,
        )
        return b""


def _persist_timeout_log(
    timeout: float, stdout: bytes, stderr: bytes,
) -> str:
    """Persist partial subprocess output to a timestamped log file.

    Returns the file path as a string for inclusion in the timeout
    exception message. Failures to write the log are swallowed so
    that the timeout exception (the load-bearing signal) always
    surfaces; the absent log is a soft loss.
    """
    from datetime import datetime, timezone  # noqa: PLC0415 - branch-local; deferred to call time
    from nexus.config import nexus_config_dir  # noqa: PLC0415 - deferred to avoid circular import at module load

    try:
        logs_dir = nexus_config_dir() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = logs_dir / f"operator-timeout-{ts}.log"
        path.write_bytes(
            f"[operator-timeout {timeout}s] {ts}Z\n".encode()
            + b"--- stdout ---\n" + stdout + b"\n"
            + b"--- stderr ---\n" + stderr + b"\n"
        )
        return str(path)
    except Exception as exc:  # noqa: BLE001 - best-effort timeout-log write; logged via log.warning
        _log.warning("operator_timeout_log_failed", error=str(exc))
        return "(log write failed)"


async def claude_dispatch(
    prompt: str,
    json_schema: dict[str, Any],
    timeout: float = 300.0,
    *,
    allowed_tools: list[str] | None = None,
    mcp_servers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch a single operator call to claude -p, fully async.

    Args:
        prompt: The full prompt text, delivered via stdin.
        json_schema: JSON Schema the model output must conform to.
            Passed via --output-format json and --json-schema flag.
        timeout: Seconds before the subprocess is killed. Default 300s
            (5 min) — the analytical workloads these tools run
            (audit, enrich, summarise, extract) can legitimately
            take minutes. Callers that know their input is short
            should override lower; callers running heavy audits
            override up (``nx_plan_audit`` / ``nx_tidy`` use 600s).
            The prior 60s default produced a lot of false timeouts
            on real workloads.
        allowed_tools: Opt-in tool allowlist (nexus-mawqw). When set,
            ``--allowedTools <comma-joined>`` is passed so the child
            ``claude -p`` may call those tools (built-ins like ``Read`` /
            ``Grep`` / ``Glob`` and/or MCP tools like ``mcp__nexus``).
            ``None`` (default) keeps the stateless-operator contract:
            no tool access. DO NOT pass this for stateless operators
            (extract/filter/rank/etc.) — they take all input from the
            prompt and must stay tool-free.
        mcp_servers: Opt-in MCP server map (nexus-mawqw), shape
            ``{server_key: {"command": ..., "args": [...], ...}}``. When
            set, ``--mcp-config '{"mcpServers": {...}}'`` is passed inline.
            Servers provided via the flag are *explicitly* supplied, so
            they clear the post-CC-2.1.162 pending-approval gate that
            denies tool calls to unapproved ``.mcp.json`` servers. Pair
            with ``allowed_tools`` containing ``mcp__<server_key>`` (or a
            specific ``mcp__<server_key>__<tool>``) to actually permit the
            calls. ``None`` (default) injects no MCP servers.

    Returns:
        Parsed JSON dict from stdout.

    Raises:
        OperatorTimeoutError: subprocess exceeded *timeout*.
        OperatorError: subprocess exited non-zero.
        OperatorOutputError: stdout was not valid JSON.
    """
    schema_json = json.dumps(json_schema)
    # Search review I-6: start in a new process group so we can reach
    # any child processes ``claude -p`` spawns (nested claude calls, tool
    # subprocesses). Same killpg idiom as T1 chroma + MinerU cleanup
    # (PR #198). Without this, ``proc.kill()`` on timeout only kills the
    # claude leader and orphans the children.
    #
    # NX_T1_ISOLATED=1 tells the subprocess's nx SessionStart hook to NOT
    # spin up a chroma T1 server, and tells the T1 client to go straight
    # to EphemeralClient. Operator dispatch is stateless — each
    # `claude -p` invocation is a one-shot call that takes its input from
    # the prompt and produces structured JSON output. There's no cross-
    # invocation scratch to preserve, so paying the chroma startup cost
    # for every call would be pure waste.
    #
    # NX_SESSION_ID=<parent-uuid> tells the subprocess's SessionStart hook
    # that it is a NESTED session — its own conversation UUID arrives via
    # the stdin payload, but it should preserve the parent's
    # ``current_session`` flat-file pointer instead of stomping it. Without
    # this, the subprocess's hook would write its own UUID into
    # ``current_session``, the file would point at no on-disk record (skip-
    # T1 wrote none), and the parent's shell-side ``nx scratch`` would fall
    # back to EphemeralClient for the rest of the parent conversation.
    # ``read_claude_session_id`` reads the parent's UUID at dispatch time —
    # the parent's SessionStart populated it before any operator runs.
    from nexus.session import read_claude_session_id  # noqa: PLC0415 - deferred to avoid circular import at module load
    parent_session_id = read_claude_session_id()
    # RDR-105 P2.5 (nexus-4gby): build the subprocess env via the
    # three-mode helper. The operator-dispatch caller is the
    # canonical stateless one-shot, so default to ``ephemeral=True``.
    # The helper emits ``NX_T1_ISOLATED=1`` (the legacy
    # ``NEXUS_SKIP_T1`` alias was removed at 6.5.2, a major past its
    # promised 5.0 removal).
    env = _build_dispatch_env(
        ephemeral=True,
        parent_session_id=parent_session_id,
    )
    # Base argv is the stateless, tool-free default. Opt-in tool access
    # (nexus-mawqw) appends --mcp-config / --allowedTools only when the
    # caller explicitly requests it, preserving the stateless-operator
    # contract for extract/filter/rank/etc.
    argv: list[str] = [
        "claude", "-p",
        "--output-format", "json",
        "--json-schema", schema_json,
        "--no-session-persistence",
    ]
    if mcp_servers:
        argv += ["--mcp-config", json.dumps({"mcpServers": mcp_servers})]
    if allowed_tools:
        argv += ["--allowedTools", ",".join(allowed_tools)]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode()),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # Search review I-6: reach the whole process group so any claude
        # children (nested planners, tool subprocesses) get reaped too.
        # safe_killpg guards on isinstance(proc.pid, int) so mocked-
        # subprocess tests deterministically fall through to proc.kill()
        # — the pgid=1 deadlock on GitHub ubuntu-latest is covered by
        # tests/test_process_group_safety.py.
        from nexus.util.process_group import safe_killpg  # noqa: PLC0415 - deferred to avoid circular import at module load
        import signal  # noqa: PLC0415 - branch-local; deferred to call time

        if not safe_killpg(proc, signal.SIGKILL):
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 - best-effort process reap during cleanup; non-fatal
                pass
        # Reap the leader so the asyncio transport closes cleanly.
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001 - best-effort cancel cleanup before drain-and-raise; non-fatal
            pass
        # nexus-1at5: drain whatever bytes already landed in the pipe
        # buffers BEFORE raising. ``communicate()`` was cancelled mid-
        # await so its return value is gone, but the kernel-side pipe
        # still holds whatever the subprocess wrote. After kill+wait
        # the writer is dead, so the read drains cleanly without
        # blocking. Persist to a per-call log file so the operator can
        # see what claude was producing when the timeout fired -
        # otherwise a 5-minute timeout discards 5 minutes of analytical
        # output and the next debugging session starts from zero.
        partial_stdout = await _drain_pipe(proc.stdout)
        partial_stderr = await _drain_pipe(proc.stderr)
        log_path = _persist_timeout_log(timeout, partial_stdout, partial_stderr)
        raise OperatorTimeoutError(
            f"claude -p timed out after {timeout}s; "
            f"partial output ({len(partial_stdout)}B stdout, "
            f"{len(partial_stderr)}B stderr) logged to {log_path}"
        )

    if proc.returncode != 0:
        err_snippet = stderr.decode(errors="replace")[:300]
        raise OperatorError(
            f"claude -p exited {proc.returncode}: {err_snippet}"
        )

    raw = stdout.decode(errors="replace").strip()
    if not raw:
        raise OperatorOutputError("claude -p produced empty stdout")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OperatorOutputError(
            f"claude -p output is not valid JSON: {exc} — got: {raw[:200]}"
        ) from exc

    # `claude -p --output-format json` returns a wrapper:
    # {"type":"result", "is_error":bool, "result":str, "structured_output":dict, ...}
    # Callers supplied a `json_schema`, so they expect the schema-conforming
    # dict, not the wrapper.  Surface errors explicitly, unwrap otherwise.
    if isinstance(parsed, dict) and "structured_output" in parsed:
        if parsed.get("is_error"):
            raise OperatorError(
                f"claude -p reported error: {parsed.get('result', '')[:300]}"
            )
        structured = parsed.get("structured_output")
        if structured is None:
            raise OperatorOutputError(
                f"claude -p returned null structured_output; "
                f"result={parsed.get('result', '')[:200]!r}"
            )
        return structured
    return parsed
