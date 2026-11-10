### Added

- **`mcp.log` records failed tool calls and what the agent did next** (nexus-3ceda.7). Every `tools/call` that answers `isError` or raises adds an INFO `mcp_tool_error` line (tool, pid), and the next call in that process adds one `mcp_tool_after_error` line (its tool, `failed_tool`, `same_tool`, `seconds` since the failure). The retry-after-error rate is the `same_tool=True` lines over the error lines. Neither line carries an argument, a result or the error text, and the volume is bounded by the number of failures.
