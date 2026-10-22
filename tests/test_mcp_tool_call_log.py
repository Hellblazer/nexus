# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-2nxoo (shakeout F18): one ``mcp_tool_call`` debug event per tools/call.

Nobody could tell whether a client (the Claude Desktop remote-devices bridge)
sends per-request ``_meta`` such as a session id, because nx-mcp logged no
tool calls. The hook wraps ``request_handlers[CallToolRequest]`` -- FastMCP
binds that handler during ``__init__``, so the wrap has to happen on the
constructed server's low-level registry -- and logs the tool name plus the
SORTED KEYS of ``_meta``. Never values, never arguments: they can carry tokens.

These tests drive the real handler of a real FastMCP, the same path
``tests/test_stale_host.py`` uses.
"""
from __future__ import annotations

import logging

import anyio
import pytest
import structlog
from mcp import types
from mcp.server.fastmcp import FastMCP
from structlog.testing import capture_logs

from nexus.mcp import _tool_call_log

_SECRET = "s3cr3t-token-value"


@pytest.fixture(autouse=True)
def _debug_level():
    """The suite filters structlog at WARNING; the event is DEBUG. conftest's
    autouse ``_restore_structlog_after_test`` puts the config back."""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))


def _mk_server() -> FastMCP:
    server = FastMCP("test-tool-call-log")

    @server.tool(description="echo")
    def echo(value: str) -> str:
        return value

    return server


def _call(server: FastMCP, tool: str, meta: dict | None = None):
    low = server._mcp_server
    handler = low.request_handlers[types.CallToolRequest]
    params: dict = {"name": tool, "arguments": {"value": _SECRET}}
    if meta is not None:
        params["_meta"] = meta
    req = types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(**params)
    )
    return anyio.run(handler, req)


def _events(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e["event"] == "mcp_tool_call"]


class TestToolCallLog:
    def test_meta_keys_logged_sorted_without_values(self):
        server = _mk_server()
        assert _tool_call_log.install_tool_call_log(server) == "applied"
        with capture_logs() as logs:
            result = _call(server, "echo", {"progressToken": 7, "a": _SECRET})
        events = _events(logs)
        assert len(events) == 1
        assert events[0]["tool"] == "echo"
        assert events[0]["meta_keys"] == ["a", "progressToken"]
        assert events[0]["log_level"] == "info", "a non-routine key must reach mcp.log at the default INFO level"
        # keys only: no value and no argument anywhere in any event
        assert _SECRET not in repr(logs)
        # the call itself is untouched
        assert result.root.isError is False

    def test_no_meta_logs_empty_list(self):
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)
        with capture_logs() as logs:
            _call(server, "echo")
        events = _events(logs)
        assert len(events) == 1
        assert events[0]["meta_keys"] == []

    def test_routine_meta_only_stays_at_debug(self):
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)
        with capture_logs() as logs:
            _call(server, "echo")
            _call(server, "echo", {"progressToken": 3})
        assert [(e["meta_keys"], e["log_level"]) for e in _events(logs)] == [
            ([], "debug"),
            (["progressToken"], "debug"),
        ]

    def test_one_event_per_call(self):
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)
        with capture_logs() as logs:
            _call(server, "echo")
            _call(server, "echo", {"x": 1})
        assert [e["meta_keys"] for e in _events(logs)] == [[], ["x"]]

    def test_raising_logger_does_not_break_the_call(self, monkeypatch):
        server = _mk_server()
        _tool_call_log.install_tool_call_log(server)

        seen: list[object] = []

        def _boom(req):
            seen.append(req)
            raise RuntimeError("logging backend down")

        monkeypatch.setattr(_tool_call_log, "_emit", _boom)
        result = _call(server, "echo", {"a": 1})
        assert len(seen) == 1  # the hook ran, raised, and the call still completed
        assert result.root.isError is False
        assert result.root.content[0].text == _SECRET

    def test_install_degrades_on_unverifiable_shape(self):
        class _NoLow:
            pass

        status = _tool_call_log.install_tool_call_log(_NoLow())
        assert status.startswith("unavailable")

    def test_install_is_idempotent(self):
        server = _mk_server()
        assert _tool_call_log.install_tool_call_log(server) == "applied"
        assert _tool_call_log.install_tool_call_log(server) == "already applied"
        with capture_logs() as logs:
            _call(server, "echo")
        assert len(_events(logs)) == 1


class TestCoreWiring:
    def test_nexus_server_handler_is_wrapped(self):
        from nexus.mcp.core import _SDK_PATCH_RESULTS, mcp

        handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
        assert getattr(handler, "_nx_tool_call_log", False) is True
        assert _SDK_PATCH_RESULTS["tool_call_log"] == "applied"
