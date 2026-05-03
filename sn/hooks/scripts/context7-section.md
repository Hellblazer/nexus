
## Context7 MCP — Library Documentation (injected by sn plugin)

When working with libraries, frameworks, or APIs — use Context7 to fetch current docs instead of relying on training data. Training data may be outdated.

### Workflow

1. `mcp__plugin_sn_context7__resolve-library-id` with the library name and your question
2. Pick the best match (prefer exact names and version-specific IDs)
3. `mcp__plugin_sn_context7__query-docs` with the selected library ID and your question
4. Answer using the fetched docs — include code examples

### When to Use

- API syntax, configuration, setup instructions
- Version migration, library-specific debugging
- CLI tool usage, framework patterns
- Any time you're about to write code that depends on a specific library version

### When NOT to Use

- Refactoring, general programming concepts
- Business logic, code review
- Writing scripts from scratch with no library dependency
CONTEXT7
