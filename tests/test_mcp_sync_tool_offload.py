# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sync-tool offload keeps the MCP event loop free (nexus-dgvsz).

A green suite proves nothing about this defect, so the gate is a CONCURRENCY
measurement, not an assertion about code shape: register a slow sync tool and
a cheap one on a real FastMCP instance, call both, and require the cheap one
to finish while the slow one is still running. Unpatched, it cannot — FastMCP
calls a sync body directly from its async dispatch, so the body owns the loop
for its full duration. That is what starved 10s-timeout hook calls behind a
19s store_get and killed the stdio transport on the late reply.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from nexus.mcp import _sdk_patches

#: Only a deadlock guard, never a threshold: the assertions below turn on
#: whether the cheap call RAN, not on how long anything took. A blocked loop
#: fails by reaching this timeout; a free one never comes near it. Kept well
#: clear of the xdist contention this repo's suite runs under.
RELEASE_TIMEOUT = 10.0


def _build_server(patched: bool):
    """A FastMCP instance whose slow tool is unblocked BY the cheap tool.

    The two are coupled on purpose. The slow body waits on an Event that only
    the cheap body sets, so "did the loop stay free?" becomes a yes/no fact —
    either the cheap call got to run and released the slow one, or it did not
    and the slow one timed out. No wall-clock comparison, nothing to tune,
    and no way for a loaded CI box to turn a pass into a failure.
    """
    from mcp.server.fastmcp import FastMCP

    if patched:
        status = _sdk_patches._patch_sync_tool_offload()
        assert status in {"applied", "already applied"}, status
    else:
        # Importing nexus.mcp already applied the patch, so the baseline has
        # to be restored deliberately — otherwise this leg measures the
        # patched path and the non-vacuity claim is empty.
        assert _sdk_patches.restore_sync_tool_offload(), (
            "no pristine from_function was captured; the offload patch never "
            "applied, so this test cannot establish a baseline"
        )

    released = threading.Event()
    mcp = FastMCP("offload-test")

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
    executing — the loop stayed free. ``"timed-out"`` means it never got the
    chance, which is the blocked-loop signature.
    """
    result: dict[str, str] = {}

    async def call(name: str) -> None:
        out = await mcp._tool_manager.call_tool(name, {})
        result[name] = str(out)

    slow = asyncio.create_task(call("slow_sync"))
    # Yield until the slow call is actually in flight, so "the cheap call was
    # second" is a fact rather than a hope.
    await asyncio.sleep(0)
    fast = asyncio.create_task(call("fast_sync"))
    await asyncio.gather(slow, fast)
    return result["slow_sync"]


@pytest.fixture
def _restore_from_function():
    """Undo the class-level patch so test order cannot leak it."""
    from mcp.server.fastmcp.tools.base import Tool

    saved = Tool.__dict__.get("from_function")
    yield
    if saved is not None:
        Tool.from_function = saved  # type: ignore[assignment]


def test_a_sync_tool_body_blocks_the_loop_without_the_patch(
    _restore_from_function,
) -> None:
    """Non-vacuity: the condition the patch fixes is real and reproduced here.

    Without this, the patched test below would pass against an SDK that never
    had the problem, and the gate would be measuring nothing.
    """
    mcp = _build_server(patched=False)
    assert asyncio.run(_race(mcp)) == "timed-out", (
        "the cheap call ran while a sync tool body held the loop — if this "
        "fails, the SDK now offloads sync bodies itself and "
        "_patch_sync_tool_offload should be retired"
    )


def test_the_patch_frees_the_loop(_restore_from_function) -> None:
    mcp = _build_server(patched=True)
    assert asyncio.run(_race(mcp)) == "released", (
        "the cheap call never ran while the slow body was executing — the "
        "offload is not in effect"
    )


def test_the_wire_schema_is_unchanged_by_the_offload(
    _restore_from_function,
) -> None:
    """The wrapper must not alter the tool's advertised signature.

    functools.wraps keeps __wrapped__, so inspect.signature — which is what
    FastMCP builds the JSON schema from — still reports the original. If that
    ever stops holding, every tool's schema changes silently.
    """
    from mcp.server.fastmcp import FastMCP

    def _schema_for(patched: bool) -> dict:
        if patched:
            _sdk_patches._patch_sync_tool_offload()
        else:
            assert _sdk_patches.restore_sync_tool_offload()
        mcp = FastMCP("schema-test")

        @mcp.tool()
        def probe(alpha: str, beta: int = 3) -> str:
            """A probe."""
            return alpha * beta

        (tool,) = [t for t in mcp._tool_manager.list_tools() if t.name == "probe"]
        return tool.parameters

    from mcp.server.fastmcp.tools.base import Tool

    saved = Tool.__dict__.get("from_function")
    try:
        unpatched = _schema_for(patched=False)
    finally:
        if saved is not None:
            Tool.from_function = saved  # type: ignore[assignment]

    patched = _schema_for(patched=True)
    assert patched == unpatched


def test_an_async_tool_is_left_alone(_restore_from_function) -> None:
    """Already-async bodies must not be double-wrapped."""
    from mcp.server.fastmcp import FastMCP

    _sdk_patches._patch_sync_tool_offload()
    mcp = FastMCP("async-test")

    seen: list[str] = []

    @mcp.tool()
    async def already_async() -> str:
        seen.append("ran")
        return "ok"

    (tool,) = [t for t in mcp._tool_manager.list_tools() if t.name == "already_async"]
    assert tool.fn is already_async
    asyncio.run(mcp._tool_manager.call_tool("already_async", {}))
    assert seen == ["ran"]


def test_the_patch_is_idempotent(_restore_from_function) -> None:
    first = _sdk_patches._patch_sync_tool_offload()
    second = _sdk_patches._patch_sync_tool_offload()
    assert first in {"applied", "already applied"}
    assert second == "already applied"


def test_the_mcp_hook_registry_is_locked() -> None:
    """The offload makes concurrent hook fires reachable, so the registry
    the MCP server fires through must serialize them.

    Before the offload, every sync tool body ran on the one event-loop
    thread with no await point inside it, so two store_put calls could not
    interleave and an unlocked registry was safe by construction. That is
    no longer true. The live hazard LockedHookRegistry documents is the
    manifest hook's client-side read-modify-write sweep (nexus-11gh6 /
    nexus-wxjr6); the bulk indexer already wraps its registry this way for
    the same reason.

    Pinned because the unsafe version is an ABSENCE — a plain
    ``HookRegistry()`` here looks entirely ordinary and nothing else in the
    suite would notice.
    """
    from nexus.hook_registry import LockedHookRegistry
    from nexus.mcp import core

    assert isinstance(core._hooks, LockedHookRegistry), (
        "nexus.mcp.core._hooks must be a LockedHookRegistry: the sync-tool "
        "offload runs tool bodies on real threads, so concurrent fires of "
        "the same hook are reachable from the MCP server (nexus-dgvsz)"
    )


def test_apply_sdk_patches_reports_the_offload(_restore_from_function) -> None:
    results = _sdk_patches.apply_sdk_patches()
    assert "sync_tool_offload" in results
    assert not results["sync_tool_offload"].startswith("failed")


def _not_offloaded(tools) -> list[str]:
    """Names of tools whose registered body will not run off the loop.

    A sync ``@mcp.tool()`` body wrapped by ``_offloaded`` is an ``async def``
    closure, and an already-async body is trivially a coroutine function too
    -- so ``iscoroutinefunction`` is true for every correctly-registered tool
    regardless of how its author wrote it. There is no allowlist: the patch
    wraps unconditionally at the ``Tool.from_function`` registration
    boundary, so nothing legitimately skips it.
    """
    return [t.name for t in tools if not asyncio.iscoroutinefunction(t.fn)]


def test_every_registered_core_tool_runs_off_the_loop() -> None:
    """No NEW sync tool can land un-offloaded (nexus-dgvsz).

    Uses the real, module-level ``nexus.mcp.core.mcp`` instance -- the one
    the server actually serves from -- so this is a guard on production
    registration, not a synthetic FastMCP built just for the test. A future
    sync tool that somehow bypasses ``Tool.from_function`` (or a regression
    that undoes the patch) fails here instead of surfacing as a live
    timeout under hook load.
    """
    from nexus.mcp.core import mcp

    tools = list(mcp._tool_manager.list_tools())
    assert tools, "no tools registered on nexus.mcp.core.mcp -- test is checking nothing"
    missed = _not_offloaded(tools)
    assert not missed, (
        f"tool(s) registered on the 'nexus' MCP server whose body will run "
        f"directly on the event loop instead of a worker thread: {missed}"
    )


def test_every_registered_catalog_tool_runs_off_the_loop() -> None:
    """Same guard as above, for the ``nexus-catalog`` server (nexus-dgvsz).

    The offload patch is applied once at import time and shared by both
    servers (the catalog server imports core, which applies it); this pins
    that the sharing actually reaches catalog.py's own registrations.
    """
    from nexus.mcp.catalog import mcp

    tools = list(mcp._tool_manager.list_tools())
    assert tools, "no tools registered on nexus.mcp.catalog.mcp -- test is checking nothing"
    missed = _not_offloaded(tools)
    assert not missed, (
        f"tool(s) registered on the 'nexus-catalog' MCP server whose body "
        f"will run directly on the event loop instead of a worker thread: "
        f"{missed}"
    )
