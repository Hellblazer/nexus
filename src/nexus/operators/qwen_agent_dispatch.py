# SPDX-License-Identifier: AGPL-3.0-or-later
"""Async qwen-agent dispatch — Qwen-backed alternative to ``claude_dispatch``
for *tier-B* operator tools whose prompts invite mid-loop tool-use.

Where :func:`nexus.operators.qwen_dispatch.qwen_dispatch` is a pure HTTP
oneshot against llama-server's OpenAI-compat endpoint (no tools), this
module talks to the **qwen-coprocessor-stack supervisor** over MCP-stdio
and invokes its ``qwen_oneshot`` tool. The supervisor spawns a qwen CLI
session with configured MCP extensions (e.g. the ``nx`` extension that
exposes nexus's own search/query tools), runs the prompt with tool-use
available, parses JSON against the supplied schema, then stops the
session. Return shape mirrors ``claude_dispatch``'s parsed-dict contract.

Supervisor source:
    https://github.com/Hellblazer/qwen-coprocessor-stack
    mcp-bridges/qwen-agent-server/src/server.ts  (qwen_oneshot tool)

Resolution chain for the supervisor binary command-line:

* explicit ``supervisor_binary=`` arg (a shell-style command string)
* ``QWEN_AGENT_SUPERVISOR`` env (e.g. ``"node /path/to/dist/server.js"``)
* ``~/.qwen-coprocessor-stack/config.json`` field ``supervisor_binary``
  (or the ``QWEN_CONFIG_DIR`` override path)
* hardcoded developer-machine default — see ``_DEFAULT_SUPERVISOR_CMD``

The hardcoded default is intentionally developer-machine-specific. In
production deployments the operator MUST set ``QWEN_AGENT_SUPERVISOR``
or the config-file field; the default exists only so the test suite and
local dev box don't need extra wiring.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "qwen_agent_dispatch",
    "QwenAgentOperatorError",
    "QwenAgentOperatorTimeoutError",
    "QwenAgentOperatorOutputError",
]


class QwenAgentOperatorError(RuntimeError):
    """Raised when the supervisor returns an unexpected payload, the MCP
    transport fails, or the ``qwen_oneshot`` tool returns
    ``{ok: False}`` with a non-timeout / non-parse error code."""


class QwenAgentOperatorTimeoutError(asyncio.TimeoutError):
    """Raised when the supervisor reports
    ``{ok: False, error: {code: "timeout"}}`` or when the MCP-side
    asyncio wait exceeds *timeout* with a generous slack."""


class QwenAgentOperatorOutputError(QwenAgentOperatorError):
    """Raised when the supervisor reports
    ``{ok: False, error: {code: "validation_failed"}}`` after retries —
    i.e. the model produced non-JSON / schema-non-conforming output on
    every attempt."""


# Module-level concurrency cap mirrors qwen_dispatch's design: a single
# llama-server serves one session at a time. Multiple supervisor sessions
# queue at the backend with no benefit. Default 1; raise via env when a
# multi-backend pool is configured upstream.
_QWEN_AGENT_CONCURRENCY = max(
    1, int(os.environ.get("NEXUS_QWEN_AGENT_CONCURRENCY", "1"))
)
_QWEN_AGENT_SEMAPHORE: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    """Lazy-init the module semaphore so it binds to the running loop."""
    global _QWEN_AGENT_SEMAPHORE
    if _QWEN_AGENT_SEMAPHORE is None:
        _QWEN_AGENT_SEMAPHORE = asyncio.Semaphore(_QWEN_AGENT_CONCURRENCY)
    return _QWEN_AGENT_SEMAPHORE


# Developer-machine default. Operators MUST override in prod via
# QWEN_AGENT_SUPERVISOR or ``supervisor_binary`` config-file field.
_DEFAULT_SUPERVISOR_CMD = (
    "node /Volumes/Transcend Hell/git/qwen-coprocessor-stack/"
    "mcp-bridges/qwen-agent-server/dist/server.js"
)


def _read_qwen_stack_config() -> dict[str, Any] | None:
    """Read ``~/.qwen-coprocessor-stack/config.json`` if present.

    Same file qwen_dispatch reads. Failures (missing, invalid JSON, OS
    error) return ``None`` so resolution falls through to the default.
    """
    override = os.environ.get("QWEN_CONFIG_DIR")
    cfg_path = (
        Path(override) / "config.json"
        if override
        else Path.home() / ".qwen-coprocessor-stack" / "config.json"
    )
    try:
        return json.loads(cfg_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _resolve_supervisor_cmd(explicit: str | None) -> list[str]:
    """Resolve the supervisor command into argv form (split via shlex)."""
    if explicit:
        return shlex.split(explicit)
    env = os.environ.get("QWEN_AGENT_SUPERVISOR")
    if env:
        return shlex.split(env)
    cfg = _read_qwen_stack_config()
    if cfg:
        sb = cfg.get("supervisor_binary")
        if isinstance(sb, str) and sb:
            return shlex.split(sb)
    return shlex.split(_DEFAULT_SUPERVISOR_CMD)


def _extract_oneshot_result(call_result: Any) -> dict[str, Any]:
    """Pull the ``qwen_oneshot`` JSON return value out of an MCP CallToolResult.

    MCP tool returns surface as ``CallToolResult`` with a ``content``
    list of typed parts. For ``qwen_oneshot`` we expect a single
    text-part whose body is the JSON-encoded ``OneshotResult``. We are
    defensive: also accept ``structuredContent`` when the server emits
    it, and pull the first text-part if there are multiple.
    """
    # structuredContent path (newer MCP servers)
    structured = getattr(call_result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured

    content = getattr(call_result, "content", None) or []
    for part in content:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue

    raise QwenAgentOperatorError(
        f"qwen_oneshot returned no parseable content: {call_result!r}"
    )


async def qwen_agent_dispatch(
    prompt: str,
    json_schema: dict[str, Any],
    *,
    timeout: float = 600.0,
    extensions: list[str] | None = None,
    max_tool_calls: int = 20,
    max_attempts: int = 2,
    operator_name: str | None = None,
    supervisor_binary: str | None = None,
) -> dict[str, Any]:
    """Dispatch a prompt to qwen via the qwen-agent-server supervisor.

    Opens an MCP-stdio client to the supervisor binary, calls the
    ``qwen_oneshot`` tool with the supplied prompt, schema, and
    extension loadout, and returns the parsed JSON dict (same shape
    as ``claude_dispatch``).

    Args:
        prompt: The task prompt forwarded to qwen.
        json_schema: JSON Schema the response must conform to.
        timeout: Hard timeout in seconds. Forwarded to ``qwen_oneshot``
            as ``timeout_ms``; the python-side asyncio wait gets a
            small slack to allow the supervisor to surface its own
            timeout error cleanly. Default 600 s (parity with tier-B
            tools' 600 s claude_dispatch ceiling).
        extensions: Optional list of qwen-agent extension names to
            enable for the spawned session. ``None`` lets the supervisor
            use its ``default_extensions`` config field.
        max_tool_calls: Cap on tool-use turns inside the session.
        max_attempts: Retry budget for JSON parse / schema failures.
        operator_name: Tag attached to the structured cost log line.
        supervisor_binary: Override the supervisor command resolution.

    Returns:
        Parsed JSON dict from the model response.

    Raises:
        QwenAgentOperatorTimeoutError: supervisor reported a timeout
            or the python-side wait exceeded *timeout* plus slack.
        QwenAgentOperatorOutputError: supervisor reported
            ``validation_failed`` after exhausting retries.
        QwenAgentOperatorError: transport / session / other failure.
    """
    # Late-import the mcp client so import of this module does not
    # incur the cost (or the failure mode) of pulling in anyio /
    # mcp transports unless dispatch is actually invoked.
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    argv = _resolve_supervisor_cmd(supervisor_binary)
    if not argv:
        raise QwenAgentOperatorError(
            "qwen_agent_dispatch: empty supervisor command after resolution"
        )

    server_params = StdioServerParameters(command=argv[0], args=argv[1:])

    opts: dict[str, Any] = {
        "json_schema": json_schema,
        "max_tool_calls": max_tool_calls,
        "timeout_ms": int(timeout * 1000),
        "max_attempts": max_attempts,
    }
    if extensions is not None:
        # qwen_oneshot's ``opts.extensions`` is an object with
        # ``enable`` / ``disable`` / ``only`` array fields — not a
        # bare list. Treat the list arg as "only these extensions"
        # (the most restrictive / predictable semantic for tier-B
        # dispatch). If a future caller needs enable/disable
        # subsets, widen the arg type at that point.
        opts["extensions"] = {"only": list(extensions)}

    # Generous slack so the supervisor's own timeout fires first and
    # we get its structured error rather than a python-side cancel.
    wait_timeout = timeout + 30.0

    async with _semaphore():
        try:
            async with asyncio.timeout(wait_timeout):
                async with stdio_client(server_params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        call_result = await session.call_tool(
                            "qwen_oneshot",
                            arguments={"task": prompt, "opts": opts},
                        )
        except TimeoutError as exc:
            raise QwenAgentOperatorTimeoutError(
                f"qwen_agent_dispatch python-side wait exceeded "
                f"{wait_timeout}s (supervisor did not surface its own "
                f"timeout in time)"
            ) from exc
        except (QwenAgentOperatorError, QwenAgentOperatorTimeoutError):
            raise
        except Exception as exc:
            raise QwenAgentOperatorError(
                f"qwen_agent_dispatch MCP transport error: {exc}"
            ) from exc

    payload = _extract_oneshot_result(call_result)

    # Cost telemetry — emit the same ``operator_dispatch_cost`` event
    # PR #776 added for claude / qwen, so downstream rollups don't need
    # a new engine-aware path. The supervisor's ``budget`` block carries
    # tool_calls + (when available on the OpenAI/Anthropic envelope)
    # input/output token counts; missing fields log as nulls.
    budget = payload.get("budget") if isinstance(payload, dict) else None
    in_tok: int | None = None
    out_tok: int | None = None
    tool_calls: int | None = None
    if isinstance(budget, dict):
        for src_key, dst in (
            ("input_tokens", "in_tok"),
            ("output_tokens", "out_tok"),
            ("tool_calls", "tool_calls"),
        ):
            v = budget.get(src_key)
            if isinstance(v, int):
                if dst == "in_tok":
                    in_tok = v
                elif dst == "out_tok":
                    out_tok = v
                else:
                    tool_calls = v
    _log.info(
        "operator_dispatch_cost",
        dispatch_engine="qwen_agent",
        dispatch_operator=operator_name,
        dispatch_input_tokens=in_tok,
        dispatch_output_tokens=out_tok,
        dispatch_tool_calls=tool_calls,
        dispatch_cost_usd=0.0,
        dispatch_would_have_cost_usd=None,
    )

    if payload.get("ok") is True:
        parsed = payload.get("parsed")
        if not isinstance(parsed, dict):
            raise QwenAgentOperatorOutputError(
                f"qwen_oneshot returned ok=True but parsed is not a dict: "
                f"{type(parsed).__name__}"
            )
        return parsed

    err = payload.get("error") if isinstance(payload, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    msg = err.get("message", "") if isinstance(err, dict) else ""

    if code == "timeout":
        raise QwenAgentOperatorTimeoutError(
            f"qwen_oneshot timeout: {msg}"
        )
    if code == "validation_failed":
        raise QwenAgentOperatorOutputError(
            f"qwen_oneshot validation_failed after retries: {msg}"
        )
    raise QwenAgentOperatorError(
        f"qwen_oneshot failed (code={code!r}): {msg}"
    )
