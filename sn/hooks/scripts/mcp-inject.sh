#!/bin/bash

# sn SubagentStart hook — inject Serena + Context7 MCP tool guidance
# These tools are available to subagents but subagents don't know
# they should use them or how to call them correctly.
# Timeout: 5s (hooks.json) — static heredocs only, no I/O. Increase if dynamic content added.

cat <<'SERENA'
## Serena MCP — Symbol Navigation (injected by sn plugin)

Serena provides LSP-backed code intelligence. **Use Serena for symbol tasks; Grep for text tasks.**

The project is auto-activated from the working directory (`--project-from-cwd`) if `.serena/project.yml` exists in the git root. No `activate_project` call needed. If Serena tools return empty results, the project may not be configured — run `serena project create /path/to/project`.

SERENA

cat <<'ROUTING'

### When to Use Serena vs Standard Tools

| Task | Serena tool |
|------|-------------|
| Symbol definition | `jet_brains_find_symbol` |
| All callers | `jet_brains_find_referencing_symbols` |
| File structure | `jet_brains_get_symbols_overview` |
| Class hierarchy | `jet_brains_type_hierarchy` |
| Replace function body | `replace_symbol_body` |
| Insert code | `insert_before_symbol` / `insert_after_symbol` |
| Rename safely | `rename_symbol` |
| Text search in code | `search_for_pattern` |

**Use standard tools for:** file search (Glob), text/config search (Grep), reading known files (Read).

### Critical Parameter Signatures

Subagents frequently get these wrong. Use exactly these signatures:

```
# find_symbol — name_path_pattern is REQUIRED (not name_path)
jet_brains_find_symbol(name_path_pattern="ClassName", include_body=false, depth=0)
jet_brains_find_symbol(name_path_pattern="ClassName/methodName", include_body=true)

# find_referencing_symbols — relative_path is REQUIRED (must be a FILE, not dir)
jet_brains_find_referencing_symbols(
    name_path="ClassName",
    relative_path="path/to/ClassName.java"  # MUST be the file containing the symbol
)
# NO include_body parameter. NO name_path_pattern. Use name_path + relative_path.

# get_symbols_overview — takes relative_path to a FILE
jet_brains_get_symbols_overview(relative_path="path/to/File.java")

# search_for_pattern — uses substring_pattern (not pattern)
search_for_pattern(substring_pattern="searchText", relative_path="optional/dir")
```

### Rules

- `get_symbols_overview` before reading whole files — ~10x context savings.
- `find_referencing_symbols` before any signature change — LSP-accurate, catches aliases.
- `find_symbol(include_body=false)` first, `true` only when you need the body.
ROUTING

cat <<'CONTEXT7'

## Context7 MCP — Library Documentation (injected by sn plugin)

When working with libraries, frameworks, or APIs — use Context7 to fetch current docs instead of relying on training data. Training data may be outdated.

### Workflow

1. `resolve-library-id` with the library name and your question
2. Pick the best match (prefer exact names and version-specific IDs)
3. `query-docs` with the selected library ID and your question
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
