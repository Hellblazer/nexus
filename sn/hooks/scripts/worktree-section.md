## Worktree isolation — Serena writes are BLOCKED here (sn plugin)

Your cwd is a linked git worktree. The Serena MCP server is shared with the main session and writes to the project root it resolved at startup, which is the PRIMARY checkout, not this worktree. Three past incidents: the tool reported success and the edit landed in the wrong tree. The sn PreToolUse hook now denies every Serena write tool (`replace_in_files`, `replace_symbol_body`, `insert_*_symbol`, `rename_symbol`, `jet_brains_rename`/`move`/`safe_delete`/`inline_symbol`, `replace_content`, `*_lines`, memory writes) while your cwd is a worktree. Do not try to route around the denial.

### What to use instead

| Task | Tool |
|------|------|
| Definition, references, hover, document/workspace symbols, implementations, call hierarchy | built-in `LSP` tool (`operation=` `goToDefinition` / `findReferences` / `hover` / `documentSymbol` / `workspaceSymbol` / `goToImplementation` / `incomingCalls` / `outgoingCalls`), with the ABSOLUTE `filePath` under your worktree |
| Symbol lookup by name path, file overview, type hierarchy (read-only) | Serena `find_symbol` / `get_symbols_overview` / `jet_brains_*` READ tools are allowed; they resolve against the primary checkout, which matches your worktree until you edit a file, so prefer `LSP` for anything you have already changed |
| Any edit | `Edit` / `Write` with an ABSOLUTE path under your worktree, or `sed -i` via Bash on the absolute path |
| Rename across files | `Grep` for call sites, then `Edit` each; there is no worktree-safe rename |
| Text search | `Grep` with `path=` your worktree root |

### Verify every write

After each edit run `git status --short` in your worktree. If the tool reported success and your tree is unchanged, the write went elsewhere; stop and report it. Serena's own guidance (below or in `initial_instructions`) says to prefer its editing tools; in a worktree that guidance does not apply.
