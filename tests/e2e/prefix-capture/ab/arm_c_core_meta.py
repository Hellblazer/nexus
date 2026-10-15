

# --- nexus-ly9c2 arm C, experiment only (appended in the container) ---------
# Appended to the installed nexus/mcp/core.py, where `mcp` is that module's
# FastMCP instance; not importable on its own.
# Marks a small core of nexus tools always-loaded per tool; the server entry
# is alwaysLoad false, so every other tool stays deferred behind ToolSearch.
_NXAB_CORE = frozenset({
    "search", "query", "memory_get", "memory_put", "memory_search",
    "scratch", "store_get",
})
for _nxab_tool in mcp._tool_manager.list_tools():
    if _nxab_tool.name in _NXAB_CORE:
        _nxab_tool.meta = {**(_nxab_tool.meta or {}), "anthropic/alwaysLoad": True}
