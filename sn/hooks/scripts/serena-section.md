## Serena MCP — Code Intelligence (sn plugin)

**Use Serena for symbol tasks; Grep for text.** Project auto-activated via `--project-from-cwd`.

**Root is fixed at server start.** Serena resolves every path against the project root it found when the MCP server started, not against your cwd. If your cwd is a linked git worktree (dispatched with `isolation: "worktree"`), Serena's write tools are denied by the sn hook and a worktree section above this one tells you what to use instead; read tools still answer, against the primary checkout.

### Setup — load tools before first use

Tool names vary by backend. The JetBrains backend prefixes `jet_brains_`; the LSP backend is unprefixed. Load both variants via ToolSearch; only the available ones resolve:

```
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_find_symbol,mcp__plugin_sn_serena__find_symbol")
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_find_referencing_symbols,mcp__plugin_sn_serena__find_referencing_symbols")
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_get_symbols_overview,mcp__plugin_sn_serena__get_symbols_overview")
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_type_hierarchy,mcp__plugin_sn_serena__jet_brains_find_implementations,mcp__plugin_sn_serena__find_implementations")
ToolSearch("select:mcp__plugin_sn_serena__jet_brains_rename,mcp__plugin_sn_serena__rename_symbol")
ToolSearch("select:mcp__plugin_sn_serena__replace_in_files,mcp__plugin_sn_serena__replace_symbol_body,mcp__plugin_sn_serena__insert_before_symbol,mcp__plugin_sn_serena__insert_after_symbol")
```

Then call `mcp__plugin_sn_serena__initial_instructions` for full backend-specific usage guidance.

### Task → Tool Mapping

| Task | JetBrains backend | LSP backend |
|------|-------------------|-------------|
| Find symbol definition | `jet_brains_find_symbol` | `find_symbol` |
| Find all callers/references | `jet_brains_find_referencing_symbols` | `find_referencing_symbols` |
| File structure overview | `jet_brains_get_symbols_overview` | `get_symbols_overview` |
| Class/type hierarchy | `jet_brains_type_hierarchy` | none; `find_implementations` covers the downward direction |
| Implementations of an interface | `jet_brains_find_implementations` | `find_implementations` |
| Inline a symbol / delete safely | `jet_brains_inline_symbol` / `jet_brains_safe_delete` | `safe_delete_symbol` |
| Rename across codebase | `jet_brains_rename` | `rename_symbol` |
| Replace function body | `replace_in_files` | `replace_symbol_body` |
| Insert code at symbol | `replace_in_files` | `insert_before_symbol` / `insert_after_symbol` |
| Move a symbol | `jet_brains_move` | (Edit) |
| Static analysis on a file | `jet_brains_run_inspections` | `get_diagnostics_for_file` |

`find_file`, `list_dir`, and `search_for_pattern` are excluded in this context: use Glob, Bash, and Grep. Standard tools for broad text search (Grep), reading known files (Read), writing new files (Write).

### Rules

- `get_symbols_overview` before reading whole files.
- `find_referencing_symbols` before any signature change.
- `find_symbol(include_body=false)` first, `true` only when you need the body.
