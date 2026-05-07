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
    parent_session_id: str | None = None,
) -> dict[str, str]:
    """Build the env dict for a dispatched ``claude -p`` subprocess.

    RDR-105 P1 (nexus-4fek). Two modes, gated on the
    ``NX_T1_NEW_DISCOVERY=1`` feature flag in the parent's env:

    Shared T1 (``share_t1=True`` AND flag-on AND parent T1 live)
        Inherits ``NX_T1_HOST/PORT`` from the parent's
        ``nexus.mcp._t1_state.T1_ADDR``. ``NEXUS_SKIP_T1`` is removed
        so the subprocess connects to the parent's chroma instead of
        falling through to ``EphemeralClient``. ``NX_T1_NEW_DISCOVERY``
        propagates so the subprocess's ``T1Database`` constructor takes
        the new-discovery branch.

    Legacy ephemeral (everything else)
        ``NEXUS_SKIP_T1=1`` — the existing stateless-operator pattern.
        Subprocess uses ``EphemeralClient``; no parent T1 visibility.

    Raises ``RuntimeError`` when ``share_t1=True`` is requested but the
    parent's T1 isn't live (``_t1_state.T1_ADDR is None``). Fail-loud
    is correct: a silent fallback to ephemeral would defeat the
    caller's intent.
    """
    base = dict(os.environ)
    if share_t1 and base.get("NX_T1_NEW_DISCOVERY") == "1":
        from nexus.mcp import _t1_state

        if _t1_state.T1_ADDR is None:
            raise RuntimeError(
                "share_t1=True requires the top-level MCP's T1 to be "
                "live (NX_T1_NEW_DISCOVERY=1 is set but "
                "nexus.mcp._t1_state.T1_ADDR is None — the lifespan "
                "publish path did not run)."
            )
        host, port = _t1_state.T1_ADDR
        base["NX_T1_HOST"] = host
        base["NX_T1_PORT"] = str(port)
        base.pop("NEXUS_SKIP_T1", None)
    else:
        base["NEXUS_SKIP_T1"] = "1"

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
    except Exception:
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
    from datetime import datetime, timezone
    from nexus.config import nexus_config_dir

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
    except Exception as exc:
        _log.warning("operator_timeout_log_failed", error=str(exc))
        return "(log write failed)"


async def claude_dispatch(
    prompt: str,
    json_schema: dict[str, Any],
    timeout: float = 300.0,
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
    # NEXUS_SKIP_T1=1 tells the subprocess's nx SessionStart hook to NOT
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
    from nexus.session import read_claude_session_id
    env = {**os.environ, "NEXUS_SKIP_T1": "1"}
    parent_session_id = read_claude_session_id()
    if parent_session_id:
        env["NX_SESSION_ID"] = parent_session_id
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p",
        "--output-format", "json",
        "--json-schema", schema_json,
        "--no-session-persistence",
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
        from nexus.util.process_group import safe_killpg
        import signal

        if not safe_killpg(proc, signal.SIGKILL):
            try:
                proc.kill()
            except Exception:
                pass
        # Reap the leader so the asyncio transport closes cleanly.
        try:
            await proc.wait()
        except Exception:
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
