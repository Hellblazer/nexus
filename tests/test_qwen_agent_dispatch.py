# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nexus.operators.qwen_agent_dispatch``.

The MCP stdio transport is not exercised end-to-end — we mock the
``stdio_client`` / ``ClientSession`` surface to return synthetic
``qwen_oneshot`` payloads. This keeps the test independent of the
qwen-coprocessor-stack supervisor binary and runs offline.

Coverage:
  * Supervisor-command resolution: explicit > env > config > default
  * Happy path → parsed dict returned, cost telemetry emitted
  * ``error.code == "timeout"`` → QwenAgentOperatorTimeoutError
  * ``error.code == "validation_failed"`` → QwenAgentOperatorOutputError
  * Other error codes → QwenAgentOperatorError
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nexus.operators import qwen_agent_dispatch as mod
from nexus.operators.qwen_agent_dispatch import (
    QwenAgentOperatorError,
    QwenAgentOperatorOutputError,
    QwenAgentOperatorTimeoutError,
    _resolve_supervisor_cmd,
    qwen_agent_dispatch,
)


class _LogCapture:
    """Drop-in for the module ``_log``: records each ``info(...)`` call as
    ``(event, kwargs)``. Avoids depending on the harness's structlog
    bridge — mirrors the same helper in ``test_qwen_dispatch.py``."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.entries.append((event, kwargs))

    def debug(self, *a: object, **kw: object) -> None:  # pragma: no cover
        pass

    def warning(self, *a: object, **kw: object) -> None:  # pragma: no cover
        pass

    def error(self, *a: object, **kw: object) -> None:  # pragma: no cover
        pass


_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


# ── Env isolation ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for var in (
        "QWEN_AGENT_SUPERVISOR",
        "NEXUS_QWEN_AGENT_CONCURRENCY",
    ):
        monkeypatch.delenv(var, raising=False)
    # Empty tmpdir → no config.json → resolver falls through to the
    # hardcoded developer-machine default.
    monkeypatch.setenv("QWEN_CONFIG_DIR", str(tmp_path))


# ── Supervisor-command resolution ─────────────────────────────────────────


class TestResolveSupervisor:
    def test_explicit_arg_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QWEN_AGENT_SUPERVISOR", "node /env/server.js")
        argv = _resolve_supervisor_cmd("node /explicit/server.js --flag")
        assert argv == ["node", "/explicit/server.js", "--flag"]

    def test_env_beats_config_and_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"supervisor_binary": "node /cfg/server.js"})
        )
        monkeypatch.setenv("QWEN_AGENT_SUPERVISOR", "node /env/server.js")
        argv = _resolve_supervisor_cmd(None)
        assert argv == ["node", "/env/server.js"]

    def test_config_file_used_when_no_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"supervisor_binary": "node /cfg/server.js"})
        )
        monkeypatch.delenv("QWEN_AGENT_SUPERVISOR", raising=False)
        argv = _resolve_supervisor_cmd(None)
        assert argv == ["node", "/cfg/server.js"]

    def test_falls_through_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("QWEN_AGENT_SUPERVISOR", raising=False)
        argv = _resolve_supervisor_cmd(None)
        # Default is developer-machine-specific but always begins with
        # ``node`` and points at a ``server.js``.
        assert argv[0] == "node"
        assert argv[-1].endswith("server.js")


# ── Mock MCP client harness ───────────────────────────────────────────────


def _call_result(payload: dict) -> SimpleNamespace:
    """Synthesize a CallToolResult-shaped object whose single content
    part carries ``payload`` as JSON text."""
    text_part = SimpleNamespace(text=json.dumps(payload))
    return SimpleNamespace(content=[text_part], structuredContent=None)


@asynccontextmanager
async def _fake_stdio_client(server_params):
    yield (object(), object())


class _FakeSession:
    """Minimal stand-in for ``mcp.ClientSession``.

    Captures the ``call_tool`` invocation kwargs so tests can assert on
    them, and returns a synthetic ``qwen_oneshot`` payload supplied at
    construction time.
    """

    def __init__(self, payload: dict):
        self._payload = payload
        self.initialize = AsyncMock(return_value=None)
        self.call_tool = AsyncMock(return_value=_call_result(payload))
        self.last_args: dict | None = None

        async def _capture(name, arguments=None):
            self.last_args = {"name": name, "arguments": arguments}
            return _call_result(self._payload)

        self.call_tool.side_effect = _capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_mcp(monkeypatch, session: _FakeSession):
    """Patch the lazy mcp imports inside qwen_agent_dispatch."""
    import mcp
    import mcp.client.stdio

    def _client_factory(*a, **kw):
        return _fake_stdio_client(None)

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", _client_factory)
    monkeypatch.setattr(
        mcp, "ClientSession", lambda *a, **kw: session
    )
    # Identity StdioServerParameters — we don't use it beyond construction.
    monkeypatch.setattr(
        mcp,
        "StdioServerParameters",
        lambda **kw: SimpleNamespace(**kw),
    )


# ── Behavioural tests ─────────────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_returns_parsed_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession({
            "ok": True,
            "parsed": {"answer": "42"},
            "budget": {
                "input_tokens": 100,
                "output_tokens": 50,
                "tool_calls": 3,
            },
        })
        _patch_mcp(monkeypatch, session)

        result = await qwen_agent_dispatch(
            "what is the answer",
            _SCHEMA,
            timeout=10.0,
            extensions=["nx"],
            operator_name="test_op",
            supervisor_binary="node /fake/server.js",
        )
        assert result == {"answer": "42"}

        # Forwarded args: opts.timeout_ms = timeout * 1000, schema + ext.
        assert session.last_args is not None
        args = session.last_args["arguments"]
        assert session.last_args["name"] == "qwen_oneshot"
        assert args["task"] == "what is the answer"
        assert args["opts"]["json_schema"] == _SCHEMA
        assert args["opts"]["extensions"] == {"only": ["nx"]}
        assert args["opts"]["timeout_ms"] == 10000

    @pytest.mark.asyncio
    async def test_cost_telemetry_emitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession({
            "ok": True,
            "parsed": {"answer": "ok"},
            "budget": {
                "input_tokens": 200,
                "output_tokens": 75,
                "tool_calls": 5,
            },
        })
        _patch_mcp(monkeypatch, session)

        cap = _LogCapture()
        with patch("nexus.operators.qwen_agent_dispatch._log", cap):
            await qwen_agent_dispatch(
                "prompt",
                _SCHEMA,
                operator_name="nx_enrich_beads",
                supervisor_binary="node /fake/server.js",
            )

        cost_entries = [e for e in cap.entries if e[0] == "operator_dispatch_cost"]
        assert cost_entries, f"expected cost log entry; got {cap.entries!r}"
        _, kw = cost_entries[-1]
        assert kw["dispatch_engine"] == "qwen_agent"
        assert kw["dispatch_operator"] == "nx_enrich_beads"
        assert kw["dispatch_input_tokens"] == 200
        assert kw["dispatch_output_tokens"] == 75
        assert kw["dispatch_tool_calls"] == 5
        assert kw["dispatch_cost_usd"] == 0.0


class TestErrorMapping:
    @pytest.mark.asyncio
    async def test_timeout_code_maps_to_timeout_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession({
            "ok": False,
            "error": {"code": "timeout", "message": "oneshot timed out"},
        })
        _patch_mcp(monkeypatch, session)
        with pytest.raises(QwenAgentOperatorTimeoutError):
            await qwen_agent_dispatch(
                "p", _SCHEMA, supervisor_binary="node /fake/s.js",
            )

    @pytest.mark.asyncio
    async def test_validation_failed_maps_to_output_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession({
            "ok": False,
            "error": {"code": "validation_failed", "message": "JSON.parse failed"},
        })
        _patch_mcp(monkeypatch, session)
        with pytest.raises(QwenAgentOperatorOutputError):
            await qwen_agent_dispatch(
                "p", _SCHEMA, supervisor_binary="node /fake/s.js",
            )

    @pytest.mark.asyncio
    async def test_other_error_maps_to_generic_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession({
            "ok": False,
            "error": {"code": "session_error", "message": "spawn ENOENT"},
        })
        _patch_mcp(monkeypatch, session)
        with pytest.raises(QwenAgentOperatorError) as exc_info:
            await qwen_agent_dispatch(
                "p", _SCHEMA, supervisor_binary="node /fake/s.js",
            )
        # NOT the timeout/output subclasses.
        assert not isinstance(exc_info.value, QwenAgentOperatorTimeoutError)
        assert not isinstance(exc_info.value, QwenAgentOperatorOutputError)


# Reset the module semaphore between tests so concurrency state doesn't
# leak across the suite (matches the qwen_dispatch test convention).
@pytest.fixture(autouse=True)
def _reset_semaphore() -> None:
    mod._QWEN_AGENT_SEMAPHORE = None
    yield
    mod._QWEN_AGENT_SEMAPHORE = None
