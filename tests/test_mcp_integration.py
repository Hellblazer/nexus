# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration test: MCP client SDK round-trip to nx-mcp server.

Requires real API keys (VOYAGE_API_KEY, CHROMA_API_KEY) — marked as integration.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_server_round_trip():
    """Start nx-mcp, connect via MCP client SDK, call each tool."""
    # Find the nx-mcp entry point in the same virtualenv
    nx_mcp = Path(sys.executable).parent / "nx-mcp"
    if not nx_mcp.exists():
        pytest.skip("nx-mcp entry point not found; run 'uv sync' first")

    server_params = StdioServerParameters(
        command=str(nx_mcp),
        args=[],
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # List available tools
            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            assert "search" in tool_names
            assert "store_put" in tool_names
            assert "store_list" in tool_names
            assert "memory_put" in tool_names
            assert "memory_get" in tool_names
            assert "memory_search" in tool_names
            assert "scratch" in tool_names
            assert "scratch_manage" in tool_names

            # Call scratch put
            result = await session.call_tool("scratch", {
                "action": "put",
                "content": "integration test entry",
                "tags": "test",
            })
            text = result.content[0].text
            assert "Stored:" in text or "isolated" in text.lower()

            # Call scratch list
            result = await session.call_tool("scratch", {
                "action": "list",
            })
            text = result.content[0].text
            assert "integration test" in text or "No scratch" in text
