#!/bin/bash

# sn SessionStart hook: remind main conversation about Serena + Context7
# SubagentStart injects full tool signatures; this is a compact reminder.

cat <<'SN'

## sn: Serena + Context7 (injected by sn plugin)

Serena: code intelligence for symbol tasks (find_symbol, find_referencing_symbols, get_symbols_overview, type_hierarchy, replace_symbol_body, rename_symbol). Use instead of Grep for symbol work. Tool-name prefix varies by backend: JetBrains prefixes `jet_brains_`, LSP is unprefixed. Load via ToolSearch with both variants, or see the serena-code-nav skill.
Context7: use `resolve-library-id` + `query-docs` for library/framework docs BEFORE relying on training data.
SN
