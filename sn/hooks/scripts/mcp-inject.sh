#!/bin/bash

# sn SubagentStart hook — inject Serena + Context7 MCP tool guidance
# These tools are available to subagents but subagents don't know
# they should use them or how to call them correctly.
# IMPORTANT: Use full MCP-prefixed tool names (mcp__plugin_sn_serena__*)
# because subagents see the prefixed names, not the short names.
# Timeout: 5s (hooks.json) — static heredocs only, no I/O. Increase if dynamic content added.

cat <<'SERENA'
## Serena MCP — LSP Code Intelligence (injected by sn plugin)

Serena provides LSP-backed code intelligence. **Use Serena for symbol tasks; Grep for text tasks.**

The `jet_brains_*` tool names are Serena's convention — they work with ANY LSP backend (Python/pyright, TypeScript, Go, Rust, etc.), not just IntelliJ. The project is auto-activated from the working directory (`--project-from-cwd`) if `.serena/project.yml` exists in the git root. No `activate_project` call needed. If Serena tools return empty results, the project may not be configured — run `serena project create /path/to/project`.

SERENA

cat <<'ROUTING'

### When to Use Serena vs Standard Tools

| Task | Serena tool |
|------|-------------|
| Symbol definition | `mcp__plugin_sn_serena__jet_brains_find_symbol` |
| All callers/references | `mcp__plugin_sn_serena__jet_brains_find_referencing_symbols` |
| File structure overview | `mcp__plugin_sn_serena__jet_brains_get_symbols_overview` |
| Class/type hierarchy | `mcp__plugin_sn_serena__jet_brains_type_hierarchy` |
| Replace function body | `mcp__plugin_sn_serena__replace_symbol_body` |
| Insert code at symbol | `mcp__plugin_sn_serena__insert_before_symbol` / `mcp__plugin_sn_serena__insert_after_symbol` |
| Rename safely (all refs) | `mcp__plugin_sn_serena__rename_symbol` |
| Find files by mask | `mcp__plugin_sn_serena__find_file` (gitignore-aware, unlike Glob) |
| List directory contents | `mcp__plugin_sn_serena__list_dir` (gitignore-aware) |
| Text/pattern search | `mcp__plugin_sn_serena__search_for_pattern` (regex, scope to file/dir) |

**Use standard tools for:** broad text search (Grep), reading known files (Read), writing new files (Write).

### Critical Parameter Signatures

Subagents frequently get these wrong. Use exactly these signatures:

```
# find_symbol — name_path_pattern is REQUIRED (not name_path)
mcp__plugin_sn_serena__jet_brains_find_symbol(name_path_pattern="ClassName", include_body=false, depth=0)
mcp__plugin_sn_serena__jet_brains_find_symbol(name_path_pattern="ClassName/methodName", include_body=true)

# find_referencing_symbols — relative_path is REQUIRED (must be a FILE, not dir)
mcp__plugin_sn_serena__jet_brains_find_referencing_symbols(
    name_path="ClassName",
    relative_path="path/to/ClassName.py"  # MUST be the file containing the symbol
)
# NO include_body parameter. NO name_path_pattern. Use name_path + relative_path.

# get_symbols_overview — takes relative_path to a FILE
mcp__plugin_sn_serena__jet_brains_get_symbols_overview(relative_path="path/to/File.py")

# search_for_pattern — uses substring_pattern (regex, not literal)
mcp__plugin_sn_serena__search_for_pattern(substring_pattern="searchText", relative_path="optional/dir")

# find_file — gitignore-aware file search
mcp__plugin_sn_serena__find_file(file_mask="*.py", relative_path="src/")
```

### Rules

- `get_symbols_overview` before reading whole files — ~10x context savings.
- `find_referencing_symbols` before any signature change — LSP-accurate, catches aliases.
- `find_symbol(include_body=false)` first, `true` only when you need the body.
- `find_file` over Glob when you want gitignore filtering.

### Serena Memories

Serena has its own persistent memory system (separate from nx T2). Use for project-specific code navigation notes:

```
mcp__plugin_sn_serena__write_memory(memory_name="auth/login_flow", content="...")
mcp__plugin_sn_serena__read_memory(memory_name="auth/login_flow")
mcp__plugin_sn_serena__list_memories(topic="auth")
```

Use "/" in names to organize by topic. Prefer nx T2 memory for project decisions; use Serena memories for code structure notes that help future symbol navigation.
ROUTING

cat <<'CONTEXT7'

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
