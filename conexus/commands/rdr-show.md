---
description: Show detailed information about a specific RDR including content, research findings, and linked beads
---

# RDR Show

!{
NEXUS_RDR_ARGS="${ARGUMENTS:-}" python3 "$CLAUDE_PLUGIN_ROOT/resources/rdr_commands/rdr_show.py"
}

## RDR to Show

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

- If an RDR ID was given: display metadata table, full content, T2 metadata, research findings, and linked beads.
- If no ID given: display the list table + content index (most recently modified first).
- RDR ID is parsed from `$ARGUMENTS` (e.g. `003`, `RDR-003`, or `NX-003`).
- If the ID is not found, show the available RDR list as fallback.
