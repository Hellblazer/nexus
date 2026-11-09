### Added

- **A slow MCP tool call can say where its time went.** With `NX_MCP_SLOW_CALL_S=<seconds>` set, both MCP servers sample the stacks of a `tools/call` that runs past that threshold until it ends, and log one `mcp_tool_call_slow` event with the wall time, whether the call was waiting or computing (process CPU against wall time), and the nexus functions the samples landed in most. Unset, nothing is installed (nexus-3ceda.3).
