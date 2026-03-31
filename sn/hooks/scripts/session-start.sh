#!/bin/bash

# sn SessionStart hook — remind main conversation about Serena + Context7
# SubagentStart injects full tool signatures; this is a compact reminder.

cat <<'SN'

## sn: Serena + Context7 (injected by sn plugin)

Serena: LSP code intelligence — `jet_brains_find_symbol`, `find_referencing_symbols`, `get_symbols_overview`, `type_hierarchy`, `replace_symbol_body`, `rename_symbol`. Use for symbol tasks; Grep for text.
Context7: use `resolve-library-id` + `query-docs` for library/framework docs BEFORE relying on training data.
SN
