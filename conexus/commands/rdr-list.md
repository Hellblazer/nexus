---
allowed-tools: Bash
description: List all RDRs with status, type, and priority
---

# RDR List

!`nx rdr preamble rdr-list`

## Filters

$ARGUMENTS

## Action

The table above is the command's own gathered data when the `!` preamble ran; if it is missing or empty, run `nx rdr preamble rdr-list` (or read `docs/rdr/README.md`) rather than reporting an empty index.

Format the pre-gathered data as a clean index table. Apply any filters from `$ARGUMENTS` (e.g. `--status=draft`, `--type=feature`) to the table. The data source is shown (T2 or files fallback). T2 is the process authority; nothing reconciles it with the files automatically, and `nx rdr preamble rdr-audit` prints a `DRIFT:` line per disagreement.
