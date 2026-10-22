# SPDX-License-Identifier: AGPL-3.0-or-later
"""The catalog server's search tool tells a caller what its query matches (nexus-r88er, shakeout F12).

Kept out of tests/test_catalog_mcp.py, whose module-level t2_tenant mark would mint a tenant for a
test that only reads a tool description (storage-touch ceiling, tests/storage_touch_ceiling.toml).
"""

def test_search_query_param_description_states_the_scope() -> None:
    from nexus.mcp.catalog import mcp as catalog_mcp
    import asyncio

    tools = asyncio.run(catalog_mcp.list_tools())
    tool = next(t for t in tools if t.name == "search")
    desc = tool.inputSchema["properties"]["query"]["description"]
    assert "title" in desc and "metadata" in desc
    assert "mcp__plugin_conexus_nexus__search" in desc
