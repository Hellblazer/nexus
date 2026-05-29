---
allowed-tools: Bash
description: Cross-walk RDR §Approach against closing beads at a phase boundary — blocks silent scope reduction
---

# Phase Review Gate

!`nx rdr preamble phase-review-gate`

## Phase Review Gate

$ARGUMENTS

## Action

The preamble above shows usage and what this gate does (no id was passed to it). Parse the **RDR id** and `--phase N` from `$ARGUMENTS`, then drive the gate by invoking the preamble via the Bash tool — constructing the argv yourself so evidence/justification text (which may contain quotes or apostrophes) is passed as real argv tokens, never spliced into a shell-quoted line.

**Pass 1 — enumerate**: run, via the Bash tool, `nx rdr preamble phase-review-gate -- <ID> --phase <N>`. This lists the §Approach items to cross-walk. Collect an evidence pointer (bead id, or `none` + justification) from the user for each item.

**Pass 2 — validate**: run, via the Bash tool, `nx rdr preamble phase-review-gate -- <ID> --phase <N> --evidence "Item1=<ptr>,Item2=<ptr>,…"`, building the `--evidence` value as a single Bash-tool argument.

**After Pass 2 PASSED**: proceed to close the phase. Remind the user: evidence pointers are not semantically verified — review each one manually.

**After Pass 2 BLOCKED**: the phase cannot close until all §Approach items have evidence. Work with the user to identify the missing work or explicitly defer with `none` + justification.
