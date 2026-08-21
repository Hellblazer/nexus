---
allowed-tools: Bash
description: Show detailed information about a specific RDR including content, research findings, and linked beads
---

# RDR Show

!`nx rdr preamble rdr-show`

## RDR to Show

$ARGUMENTS

**Targeted load**: if an RDR ID appears in `$ARGUMENTS`, run, via the Bash
tool, `nx rdr preamble rdr-show -- <ID>` with the parsed **numeric ID** as a
literal argv token (e.g. `nx rdr preamble rdr-show -- 003`). Never splice raw
`$ARGUMENTS` into a shell-quoted line — free text with apostrophes/quotes
breaks the quoting (nexus-ybvyo).

## Action

All data is pre-loaded above — no additional tool calls needed.

- If an RDR ID was given: display metadata table, full content, T2 metadata, research findings, and linked beads.
- If no ID given: display the list table + content index (most recently modified first).
- RDR ID is parsed from `$ARGUMENTS` (e.g. `003`, `RDR-003`, or `NX-003`).
- If the ID is not found, show the available RDR list as fallback.
