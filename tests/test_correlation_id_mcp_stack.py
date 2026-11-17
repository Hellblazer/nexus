# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-167 (nexus-dngtz.1.11): the real MCP stack binds a fresh correlation id per tool call.

``tests/test_correlation_id.py`` calls the middleware entry directly. This drives a real
``MCPServer`` through ``mcp.client.Client`` (client -> dispatcher -> middleware chain -> tool), with
``install_tool_call_log`` installed, for one sync and one async tool.
"""
from __future__ import annotations

import re
from typing import Any

from mcp.server.mcpserver import MCPServer

from nexus import correlation
from nexus.mcp import _tool_call_log
from tests._mcp_inproc import call_tool


def _server() -> tuple[MCPServer, dict[str, list[str | None]]]:
    seen: dict[str, list[str | None]] = {"sync": [], "async": [], "after": []}
    server = MCPServer("correlation-probe")

    @server.tool(description="reads the bound id, sync")
    def sync_tool() -> str:
        seen["sync"].append(correlation.current_correlation_id())
        return "ok"

    @server.tool(description="reads the bound id, async")
    async def async_tool() -> str:
        seen["async"].append(correlation.current_correlation_id())
        return "ok"

    # Index 0 is the OUTERMOST middleware. It runs in the same task as the logging middleware, so what it
    # reads after call_next returns is what that middleware left bound; a check after anyio.run returns
    # would read a copy of the caller's context and pass even with the reset removed.
    async def _outer(ctx: Any, call_next: Any) -> Any:
        try:
            return await call_next(ctx)
        finally:
            if ctx.method == "tools/call":
                seen["after"].append(correlation.current_correlation_id())

    server.middleware.insert(0, _outer)
    assert _tool_call_log.install_tool_call_log(server) == "applied"
    return server, seen


def test_each_real_tool_call_sees_a_fresh_32_hex_id_and_leaves_nothing_bound() -> None:
    server, seen = _server()
    for name in ("sync_tool", "async_tool"):
        for _ in range(2):
            assert call_tool(server, name).is_error is False

    for name in ("sync", "async"):
        ids = seen[name]
        assert len(ids) == 2, (name, ids)
        assert all(i is not None and re.fullmatch(r"[0-9a-f]{32}", i) for i in ids), (name, ids)
        assert ids[0] != ids[1], (name, ids)
    every = seen["sync"] + seen["async"]
    assert len(set(every)) == 4, every
    assert seen["after"] == [None] * 4, seen["after"]
