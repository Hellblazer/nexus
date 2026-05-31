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
