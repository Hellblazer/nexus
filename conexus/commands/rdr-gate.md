---
allowed-tools: Bash
description: Run finalization gate on an RDR — structural, assumption audit, and AI critique
---

# RDR Gate

!`nx rdr preamble rdr-gate`

## RDR to Gate

$ARGUMENTS

**Targeted load**: parse the **numeric ID** (and any `--skip-research` flag)
from `$ARGUMENTS` and run, via the Bash tool, `nx rdr preamble rdr-gate -- <ID>`
with literal argv tokens. Never splice raw `$ARGUMENTS` into a shell-quoted
line — free text with apostrophes/quotes breaks the quoting (nexus-ybvyo).

## Action

All data is pre-loaded above. Follow the `rdr-gate-checklist` skill body for the full procedure — path detection, the Layer 0 re-gate sweep, the fix check, Layers 1-3, gate aggregation, and the on-pass/on-fail write-back. This command's own job is the preamble injection above and the argument parsing below; the skill is the single source for the procedure itself.

If no ID given, show the available RDR table above and prompt for an ID.
