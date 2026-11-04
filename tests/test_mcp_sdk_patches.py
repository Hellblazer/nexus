# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The local SDK corrections, after the mcp 2.x migration (nexus-sgmyl).

nexus-rjmyk carried three patches on mcp 1.x. Two are retired because mcp 2.3.0
fixed the defects they covered, and this file is where that is pinned so a
future SDK cannot quietly bring them back:

- **The SDK must not answer a request it just cancelled.** Measured 2026-09-20
  across 121 Claude Code client logs for this one server: ten teardowns of the
  stdio transport, each preceded by ``Connection error: Received a response for
  an unknown message ID``; five carried ``{"code":0,"message":"Request
  cancelled"}``. 1.x ``RequestResponder.cancel()`` replied to a request the
  client had discarded. 2.x has no ``RequestResponder``; its dispatcher writes
  nothing for a peer-cancelled request. ``test_a_cancelled_request_gets_no_response``
  drives that on the wire.
- **A sync tool body must not hold the event loop.**
  ``tests/test_mcp_sync_tool_offload.py`` carries the behavioural pin.

What is left is ``tool_failure_is_error`` (our contract, nexus-6b6hq), covered
in ``tests/test_mcp_tool_failure_is_error.py``; this file checks its guards.
"""
from __future__ import annotations

import inspect
import json

import anyio
import pytest

from nexus.mcp import _sdk_patches


# ── the retired cancellation patch: pin the SDK behaviour instead ───────────

def test_a_cancelled_request_gets_no_response() -> None:
    """On the wire: cancel an in-flight ``tools/call``, then ping.

    The next message the client reads must be the ping's reply. A reply carrying
    the cancelled request's id (the 1.x ``Request cancelled`` error) would arrive
    first and fail this. Raw JSON-RPC over in-memory streams, so the dispatcher
    is the real one.
    """
    from mcp.server.mcpserver import MCPServer
    from mcp.shared.memory import create_client_server_memory_streams
    from mcp.shared.message import SessionMessage
    from mcp_types import JSONRPCNotification, JSONRPCRequest, JSONRPCResponse

    server = MCPServer("cancel-probe")
    interrupted = anyio.Event()

    @server.tool()
    async def hang() -> str:
        try:
            await anyio.sleep_forever()
        finally:
            # Positive control: the cancel really reached the handler. Without it a
            # tool that simply never answers would pass this test vacuously.
            interrupted.set()
        return "unreachable"

    async def run() -> list[object]:
        seen: list[object] = []
        async with create_client_server_memory_streams() as (client, srv):
            c_read, c_write = client
            s_read, s_write = srv
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: server._lowlevel_server.run(
                        s_read, s_write, server._lowlevel_server.create_initialization_options()
                    )
                )

                async def send(msg: object) -> None:
                    await c_write.send(SessionMessage(message=msg))

                async def recv() -> object:
                    with anyio.fail_after(10):
                        item = await c_read.receive()
                    assert isinstance(item, SessionMessage)
                    return item.message

                await send(JSONRPCRequest(jsonrpc="2.0", id=1, method="initialize", params={
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                }))
                assert isinstance(await recv(), JSONRPCResponse)
                await send(JSONRPCNotification(jsonrpc="2.0", method="notifications/initialized"))
                await send(JSONRPCRequest(jsonrpc="2.0", id=2, method="tools/call",
                                          params={"name": "hang", "arguments": {}}))
                await anyio.sleep(0.3)  # the handler is parked in sleep_forever
                await send(JSONRPCNotification(jsonrpc="2.0", method="notifications/cancelled",
                                               params={"requestId": 2, "reason": "test"}))
                await anyio.sleep(0.3)
                await send(JSONRPCRequest(jsonrpc="2.0", id=3, method="ping"))
                seen.append(await recv())
                assert interrupted.is_set(), "the cancel never reached the tool; the test proved nothing"
                tg.cancel_scope.cancel()
        return seen

    (first,) = anyio.run(run)
    assert isinstance(first, JSONRPCResponse), first
    assert first.id == 3, (
        f"the first reply was for request {getattr(first, 'id', None)}; a reply for 2 is the "
        f"answered-after-cancel defect: {json.dumps(first.model_dump(mode='json'))[:300]}"
    )


# ── what remains: the failure-as-isError patch ──────────────────────────────

def test_it_is_idempotent() -> None:
    _sdk_patches.apply_sdk_patches()
    # Named individually rather than asserted wholesale, so a patch added
    # without a re-application status has to be a deliberate edit here.
    assert _sdk_patches.apply_sdk_patches() == {"tool_failure_is_error": "already applied"}


def test_the_retired_patches_are_not_applied() -> None:
    assert set(_sdk_patches.apply_sdk_patches()) == {"tool_failure_is_error"}
    for gone in ("_patch_cancellation_response", "_patch_sync_tool_offload", "restore_sync_tool_offload"):
        assert not hasattr(_sdk_patches, gone), gone


# ── it must never kill startup ─────────────────────────────────────────────

def test_a_missing_symbol_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin is ``mcp>=2.3,<3`` but the hook points are private: a future SDK
    can move or delete this one. A server that will not boot is worse than the bug."""
    from mcp.server.mcpserver.tools.base import Tool

    monkeypatch.delattr(Tool, "from_function", raising=False)
    result = _sdk_patches.apply_sdk_patches()
    assert result["tool_failure_is_error"].startswith("unavailable"), result


def test_an_unreadable_source_refuses_rather_than_patching_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the shape cannot be proven, do NOT patch: an unconditional override
    would silently replace whatever upstream does now."""
    from mcp.server.mcpserver.tools.base import Tool

    def _boom(_obj: object) -> str:
        raise OSError("no source")

    original = Tool.__dict__["from_function"]
    underlying = original.__func__ if isinstance(original, classmethod) else original

    def _fresh(cls: type, fn: object, *a: object, **k: object) -> object:  # no marker
        return underlying(cls, fn, *a, **k)

    monkeypatch.setattr(Tool, "from_function", classmethod(_fresh))
    monkeypatch.setattr(_sdk_patches.inspect, "getsource", _boom)
    result = _sdk_patches.apply_sdk_patches()
    assert result["tool_failure_is_error"].startswith("skipped"), result


def test_a_convert_result_that_drops_call_tool_result_pass_through_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The patch returns a ``CallToolResult``; if the SDK stopped passing one
    through, a wrapped failure would be serialized as a success. Refuse."""
    from mcp.server.mcpserver.tools.base import Tool
    from mcp.server.mcpserver.utilities.func_metadata import FuncMetadata

    original = Tool.__dict__["from_function"]
    underlying = original.__func__ if isinstance(original, classmethod) else original

    def _fresh(cls: type, fn: object, *a: object, **k: object) -> object:  # no marker
        return underlying(cls, fn, *a, **k)

    def _convert_result(self: object, result: object) -> object:
        return result

    monkeypatch.setattr(Tool, "from_function", classmethod(_fresh))
    monkeypatch.setattr(FuncMetadata, "convert_result", _convert_result)
    result = _sdk_patches.apply_sdk_patches()
    assert result["tool_failure_is_error"].startswith("unavailable"), result
    assert "pass" in result["tool_failure_is_error"]


# ── the wiring ─────────────────────────────────────────────────────────────

def test_the_server_applies_the_patch_before_it_builds_the_server() -> None:
    """A patch nothing calls is the same as no patch. It must also run BEFORE
    ``@mcp.tool()`` registers anything, because it wraps at registration."""
    source = inspect.getsource(__import__("nexus.mcp.core", fromlist=["core"]))
    assert "_apply_sdk_patches()" in source, "core.py never applies the SDK patches"
    assert source.index("_apply_sdk_patches()") < source.index('mcp = MCPServer("nexus"'), (
        "the patch must be applied before the server is constructed"
    )
