---
allowed-tools: Bash
description: Cross-walk RDR §Approach against closing beads at a phase boundary — blocks silent scope reduction
---

# Phase Review Gate

!`nx rdr preamble phase-review-gate -- '$ARGUMENTS'`

## Phase Review Gate

$ARGUMENTS

## Action

All data is pre-loaded above. The preamble has enumerated (Pass 1) or validated (Pass 2) the §Approach cross-walk.

**After Pass 1**: collect evidence pointers from the user for each §Approach item, then re-invoke with `--evidence`.

**After Pass 2 PASSED**: proceed to close the phase. Remind the user: evidence pointers are not semantically verified — review each one manually.

**After Pass 2 BLOCKED**: the phase cannot close until all §Approach items have evidence. Work with the user to identify the missing work or explicitly defer with `none` + justification.
