# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drive a real ``MCPServer`` end to end, in process (nexus-sgmyl).

mcp 1.x tests reached into ``server._mcp_server.request_handlers[CallToolRequest]``
and called it with a typed request. mcp 2.x has no handler registry: a request
reaches the tool through the dispatcher, the ``Server.middleware`` chain and the
``on_call_tool`` handler. ``mcp.client.Client`` connects to a server instance over
in-memory streams and takes that whole path, so a test that wants "what a client
sees" uses these helpers instead of poking a private structure.

``mode="legacy"`` forces the ``initialize`` handshake. Claude Code probes the
modern era first and falls back to ``initialize`` when ``nx-mcp`` refuses the probe
(``nexus.mcp.channel.serve_handshake_era``), so that is the exchange it ends up in. The server's lifespan runs on entry, so use these on a purpose-built
``MCPServer``; for ``nexus.mcp.core.mcp`` (whose lifespan opens T1) call the
middleware directly, as ``tests/test_mcp_tool_call_log.py`` does.
"""
from __future__ import annotations

from typing import Any

import anyio
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult


def call_tool(
    server: MCPServer,
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> CallToolResult:
    """One ``tools/call`` through the full client -> dispatcher -> middleware -> handler path."""

    async def _run() -> CallToolResult:
        async with Client(server, mode="legacy") as client:
            return await client.call_tool(name, arguments or {}, meta=meta)

    return anyio.run(_run)


def list_tools(server: MCPServer) -> list[Any]:
    """The ``tools/list`` result a client sees, as the SDK's ``Tool`` models."""

    async def _run() -> list[Any]:
        async with Client(server, mode="legacy") as client:
            return list((await client.list_tools()).tools)

    return anyio.run(_run)
