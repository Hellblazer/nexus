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
- **Layer 0 — Re-gate survivor sweep** (whenever the preamble printed a `### Re-gate` block; it prints after a PASSED gate too): for every prior finding printed under "Prior findings" (the last two rounds' critiques; a finding absent from both, printed under "Retired from the sweep" instead, needs no re-check) that is not a recorded residual (dispositioned at accept, never a survivor to re-sweep), sweep every site in its `Sites:` list, or where a prior critique has none, grep the RDR file for the refuted phrasing AND the corrected one; every occurrence must agree before Layer 3. A fact lives in Problem Statement, Research Findings, Technical Design and the Implementation Plan at once, and the last two are where survivors hide. Brief the Layer 3 critic to verify each prior finding closed everywhere, then run a full-document consistency pass; re-read related RDRs only if the diff touched the Relationship section (nexus-7vdf9).
- **Fix check** (whenever the preamble printed `### Fix check (required before Layer 1)`): dispatch `substantive-critic` with ONLY the printed diff range and the RDR file; per added or changed clause: contradicted elsewhere in the file, an attribution / count / universal without an enumeration or quoted source (in the diff or in the research entry the fix cites), or a cited source that does not carry the claim. A `file:line` taken from a T3 search or query hit is a lead, not a citation: the store carries the line as of index time, and the clause passes only when the line was re-read from the working tree. For every identifier whose meaning, bound, or owning phase this change alters (a column, a caller-supplied parameter, a typed error, a setting, a phase or step number), list every other occurrence in the file, and every check, bound or rule stated over the value it names under any other name, and say whether each still holds. For every check, bound or rule this change adds, name the parameter, column or setting it constrains, and for every parameter, column or setting this change adds or alters, name every check, bound or rule that constrains it, whether or not they share a name, and a pair the previous check already named is not named again. Every row carries a `Class:` of exactly one of `BLOCKS-PLANNING` (an implementer executing the plan as written would build the wrong thing, or a step cannot run in the order given), `DISCOVER-AT-IMPLEMENTATION` (real, and the first test run or first hour at the keyboard surfaces it), or `OBSERVATION` (a wording, count, paraphrase or citation-form defect that changes no decision and no step; a documentation-accuracy defect is never BLOCKS-PLANNING). The fix check is three independent dispatches of this brief on the same range, never one; a defect counts only when at least two of the three raise it at the same site, its Class is the one at least two of the three assign, and a defect one critic alone raises is recorded as an observation and never fails the check; the check fails only on a counted BLOCKS-PLANNING defect; a failed check is fixed once and checked once more, and a second failure ends the loop with its counted defects recorded as residuals for accept, never a third run. Store the consensus verdict as T2 `{id}-fix-check-<sha>` with the sha the preamble printed. The fix check and the gate critique are never dispatched against the same commit in parallel: fix, then check, then Layer 1 and Layer 3. The gate record's `fix_check:` names that sha.
- Run all three gate layers in sequence:
  - **Layer 1 — Structural**: Use the Section Structure and Section Summaries above to check completeness (required headings present, no empty sections). **If no research findings exist** and `--skip-research` was NOT passed, report **BLOCKED** and stop — do not proceed to Layer 2 or 3. If `--skip-research` was passed, note the override and continue.
  - **Layer 2 — Assumption audit**: Use T2 Research Findings above to verify assumptions are evidenced. Every finding classified as "Assumed" must have an explicit risk assessment.
  - **Layer 3 — AI critique**: Dispatch the `substantive-critic` agent via Agent tool with the full RDR content. If the RDR has `related_issues` listing other RDR IDs, read those RDRs and include their content in the critique prompt — the critic should check for consistency and contradictions between related RDRs (P7). Include the register question from `resources/rdr/REGISTER.md` in the brief as criterion 6, warn-class only (a readability miss never fails or blocks the gate): could a smart reader who does not know this project's jargon follow the problem and the decision from this RDR alone? Ask for a list of undefined terms and tribal-knowledge sentences. The critique itself is addressed to the author: name the defect, where it is, and what better looks like. Every Critical and Significant also carries a `Class:` line, `BLOCKS-PLANNING` or `DISCOVER-AT-IMPLEMENTATION` (the substantive-critic agent's Output Format states which); `Ship-blocker: yes` implies `Class: BLOCKS-PLANNING`.
- Gate outcomes: **BLOCKED** or **PASSED**, computed by `nx rdr preamble rdr-verdict -- <id> <critique-title>` after the critique is stored in T2: rounds 1 and 2 block on any Critical; from round 3 only `ship_blockers > 0` blocks and every other finding is a residual recorded in the gate record's `residuals:` field for accept to disposition (`review-rounds.toml`), and the gate appends the one printed Revision History line (date, round, outcome, counts, ship-blockers, the commit and the critique's T2 record title — nothing else) to Revision History. Write the record it prints; never compute the outcome by hand. Criterion 6 is never a finding. Do not use "Conditional Accept" or other ad-hoc outcomes. Classification governs disposition, not blocking: `ship_blockers` stays the sole blocking field; every `residuals:` line records its finding's class (`[BLOCKS-PLANNING] <title>` or `[DISCOVER-AT-IMPLEMENTATION] <title>`, an unclassified finding defaulting to `BLOCKS-PLANNING` for disposition only). A finding carrying both `Ship-blocker: yes` and `Class: DISCOVER-AT-IMPLEMENTATION` is a contradiction: the verdict tool refuses to compute an outcome and names the finding and both values. `residuals:` is a union across rounds: a residual absent from a later round's own critique is carried forward from the prior gate record rather than dropped, marked `(carried from round <N>)` and keeping its own class.
- **Fixing findings** goes through `/conexus:rdr-fix <id>`: from round 3, fix only findings marked `Ship-blocker: yes`; every other Critical and Significant is a residual — record it, do not fix it in this change; it is dispositioned at accept, never re-gated.
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
  residuals: <one line per residual finding, round 3 onward, each `  - [<class>] <title>`, or `  - [<class>] <title> (carried from round <N>)` when it is carried forward from the prior record>
  prior: [<the previous round's own critique record id, never the latest record's id>] (<OUTCOME> <nC> <nS>), <the previous record's own prior chain>
  ```
  `critique:`, `commit:` and `prior:` are what the re-gate block reads; `fix_check:` must equal `commit:`. This overwrites any previous gate result for this RDR, so only the latest gate run is stored. `nx rdr preamble rdr-verdict` computes and prints this whole block, `prior:` included — copy it verbatim rather than retyping the chain by hand.
- **If PASSED**, print: `> Run '/conexus:rdr-accept <id>' to accept this RDR.`
- If no ID given, show the available RDR table above and prompt for an ID.
