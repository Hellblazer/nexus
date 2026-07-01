# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1.2 contracts for the shared MCP-client core seam (RDR-139 Layer A).

`core.call_tool` is the fail-soft result-or-None primitive shared with
RDR-126. These pins exercise the contract against a fake `ClientSession`
so no real MCP server is needed:

- success → parsed dict (structuredContent preferred, text-JSON fallback)
- isError result → None + structured warning
- raised exception → None + structured warning (never propagates)
- secrets in arguments are redacted from the log event

Lifecycle (per-call vs daemon-held-open) is deliberately NOT decided here;
`core` only provides the connect primitive and the call contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
import structlog

from nexus.mcp_client import core


@dataclass
class _FakeContent:
    text: str
    type: str = "text"


@dataclass
class _FakeResult:
    structuredContent: dict[str, Any] | None = None
    content: list[Any] | None = None
    isError: bool = False


class _FakeSession:
    """Minimal ClientSession stand-in: records the call, returns a canned result."""

    def __init__(self, result: Any = None, *, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.calls.append((name, dict(arguments or {})))
        if self._raises is not None:
            raise self._raises
        return self._result


@pytest.mark.asyncio
async def test_call_tool_prefers_structured_content() -> None:
    session = _FakeSession(_FakeResult(structuredContent={"count": 2, "results": []}))
    out = await core.call_tool(session, "find_similar_records", {"uuid": "X"})
    assert out == {"count": 2, "results": []}
    assert session.calls == [("find_similar_records", {"uuid": "X"})]


@pytest.mark.asyncio
async def test_call_tool_parses_text_json_fallback() -> None:
    payload = {"ok": True}
    session = _FakeSession(_FakeResult(content=[_FakeContent(json.dumps(payload))]))
    out = await core.call_tool(session, "get_record_links", {"uuid": "X"})
    assert out == payload


@pytest.mark.asyncio
async def test_call_tool_is_error_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    session = _FakeSession(_FakeResult(content=[_FakeContent("boom")], isError=True))
    out = await core.call_tool(session, "set_record_tags", {"uuid": "X"})
    assert out is None


@pytest.mark.asyncio
async def test_call_tool_swallows_exception() -> None:
    session = _FakeSession(raises=RuntimeError("transport down"))
    out = await core.call_tool(session, "anything", {})
    assert out is None  # fail-soft: no DT failure may propagate


@pytest.mark.asyncio
async def test_call_tool_redacts_secret_args() -> None:
    events: list[dict[str, Any]] = []

    def _capture(logger, method_name, event_dict):  # structlog processor
        events.append(dict(event_dict))
        raise structlog.DropEvent

    structlog.configure(processors=[_capture])
    try:
        session = _FakeSession(raises=RuntimeError("x"))
        await core.call_tool(session, "t", {"uuid": "U", "authorization": "Bearer sekret"})
    finally:
        structlog.reset_defaults()

    assert events, "expected a structured log event on failure"
    logged_args = next((e.get("args") for e in events if "args" in e), {})
    assert logged_args.get("uuid") == "U"
    assert logged_args.get("authorization") == core.REDACTED


# ── describe_exception: unwrap ExceptionGroup to the real root cause ─────────
# GH #1351 / nexus-56pmt: dt_call and devonthink_status both stringified a
# raised ExceptionGroup directly (str(exc)), producing the content-free
# "unhandled errors in a TaskGroup (1 sub-exception)" — the actual failure
# (connection refused, transport teardown, protocol mismatch, ...) was never
# logged. describe_exception() unwraps (possibly nested) ExceptionGroups to
# report every leaf exception's type + message.

def test_describe_exception_plain_exception_unchanged() -> None:
    exc = RuntimeError("connection refused")
    assert core.describe_exception(exc) == "RuntimeError: connection refused"


def test_describe_exception_unwraps_single_subexception() -> None:
    leaf = ConnectionRefusedError("[Errno 61] Connection refused")
    eg = ExceptionGroup("unhandled errors in a TaskGroup", [leaf])
    described = core.describe_exception(eg)
    assert "ConnectionRefusedError" in described
    assert "Connection refused" in described
    # Must NOT be the content-free default __str__ of the group alone.
    assert described != str(eg)


def test_describe_exception_unwraps_multiple_subexceptions() -> None:
    eg = ExceptionGroup("multi", [ValueError("bad value"), TypeError("bad type")])
    described = core.describe_exception(eg)
    assert "ValueError: bad value" in described
    assert "TypeError: bad type" in described


def test_describe_exception_unwraps_nested_groups() -> None:
    inner = ExceptionGroup("inner", [OSError("disk full")])
    outer = ExceptionGroup("outer", [inner])
    described = core.describe_exception(outer)
    assert "OSError: disk full" in described
