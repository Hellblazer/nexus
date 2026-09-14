---
allowed-tools: Bash
description: Fix an RDR's gate findings — prints the findings with their sites, the diff since the gated commit, the pre-edit research title, and the fix rules
---

# RDR Fix

!`nx rdr preamble rdr-fix`

## RDR to Fix

$ARGUMENTS

**Targeted load**: parse the **numeric ID** from `$ARGUMENTS` and run, via the
Bash tool, `nx rdr preamble rdr-fix -- <ID>` (literal argv token). Never splice
raw `$ARGUMENTS` into a shell-quoted line.

## Action

All data is pre-loaded above. Follow the `rdr-fix-checklist` skill body for the full procedure — the stop conditions, the pre-edit research entry, the identifier/crosswalk clauses, the fix check dispatch, and the commit rules. This command's own job is the preamble injection above and the argument parsing below; the skill is the single source for the procedure itself.

Do not run the gate; the user drives lifecycle transitions.
