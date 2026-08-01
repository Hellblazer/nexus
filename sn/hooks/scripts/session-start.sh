#!/bin/bash

# sn SessionStart hook: remind main conversation about Serena + Context7
# SubagentStart injects full tool signatures; this is a compact reminder.

cat <<'SN'

## sn: Serena + Context7 (injected by sn plugin)

Serena: code intelligence for symbol tasks (find_symbol, find_referencing_symbols, get_symbols_overview, type_hierarchy, rename_symbol). Use instead of Grep for symbol work. Backend prefix varies: JetBrains `jet_brains_`, LSP unprefixed — see the serena-code-nav skill.
Context7: `resolve-library-id` + `query-docs` for library docs BEFORE relying on training data.
SN
