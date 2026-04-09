#!/bin/bash

# sn SubagentStart hook — inject Serena + Context7 MCP tool guidance
# Selectively injects based on agent task text to save tokens.
# Default: inject both (safe fallback on parse failure).
# Timeout: 5s (hooks.json) — stdin read + python3 ~50ms, well within budget.

# --- Agent-type detection via stdin JSON ---
STDIN=$(cat)
TASK_TEXT=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    text = ' '.join([
        str(data.get('task', '')),
        str(data.get('prompt', '')),
    ]).lower()
    print(text)
except: print('')
" "$STDIN" 2>/dev/null)

SKIP_SERENA=0
SKIP_CONTEXT7=0

if echo "$TASK_TEXT" | grep -qiE "research|synthesize|audit|survey|deep.anal|investigate|knowledge.tid"; then
    # Pure research agents don't need code nav or library docs
    SKIP_SERENA=1
    SKIP_CONTEXT7=1
elif echo "$TASK_TEXT" | grep -qiE "library|framework|api.doc|context7|package|dependency|migrate"; then
    # Library-focused agents don't need code nav
    SKIP_SERENA=1
elif echo "$TASK_TEXT" | grep -qiE "refactor|rename.*symbol|find.*method|find.*class|type.hierarch|navigate.code"; then
    # Code-nav agents don't need library docs
    SKIP_CONTEXT7=1
fi

if [[ $SKIP_SERENA -eq 0 ]]; then
cat <<'SERENA'
## Serena MCP — Code Intelligence (sn plugin)

**Use Serena for symbol tasks; Grep for text.** Project auto-activated via `--project-from-cwd`.

### Setup — load tools before first use

Tool names vary by backend (JetBrains vs LSP). Load via ToolSearch with both variants — only the available ones resolve:

```
# Symbol tools (one of each pair will resolve depending on backend)
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_find_symbol,mcp__plugin_sn_serena__find_symbol")
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_find_referencing_symbols,mcp__plugin_sn_serena__find_referencing_symbols")
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_get_symbols_overview,mcp__plugin_sn_serena__get_symbols_overview")
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_type_hierarchy,mcp__plugin_sn_serena__type_hierarchy")
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_rename,mcp__plugin_sn_serena__rename_symbol")

# These names are the same on both backends
ToolSearch("select:mcp__plugin_sn_serena__replace_symbol_body,mcp__plugin_sn_serena__insert_before_symbol,mcp__plugin_sn_serena__insert_after_symbol")
ToolSearch("select:mcp__plugin_sn_serena__search_for_pattern,mcp__plugin_sn_serena__find_file,mcp__plugin_sn_serena__list_dir")
```

Then call `mcp__plugin_sn_serena__initial_instructions` for full backend-specific usage guidance.

### Task → Tool Mapping

| Task | Tool (use whichever resolved above) |
|------|-------------------------------------|
| Find symbol definition | `find_symbol` / `jet_brains_find_symbol` |
| Find all callers/references | `find_referencing_symbols` / `jet_brains_find_referencing_symbols` |
| File structure overview | `get_symbols_overview` / `jet_brains_get_symbols_overview` |
| Class/type hierarchy | `type_hierarchy` / `jet_brains_type_hierarchy` |
| Replace function body | `replace_symbol_body` |
| Insert code at symbol | `insert_before_symbol` / `insert_after_symbol` |
| Rename across codebase | `rename_symbol` / `jet_brains_rename` |
| Find files (gitignore-aware) | `find_file` |
| List directory (gitignore-aware) | `list_dir` |
| Text/pattern search | `search_for_pattern` |

Standard tools for: broad text search (Grep), reading known files (Read), writing new files (Write).

### Rules

- `get_symbols_overview` before reading whole files.
- `find_referencing_symbols` before any signature change.
- `find_symbol(include_body=false)` first, `true` only when you need the body.
SERENA
fi

if [[ $SKIP_CONTEXT7 -eq 0 ]]; then
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
fi
