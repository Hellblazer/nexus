# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ceda.7 step 1: the call log records failures and what the agent did next.

``mcp_tool_call`` logs one INFO line per distinct ``_meta`` key set per process,
not one per call, so it cannot say how often an agent retries after an error.
The tool-call-log middleware now also logs ``mcp_tool_error`` (INFO) for every
``tools/call`` whose result is ``isError`` or that raised, and one
``mcp_tool_after_error`` (INFO) for the next call that process makes: its tool,
whether it is the tool that failed, and the seconds since. The retry-after-error
rate is after-error lines with ``same_tool`` over error lines. Neither line
carries an argument or a result: the tool name and the outcome only.

Driven through a real MCPServer and in-process client (``tests/_mcp_inproc.py``).
"""

from __future__ import annotations

import logging

import pytest
import structlog
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from structlog.testing import capture_logs

from nexus.mcp import _tool_call_log
from nexus.mcp._sdk_patches import _patch_tool_failure_is_error
from nexus.mcp._tool_failure import fail
from tests._mcp_inproc import call_tool

_SECRET = "s3cr3t-argument-value"


@pytest.fixture(autouse=True)
def _fresh_state():
    _tool_call_log._reset_state()
    yield
    _tool_call_log._reset_state()


@pytest.fixture(autouse=True)
def _info_level():
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    t = [100.0]
    monkeypatch.setattr(_tool_call_log, "_clock", lambda: t[0])
    return t


def _server() -> MCPServer:
    assert _patch_tool_failure_is_error() in ("applied", "already applied")
    server = MCPServer("test-tool-error-log")

    @server.tool(description="ok")
    def ok(value: str = "") -> str:
        return "fine"

    @server.tool(description="marked failure")
    def marked(value: str = "") -> str:
        return fail(f"Error: no such thing {value}")

    @server.tool(description="raises ToolError")
    def raises(value: str = "") -> str:
        raise ToolError(f"refused {value}")

    @server.tool(description="crashes")
    def crashes(value: str = "") -> str:
        raise RuntimeError(f"boom {value}")

    assert _tool_call_log.install_tool_call_log(server) == "applied"
    return server


def _of(logs: list[dict], event: str) -> list[dict]:
    return [e for e in logs if e["event"] == event]


@pytest.mark.parametrize("tool", ["marked", "raises", "crashes"])
def test_a_failed_call_logs_an_error_line_at_info(tool: str, now: list[float]) -> None:
    server = _server()
    with capture_logs() as logs:
        result = call_tool(server, tool, {"value": _SECRET})
    assert result.is_error is True
    errors = _of(logs, "mcp_tool_error")
    assert [(e["tool"], e["log_level"]) for e in errors] == [(tool, "info")], logs
    assert errors[0]["pid"] > 0
    assert _SECRET not in repr(errors), "the tool name and the outcome only, never an argument or the error text"


def test_a_success_logs_no_error_and_no_after_error(now: list[float]) -> None:
    server = _server()
    with capture_logs() as logs:
        call_tool(server, "ok")
        call_tool(server, "ok")
    assert _of(logs, "mcp_tool_error") == [] and _of(logs, "mcp_tool_after_error") == []


def test_the_next_call_after_an_error_is_logged_once_with_same_tool_and_seconds(now: list[float]) -> None:
    server = _server()
    with capture_logs() as logs:
        call_tool(server, "marked")
        now[0] += 4.0
        call_tool(server, "marked")  # the retry: it fails again
        now[0] += 1.0
        call_tool(server, "ok")  # moved on after the second failure
        call_tool(server, "ok")  # nothing pending: no line
    after = _of(logs, "mcp_tool_after_error")
    assert [(a["tool"], a["failed_tool"], a["same_tool"], a["seconds"]) for a in after] == [
        ("marked", "marked", True, 4.0),
        ("ok", "marked", False, 1.0),
    ], after
    assert len(_of(logs, "mcp_tool_error")) == 2


def test_a_rejected_call_counts_as_an_error(now: list[float]) -> None:
    """An unknown tool never reaches a body; the client still sees a failure."""
    server = _server()
    with capture_logs() as logs:
        result = call_tool(server, "no_such_tool")
        call_tool(server, "ok")
    assert result.is_error is True
    assert [e["tool"] for e in _of(logs, "mcp_tool_error")] == ["no_such_tool"]
    assert [a["same_tool"] for a in _of(logs, "mcp_tool_after_error")] == [False]


def test_a_raising_logger_never_breaks_the_call(now: list[float], monkeypatch: pytest.MonkeyPatch) -> None:
    server = _server()

    def _boom(*_a, **_k):
        raise RuntimeError("logger down")

    monkeypatch.setattr(_tool_call_log, "_note_outcome", _boom)
    assert call_tool(server, "marked").is_error is True
    assert call_tool(server, "ok").is_error is False
