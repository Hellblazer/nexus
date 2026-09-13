---
allowed-tools: Bash
description: Accept a gated RDR — verifies gate PASSED in T2, updates status to accepted
---

# RDR Accept

!`nx rdr preamble rdr-accept`

## RDR to Accept

$ARGUMENTS

**Targeted load**: parse the **numeric ID** from `$ARGUMENTS` and run, via the
Bash tool, `nx rdr preamble rdr-accept -- <ID>` (literal argv token). Never
splice raw `$ARGUMENTS` into a shell-quoted line — free text with
apostrophes/quotes breaks the quoting (nexus-ybvyo).

## Action

**Notation**: All references to `<ID>` below mean the **RDR ID** value from the script output (e.g. `027`). All references to `<type>` mean the **Type** value (e.g. `design`). Substitute with the actual values.

All RDR metadata is pre-loaded above. Follow the `rdr-accept-checklist` skill body for the full procedure — the T2 idempotency/self-healing check, gate verification, residual disposition, the T2/frontmatter/README updates, and the planning-chain handoff (including the planning-chain-integrity prohibition). This command's own job is the preamble injection above and the argument parsing below; the skill is the single source for the procedure itself.
