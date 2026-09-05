
## sn: Serena + Context7 (injected by sn plugin)

Serena: code intelligence for symbol tasks (find_symbol, find_referencing_symbols, get_symbols_overview, type_hierarchy, rename_symbol). Use instead of Grep for symbol work. Backend prefix varies: JetBrains `jet_brains_`, LSP unprefixed — see the serena-code-nav skill.
Worktrees: Serena writes to the root fixed at server start. Subagents dispatched with `isolation: "worktree"` get Serena write tools DENIED by the sn hook and are told to use the built-in `LSP` tool plus Edit with absolute paths; brief them that way and verify the primary tree (`git status --short`) after any worktree fan-out.
Context7: `resolve-library-id` + `query-docs` for library docs BEFORE relying on training data.
