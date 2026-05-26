---
description: Add, track, or verify structured research findings for an active RDR
---

# RDR Research

!{
NEXUS_RDR_ARGS="${ARGUMENTS:-}" python3 "$CLAUDE_PLUGIN_ROOT/resources/rdr_commands/rdr_research.py"
}

## Subcommand and Arguments

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

- RDR directory is shown above (from `.nexus.yml` `indexing.rdr_paths[0]`).
- Subcommands: `add <id>`, `status <id>`, `verify <id> <seq>`.
- Parse subcommand and RDR ID from `$ARGUMENTS`.
- Existing T2 findings and file Research Findings section are pre-loaded above.
- Dispatch `codebase-deep-analyzer` or `deep-research-synthesizer` if investigation (not just recording) is requested.
