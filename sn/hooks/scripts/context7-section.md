
## Context7 MCP — Library Documentation (sn plugin)

For libraries, frameworks and APIs, use Context7 to fetch current docs; training data may be outdated.

1. `mcp__plugin_sn_context7__resolve-library-id` with the library name and your question
2. Pick the best match (prefer exact names and version-specific IDs)
3. `mcp__plugin_sn_context7__query-docs` with the selected library ID and your question
4. Answer from the fetched docs, with code examples

Use for: API syntax, configuration, setup, version migration, library-specific debugging, CLI usage, framework patterns, any code that depends on a specific library version.
Skip for: refactoring, general programming concepts, business logic, code review, scripts with no library dependency.
