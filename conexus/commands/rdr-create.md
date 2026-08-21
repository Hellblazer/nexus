---
allowed-tools: Bash
description: Create a new RDR — scaffold from template, assign sequential ID, register in T2
---

# New RDR

!`nx rdr preamble rdr-create`

## Title / Details

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

- RDR directory is shown above (from `.nexus.yml` `indexing.rdr_paths[0]`).
- Use the existing RDR list to determine the next sequential ID (shown as **Next ID** above).
- Use the detected ID style (`RDR-NNN-*` or `NNN-*`) for the new filename.
- If `$ARGUMENTS` contains a title, pre-fill it; otherwise prompt.
- If the RDR directory does not exist, run bootstrap: create the directory and copy templates from `$CLAUDE_PLUGIN_ROOT/resources/rdr/`.
- Register the new RDR in T2: use **memory_put** tool: project="{repo}_rdr", title="{id}" with RDR metadata fields.
