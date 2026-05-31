# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-139 Layer A' (P3.5/P3.6) — live stdio spawn of the nx-mcp-devonthink server.

The unit tests in ``test_mcp_devonthink_server.py`` assert the gate logic
in-process. These tests spawn the REAL FastMCP server as a subprocess and drive
it over the actual MCP stdio transport, proving the wrapper process spawns,
serves, advertises the gated surface, and exits cleanly — the RDR Phase-3
alwaysLoad-independence requirement ("the wrapper process exits 0 / lists zero
tools with DT absent, independent of the alwaysLoad value").

The availability gate is passed directly to ``build_server`` so both cases are
deterministic with no DEVONthink and no T2 daemon (we bypass ``main()``'s
install ceremony, which is not part of the Layer A' contract). The live
DT-present agent-surface MVV against a real DEVONthink is recorded separately in
T2 ``139-phase3-mvv-live``.
"""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

mcp_stdio = pytest.importorskip("mcp.client.stdio")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def _server_params(available: bool) -> StdioServerParameters:
    # Spawn the real FastMCP server with the gate forced, bypassing main()'s
    # daemon-install ceremony so the test needs no DT and no T2 daemon.
    code = (
        "from nexus.mcp.devonthink import build_server; "
        f"build_server(available={available}).run(transport='stdio')"
    )
    return StdioServerParameters(command=sys.executable, args=["-c", code])


async def _list_and_status(available: bool) -> tuple[list[str], str]:
    async with stdio_client(_server_params(available)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            status = await session.call_tool("devonthink_status", {})
            return names, status.content[0].text


def test_live_server_stub_only_when_dt_absent() -> None:
    """DT absent -> the spawned server advertises only devonthink_status and
    answers it cleanly (Gap-0 alwaysLoad-independence, deterministic in CI)."""
    names, status = asyncio.run(_list_and_status(available=False))
    assert names == ["devonthink_status"]
    # devonthink_status still answers (no crash) and is valid JSON
    payload = json.loads(status)
    assert "available" in payload


def test_live_server_full_surface_when_available() -> None:
    """available=True -> the spawned server advertises the full curated surface
    (17 tools) over real stdio, and the out-of-scope selectors are absent."""
    names, _status = asyncio.run(_list_and_status(available=True))
    assert len(names) == 17
    assert "devonthink_status" in names
    assert "dt_incorporate" in names
    for banned in ("search_records", "lookup_records", "get_record_properties"):
        assert banned not in names
