
## sn: Serena + Context7 (injected by sn plugin)

Serena: code intelligence for symbol tasks (find_symbol, find_referencing_symbols, get_symbols_overview, type_hierarchy, rename_symbol). Use instead of Grep for symbol work. Backend prefix varies: JetBrains `jet_brains_`, LSP unprefixed — see the serena-code-nav skill.
Worktrees: Serena writes to the root fixed at server start. Subagents dispatched with `isolation: "worktree"` get Serena write tools DENIED by the sn hook and are told to use the built-in `LSP` tool plus Edit with absolute paths; brief them that way and verify the primary tree (`git status --short`) after any worktree fan-out. If THIS session relocates into a worktree by absolute path (cwd never actually moves there), a Serena write can SUCCEED against the primary instead — its own "DRY RUN" report is not trustworthy proof otherwise (nexus-ebx0s) — so prefer Edit/Write with absolute paths unless you are certain this session started inside the worktree it is editing.
Context7: `resolve-library-id` + `query-docs` for library docs BEFORE relying on training data.
