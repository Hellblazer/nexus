---
allowed-tools: Bash
description: List all RDRs with status, type, and priority
---

# RDR List

!`nx rdr preamble rdr-list`

## Filters

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

Format the pre-gathered data as a clean index table. Apply any filters from `$ARGUMENTS` (e.g. `--status=draft`, `--type=feature`) to the table. The data source is shown (T2 or files fallback). T2 is the process authority; SessionStart reconciliation keeps it in sync with files.
