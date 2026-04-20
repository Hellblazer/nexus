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
    # spin up a chroma T1 server. Operator dispatch is stateless — each
    # `claude -p` invocation is a one-shot call that takes its input from
    # the prompt and produces structured JSON output. There's no cross-
    # invocation scratch to preserve, so paying the chroma startup cost
    # for every call would be pure waste. The subprocess's T1 client
    # falls back to EphemeralClient when no server is found, which is
    # the correct semantics here: isolated, in-process, no cross talk.
    env = {**os.environ, "NEXUS_SKIP_T1": "1"}
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
        raise OperatorTimeoutError(
            f"claude -p timed out after {timeout}s"
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
