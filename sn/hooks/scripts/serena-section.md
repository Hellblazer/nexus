## Serena MCP — Code Intelligence (sn plugin)

**Use Serena for symbol tasks; Grep for text.** Project auto-activated via `--project-from-cwd`.

**Root is fixed at server start.** Serena resolves every path against the project root it found when its MCP server started, not against your cwd. In a linked git worktree (`isolation: "worktree"`) the sn hook denies Serena's write tools and the worktree section above says what to use instead; read tools still answer, against the primary checkout.

**Serena writes are not auto-approved.** sn approves Serena's read tools only. Its write tools, `jet_brains_debug`, `query_project`, `onboarding` and `restart_language_server` go through Claude Code's permission flow, like Edit and Write. If one is denied or never answered, do not retry it: use Edit or Write on the absolute path, or report that you are blocked.

### Setup — load tools before first use

Tool names vary by backend: JetBrains prefixes `jet_brains_`, LSP is unprefixed. Load what you need in one ToolSearch call naming both variants, as full names `mcp__plugin_sn_serena__<tool>` from the table below; only the available ones resolve. Example: `ToolSearch("select:mcp__plugin_sn_serena__jet_brains_find_symbol,mcp__plugin_sn_serena__find_symbol")`. Then call `mcp__plugin_sn_serena__initial_instructions` for backend-specific usage.

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

`find_file`, `list_dir` and `search_for_pattern` are excluded here: use Glob, Bash and Grep.

### Rules

- `get_symbols_overview` before reading whole files.
- `find_referencing_symbols` before any signature change.
- `find_symbol(include_body=false)` first, `true` only when you need the body.
