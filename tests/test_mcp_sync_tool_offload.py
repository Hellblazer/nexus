# SPDX-License-Identifier: AGPL-3.0-or-later
"""A sync tool body must not hold the MCP event loop (nexus-dgvsz, nexus-sgmyl).

The defect (mcp 1.x): the SDK called a sync tool body directly from its async
dispatch, so one 19 s ``store_get`` starved the 10 s-timeout hook calls on the
same connection and the late reply killed the stdio transport. nexus-dgvsz carried
a ``Tool.from_function`` wrapper to move sync bodies onto a thread.

mcp 2.3.0 does it itself (``FuncMetadata.call_fn`` runs a sync body with
``anyio.to_thread.run_sync``), so the wrapper is gone and THIS file is the pin that
keeps it from coming back as a regression: a green suite proves nothing about this
defect, so the gate is a CONCURRENCY measurement, not an assertion about code
shape. A slow sync tool and a cheap one are registered on a real MCPServer; the
cheap one must run while the slow one is still executing.
"""
from __future__ import annotations

import asyncio
import threading

#: Only a deadlock guard, never a threshold: the assertions below turn on
#: whether the cheap call RAN, not on how long anything took. A blocked loop
#: fails by reaching this timeout; a free one never comes near it. Kept well
#: clear of the xdist contention this repo's suite runs under.
RELEASE_TIMEOUT = 10.0


def _build_server(*, slow_is_async: bool = False):
    """A MCPServer instance whose slow tool is unblocked BY the cheap tool.

    The two are coupled on purpose. The slow body waits on an Event that only
    the cheap body sets, so "did the loop stay free?" becomes a yes/no fact --
    either the cheap call got to run and released the slow one, or it did not
    and the slow one timed out. No wall-clock comparison, nothing to tune, and
    no way for a loaded CI box to turn a pass into a failure.

    ``slow_is_async`` builds the control: an ``async def`` whose body blocks the
    thread. That is exactly what a sync body did on the loop in mcp 1.x, so it
    must time out, which proves the race below can tell a blocked loop from a
    free one.
    """
    from mcp.server.mcpserver import MCPServer

    released = threading.Event()
    mcp = MCPServer("offload-test")

    if slow_is_async:

        @mcp.tool()
        async def slow_sync() -> str:
            return "released" if released.wait(RELEASE_TIMEOUT) else "timed-out"

    else:

        @mcp.tool()
        def slow_sync() -> str:
            return "released" if released.wait(RELEASE_TIMEOUT) else "timed-out"

    @mcp.tool()
    def fast_sync() -> str:
        released.set()
        return "fast"

    return mcp


async def _race(mcp) -> str:
    """Start the slow tool, then the cheap one. Returns the slow tool's word.

    ``"released"`` means the cheap call ran while the slow body was still
    executing -- the loop stayed free. ``"timed-out"`` means it never got the
    chance, which is the blocked-loop signature.
    """
    result: dict[str, str] = {}

    async def call(name: str) -> None:
        out = await mcp.call_tool(name, {})
        result[name] = out.content[0].text

    slow = asyncio.create_task(call("slow_sync"))
    # Yield until the slow call is actually in flight, so "the cheap call was
    # second" is a fact rather than a hope.
    await asyncio.sleep(0)
    fast = asyncio.create_task(call("fast_sync"))
    await asyncio.gather(slow, fast)
    return result["slow_sync"]


def test_a_body_that_blocks_the_loop_is_detected_by_the_race() -> None:
    """Non-vacuity: the condition the SDK's offload fixes is reproduced here.

    Without this, the test below would pass against a harness that cannot tell a
    blocked loop from a free one, and the gate would be measuring nothing.
    """
    mcp = _build_server(slow_is_async=True)
    assert asyncio.run(_race(mcp)) == "timed-out", (
        "the cheap call ran while a thread-blocking async body held the loop; "
        "the race no longer detects a blocked loop"
    )


def test_a_sync_tool_body_does_not_block_the_loop() -> None:
    mcp = _build_server()
    assert asyncio.run(_race(mcp)) == "released", (
        "the cheap call never ran while the slow sync body was executing -- the "
        "SDK no longer offloads sync bodies (the nexus-dgvsz regression); restore "
        "an offload at the Tool.from_function registration boundary"
    )


def test_the_wire_schema_is_what_the_original_signature_gives() -> None:
    """The failure wrapper must not alter the tool's advertised signature.

    ``functools.wraps`` keeps ``__wrapped__``, so ``inspect.signature`` -- which is
    what MCPServer builds the JSON schema from -- still reports the original. If
    that ever stops holding, every tool's schema changes silently.
    """
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.utilities.func_metadata import func_metadata

    mcp = MCPServer("schema-test")

    def probe(alpha: str, beta: int = 3) -> str:
        """A probe."""
        return alpha * beta

    mcp.tool()(probe)
    (tool,) = [t for t in mcp._tool_manager.list_tools() if t.name == "probe"]
    assert getattr(tool.fn, "_nx_failure_wrapped", False), "the registration boundary no longer wraps"
    expected = func_metadata(probe).arg_model.model_json_schema(by_alias=True)
    assert tool.parameters == expected


def test_an_async_tool_is_left_alone() -> None:
    """Already-async bodies must not be double-wrapped or turned into thread work."""
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("async-test")
    seen: list[str] = []

    @mcp.tool()
    async def already_async() -> str:
        seen.append("ran")
        return "ok"

    (tool,) = [t for t in mcp._tool_manager.list_tools() if t.name == "already_async"]
    # The nexus-6b6hq failure wrapper is the only layer allowed around the body.
    fn = tool.fn
    if getattr(fn, "_nx_failure_wrapped", False):
        fn = fn.__wrapped__
    assert fn is already_async
    assert tool.is_async is True
    asyncio.run(mcp.call_tool("already_async", {}))
    assert seen == ["ran"]


def test_the_mcp_hook_registry_is_locked() -> None:
    """Sync tool bodies run on real threads, so the registry the MCP server fires
    through must serialize concurrent fires.

    On the event-loop thread alone, two store_put calls could not interleave and
    an unlocked registry was safe by construction. The live hazard
    LockedHookRegistry documents is the manifest hook's client-side
    read-modify-write sweep (nexus-11gh6 / nexus-wxjr6); the bulk indexer
    already wraps its registry this way for the same reason.

    Pinned because the unsafe version is an ABSENCE -- a plain ``HookRegistry()``
    here looks entirely ordinary and nothing else in the suite would notice.
    """
    from nexus.hook_registry import LockedHookRegistry
    from nexus.mcp import core

    assert isinstance(core._hooks, LockedHookRegistry), (
        "nexus.mcp.core._hooks must be a LockedHookRegistry: sync tool bodies "
        "run on real threads, so concurrent fires of the same hook are "
        "reachable from the MCP server (nexus-dgvsz)"
    )


def _misrouted(tools) -> list[str]:
    """Names of tools whose SDK routing disagrees with the body they carry.

    The SDK picks "worker thread" or "event loop" from ``Tool.is_async``. A sync
    body whose registered form reads as a coroutine function would be awaited on
    the loop; an async body that reads as sync would be handed to a thread and
    return an un-awaited coroutine. Both are the failure wrappers' job to avoid
    (they keep the body's kind), and this is the production-registration guard.
    """
    return [t.name for t in tools if bool(t.is_async) != asyncio.iscoroutinefunction(t.fn)]


def test_every_registered_core_tool_is_routed_by_the_kind_of_its_body() -> None:
    """Uses the real, module-level ``nexus.mcp.core.mcp`` instance -- the one the
    server serves from -- so this is a guard on production registration."""
    from nexus.mcp.core import mcp

    tools = list(mcp._tool_manager.list_tools())
    assert tools, "no tools registered on nexus.mcp.core.mcp -- test is checking nothing"
    assert any(not t.is_async for t in tools), "no sync tool registered: the offload guard is vacuous"
    assert not _misrouted(tools), _misrouted(tools)


def test_every_registered_catalog_tool_is_routed_by_the_kind_of_its_body() -> None:
    from nexus.mcp.catalog import mcp

    tools = list(mcp._tool_manager.list_tools())
    assert tools, "no tools registered on nexus.mcp.catalog.mcp -- test is checking nothing"
    assert any(not t.is_async for t in tools), "no sync tool registered: the offload guard is vacuous"
    assert not _misrouted(tools), _misrouted(tools)
