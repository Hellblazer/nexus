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
import contextvars
import json
import os
from contextlib import contextmanager
from typing import Any, Iterator

import structlog

_log = structlog.get_logger()

#: nexus-l1qpj: fail-open BATCH callers (taxonomy discover/review) roll
#: dispatch failures up into one summary line themselves — inside their
#: scope the per-failure ``operator_dispatch_failed`` event demotes from
#: WARNING to INFO so a bad run does not wall the terminal with one
#: WARNING per failed batch. INFO still reaches any attached file handler
#: (``open_run_log`` unlocks INFO for its file while pinning stderr
#: quiet), so the per-failure record survives; only the stderr noise goes.
_ROLLED_UP: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "dispatch_failures_rolled_up", default=False,
)


@contextmanager
def rolled_up_dispatch_failures() -> Iterator[None]:
    """Demote per-failure dispatch WARNINGs to INFO within this scope.

    ONLY for callers that (a) are fail-open by contract AND (b) emit their
    own end-of-batch rollup summary naming the failure count and where the
    per-failure records went. Everyone else keeps the WARNING default —
    the durable-record posture of nexus-q6830 is the point of the choke
    point.
    """
    token = _ROLLED_UP.set(True)
    try:
        yield
    finally:
        _ROLLED_UP.reset(token)

#: Per-stream cap on what the failure log records, in characters.
#: Sized against the handler's real budget, not picked for feel: the log
#: rotates at 10 MB x 5 backups (:mod:`nexus.logging_setup`), so a 16 KB
#: two-stream worst case is ~625 max-size entries per rotation — room for
#: a pathological failure loop without letting it evict the retained
#: window. Deliberately NOT the exception message's 300-char cap, which is
#: sized for terminal readability: a JSON error payload with a stack
#: summary clears 300 easily, and a durable record that inherits a
#: readability cap loses the diagnostic all over again.
_LOG_STREAM_CAP: int = 8000

#: Appended when a stream is cut at the cap. Without it a field of exactly
#: _LOG_STREAM_CAP chars is indistinguishable from a diagnostic that
#: happened to be exactly that long — the same ambiguity the "no output on
#: stdout or stderr" sentinel exists to prevent, one field over.
_TRUNCATION_MARKER: str = "...[truncated]"

__all__ = [
    "claude_dispatch",
    "rolled_up_dispatch_failures",
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

    RDR-105 P4 (nexus-jnx7). Three modes (RDR-155 P4b: the chroma T1
    server is retired — ``share_t1=True`` now raises, since there is no
    parent-owned chroma address to share; cross-process findings go to
    T2, and service-mode T1 sharing rides the session-lease mechanism):

    Shared T1 (``share_t1=True``)
        RETIRED. Raises ``RuntimeError`` unconditionally.

    Ephemeral (``ephemeral=True``)
        Sets ``NX_T1_ISOLATED=1`` so the receiving process's T1 routing
        takes the private in-process path (InMemoryVectorClient).
        Strips inherited ``NX_T1_HOST`` / ``NX_T1_PORT`` legacy vars.
        Mutually exclusive with ``share_t1``.

    Owned (default, neither flag set)
        Strips ``NX_T1_HOST`` / ``NX_T1_PORT`` / ``NX_T1_ISOLATED``
        so the subprocess resolves its own T1 (service-backed in
        service mode, private in-process otherwise). The subprocess
        gets a sealed-from-parent T1 session of its own.

    nexus-5daww (defense in depth): both ``ephemeral`` and ``owned`` also
    strip ``NX_T1_SESSION`` / ``NX_T1_SESSION_ID`` -- the SERVICE-backed T1
    session-token pair minted by the top-level MCP's
    ``_t1_lifespan`` Branch 0. Pre-fix, ``base = dict(os.environ)``
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
    ``mcp.core._t1_lifespan`` Branch 0 (nexus-5daww) and is the
    layer that actually prevents rotation.
    """
    if share_t1 and ephemeral:
        raise ValueError(
            "share_t1 and ephemeral are mutually exclusive: a subprocess "
            "cannot both inherit the parent's T1 and skip T1 entirely."
        )

    base = dict(os.environ)

    if share_t1:
        raise RuntimeError(
            "share_t1=True is retired (RDR-155 P4b): the parent-owned "
            "chroma T1 server no longer exists. Use T2 (memory_put) for "
            "cross-process findings, or service-mode T1 session leases."
        )
    if ephemeral:
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


def _capped(raw: bytes) -> str:
    """Decode a subprocess stream for the failure log, marking any cut."""
    text = raw.decode(errors="replace").strip()
    if len(text) <= _LOG_STREAM_CAP:
        return text
    return text[:_LOG_STREAM_CAP] + _TRUNCATION_MARKER


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
        # GH #1414: `claude -p --output-format json` reports its errors on
        # STDOUT, so a stderr-only message rendered as the bare, useless
        # "claude -p exited 1:" — twice, for nx_plan_audit, with nothing in
        # mcp.log either. Report whichever stream spoke.
        err_snippet = stderr.decode(errors="replace").strip()[:300]
        out_snippet = stdout.decode(errors="replace").strip()[:300]
        parts = [
            f"{label}: {text}"
            for label, text in (("stderr", err_snippet), ("stdout", out_snippet))
            if text
        ]
        # Silence must READ as silence: a bare trailing colon is
        # indistinguishable from "we dropped the output", which is the
        # ambiguity that cost GH #1414 a hand investigation.
        detail = " | ".join(parts) if parts else "no output on stdout or stderr"
        # The DURABLE half. The exception above is visible for exactly one
        # turn, in whatever renders the tool error; nothing writes it down.
        # GH #1414 searched a May-July mcp.log and found nothing, because
        # FastMCP's handler returns str(e) to the client without logging —
        # and of the 17 call sites, 13 propagate bare on at least one real
        # invocation path, three of them (nx_plan_audit, nx_tidy,
        # nx_enrich_beads) with no covered path at all. This is the one
        # choke point every caller passes through, including call site 18
        # that nobody has written yet, so the record belongs here rather
        # than at N call sites that must each remember to opt in.
        #
        # Deliberately NOT capped at the exception's 300 chars: that cap
        # buys a readable message, and a durable record that inherits it
        # loses the same diagnostic tail all over again (nexus-1at5's
        # actual lesson was durability independent of the exception text,
        # which the first cut of this fix claimed but did not deliver).
        #
        # SCOPE, precisely: this fires for all 17 call sites, but DURABILITY
        # is a property of the calling process's logging mode, not of this
        # choke point. mode="mcp" gets the rotating file handler, so the 15
        # server-side sites get a record on disk. `nx taxonomy discover` and
        # `review --auto` (taxonomy_cmd.py:1537,:1653) run under
        # mode="cli", which logging_setup returns from before any file
        # handler is attached — stderr only. Those two go from 100% silent
        # to one stderr line per failure during the run, which is an
        # improvement and is NOT "something to grep afterward".
        emit = _log.info if _ROLLED_UP.get() else _log.warning
        emit(
            "operator_dispatch_failed",
            returncode=proc.returncode,
            stdout=_capped(stdout),
            stderr=_capped(stderr),
        )
        # nexus-ri56e: (a) origin unambiguity — a populated message now
        # reads like an ordinary application error, but this is the
        # DISPATCH HARNESS failing (the claude -p CLI exited non-zero);
        # whoever hits it must not mistake the relayed error text for a
        # model-level answer. (b) addressability — the timeout branch has
        # always named its artifact in the exception; name ours too, or
        # honestly say there is none (plain CLI mode).
        from nexus.logging_setup import active_log_file  # noqa: PLC0415 — deferred: logging_setup is heavier than this hot-free error path needs at import time

        log_file = active_log_file()
        where = (
            f"durable record: operator_dispatch_failed in {log_file}"
            if log_file is not None
            else "no log file attached (plain CLI mode) — this message is "
                 "the only record"
        )
        raise OperatorError(
            f"claude -p exited {proc.returncode} (dispatch-harness "
            f"failure, not a model answer): {detail} [{where}]"
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
