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

All data is pre-loaded above — no additional tool calls needed, except the reads the Fix check and Layer 0 name (the diff range, and a prior critique whose findings carry no `Sites:` line).

- RDR directory is shown above (from `.nexus.yml` `indexing.rdr_paths[0]`).
- **Layer 0 — Re-gate survivor sweep** (whenever the preamble printed a `### Re-gate` block; it prints after a PASSED gate too): for every prior finding listed there, sweep every site in its `Sites:` list, or where a prior critique has none, grep the RDR file for the refuted phrasing AND the corrected one; every occurrence must agree before Layer 3. A fact lives in Problem Statement, Research Findings, Technical Design and the Implementation Plan at once, and the last two are where survivors hide. Brief the Layer 3 critic to verify each prior finding closed everywhere, then run a full-document consistency pass; re-read related RDRs only if the diff touched the Relationship section (nexus-7vdf9).
- **Fix check** (whenever the preamble printed `### Fix check (required before Layer 1)`): dispatch `substantive-critic` with ONLY the printed diff range and the RDR file; per added or changed clause: contradicted elsewhere in the file, an attribution / count / universal without an enumeration or quoted source (in the diff or in the research entry the fix cites), or a cited source that does not carry the claim. Store the verdict as T2 `{id}-fix-check-<sha>` with the sha the preamble printed; any FAIL is fixed and re-checked before Layer 1. The gate record's `fix_check:` names that sha.
- Run all three gate layers in sequence:
  - **Layer 1 — Structural**: Use the Section Structure and Section Summaries above to check completeness (required headings present, no empty sections). **If no research findings exist** and `--skip-research` was NOT passed, report **BLOCKED** and stop — do not proceed to Layer 2 or 3. If `--skip-research` was passed, note the override and continue.
  - **Layer 2 — Assumption audit**: Use T2 Research Findings above to verify assumptions are evidenced. Every finding classified as "Assumed" must have an explicit risk assessment.
  - **Layer 3 — AI critique**: Dispatch the `substantive-critic` agent via Agent tool with the full RDR content. If the RDR has `related_issues` listing other RDR IDs, read those RDRs and include their content in the critique prompt — the critic should check for consistency and contradictions between related RDRs (P7). Include the register question from `resources/rdr/REGISTER.md` in the brief as criterion 6, warn-class only (a readability miss never fails or blocks the gate): could a smart reader who does not know this project's jargon follow the problem and the decision from this RDR alone? Ask for a list of undefined terms and tribal-knowledge sentences. The critique itself is addressed to the author: name the defect, where it is, and what better looks like.
- Gate outcomes: **BLOCKED** or **PASSED**, by the round the preamble printed: rounds 1 and 2 block on any Critical; from round 3 only `ship_blockers > 0` blocks and every other finding is a residual recorded in the gate record (`residuals:`) and Revision History for accept to disposition. Criterion 6 is never a finding. Do not use "Conditional Accept" or other ad-hoc outcomes.
- **Store the critique in T2 FIRST**: memory_put project="{repo_name}_rdr", title="{id}-gate-critique-{date}" (same-day re-gates append a letter: `{date}b`, `{date}c`), ttl="permanent", tags="rdr,gate,critique", content=the critic's full report. T2 is where `nx rdr preamble rdr-gate` reads it back on a re-gate; a T3 copy is optional and never the only copy.
- **Write T2 gate result** after completing all layers. Use the repo name from above:
  Use **memory_put** tool: project="{repo_name}_rdr", title="{id}-gate-latest", ttl="permanent", tags="rdr,gate", content with:
  ```
  outcome: "PASSED"  # or "BLOCKED"
  date: "YYYY-MM-DD"
  critical_count: 0
  significant_count: 2
  observation_count: 3
  summary: "One-sentence summary of gate result"
  ship_blockers: 0
  critique: {repo_name}_rdr/{id}-gate-critique-{date}
  commit: <output of: git log -1 --format=%h -- <rdr file>>
  fix_check: <{repo_name}_rdr/{id}-fix-check-<sha> with sha equal to commit:, or 'none (no change since <sha>)'; mandatory on every re-gate (a record with prior:)>
  residuals: <one line per residual finding, round 3 onward>
  prior: [<previous gate-latest id>] (<OUTCOME> <nC> <nS>), <the previous record's own prior chain>
  ```
  `critique:`, `commit:` and `prior:` are what the re-gate block reads; `fix_check:` must equal `commit:`. This overwrites any previous gate result for this RDR, so only the latest gate run is stored.
- **If PASSED**, print: `> Run '/conexus:rdr-accept <id>' to accept this RDR.`
- If no ID given, show the available RDR table above and prompt for an ID.
