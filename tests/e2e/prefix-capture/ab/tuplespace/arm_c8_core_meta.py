

# --- arm C (8-tool core, matches develop ALWAYS_LOAD_TOOLS), experiment only ---
# Appended to the container's installed nexus/mcp/core.py, where `mcp` is the
# module's FastMCP instance; not importable on its own.
_NXAB_CORE = frozenset({
    "search", "query", "memory_get", "memory_put", "memory_search",
    "scratch", "store_get", "store_put",
})
for _nxab_tool in mcp._tool_manager.list_tools():
    if _nxab_tool.name in _NXAB_CORE:
        _nxab_tool.meta = {**(_nxab_tool.meta or {}), "anthropic/alwaysLoad": True}
