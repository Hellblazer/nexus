### Added

- **Every `nx` invocation and every MCP tool call carries a correlation id in its log lines.** `nexus.correlation` holds one id in one `ContextVar` (32 lowercase hex characters); the CLI group callback and each `tools/call` bind a fresh one and reset it by token when the invocation or call ends, so nothing stays bound afterwards. A structlog processor adds `correlation_id` to each log line while an id is bound and the line does not already carry that key. Nothing sends the id to the engine yet. RDR-167, Phase 1.
