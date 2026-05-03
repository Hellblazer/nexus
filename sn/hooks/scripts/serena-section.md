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
