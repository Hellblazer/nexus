---
name: rdr-gate
description: Use when an RDR appears complete and needs finalization validation — structural, assumption, and AI critique checks
effort: medium
---

# RDR Gate Skill

Optional validation for high-stakes decisions. Most RDRs don't need a formal gate — use this when the decision is expensive to reverse and you want to confront what you don't actually know before committing.

Delegates Layer 3 to the **substantive-critic** agent (sonnet). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- User says "gate this RDR", "finalization check", "is this RDR ready?"
- User invokes `/conexus:rdr-gate`
- User wants to validate an RDR before locking it as Final

## Input

- RDR ID (required) — e.g., `003`

## Path Detection

Resolve RDR directory from `.nexus.yml` `indexing.rdr_paths[0]`; default `docs/rdr`. Use the Step 0 snippet from the rdr-create skill, stored as `RDR_DIR`. All file paths below use `$RDR_DIR` in place of `docs/rdr`.

## Validation Layers (run in sequence)

### Layer 0 — Re-gate survivor sweep (whenever a prior gate record exists)

`nx rdr preamble rdr-gate <id>` prints a **Re-gate** block whenever the RDR
has a `{id}-gate-latest` record, PASSED or BLOCKED: the prior outcome, the
gate round number (derived from the record's `prior:` chain), the critique's
Critical and Significant lines verbatim, the diff of the RDR file since the
gated commit, and the Fix check section. Do this before anything else:

1. For every prior finding that is not a recorded residual (dispositioned at
   accept, never a survivor to re-sweep), sweep every site in its `Sites:`
   list (the critic emits one per finding; see the Layer 3 brief). Where a
   prior critique has no `Sites:` list, grep the RDR file for the refuted
   phrasing AND the corrected one. Every occurrence must agree. A fact lives
   in the Problem Statement, Research Findings, Technical Design and the
   Implementation Plan at once; the last two paraphrase the design and are
   where survivors hide. Fix every site.
2. If a finding changed a design decision, read Implementation Plan, Test Plan,
   Day 2 Operations, Trade-offs and Proportionality in full.
3. Brief the Layer 3 critic with the prior findings and ask it to verify each is
   closed everywhere, then run a full-document consistency pass. Re-read related
   RDRs only if the diff touched the Relationship section; otherwise the prior
   round's P7 check stands.

A first gate has no Layer 0. A re-gate after a PASSED result has one.

### Fix check — diff-scoped verification (whenever the RDR changed since the gated commit)

The Re-gate block prints `Fix check: not required` when the file is unchanged.
Otherwise it prints the exact range (`git diff <commit>..HEAD -- <file>`), the
fix commits, and the T2 title for the verdict. Then:

1. Dispatch `substantive-critic` with ONLY that diff and the RDR file as input.
   Brief, for every ADDED or CHANGED clause (a parenthetical or a trailing
   "and X" is its own item):
   1. Is it contradicted by any other line in this file? Cite both lines.
   2. Is it an attribution ("X created / set / owns / defines Y"), a count, or a
      universal (never / always / only / nothing / every / the one / all)? Then
      it needs an enumeration or the artifact's own text, quoted. Two sites that
      agree are not a source. The enumeration requirement applies equally to
      counts and universals in the research entry the fix cites.
   3. Does its cited source (changeset, file:line, RDR, T2 entry) contain the
      claim as stated?
   4. A `file:line` taken from a T3 search or query hit is a lead, not a
      citation: the store carries the line as of index time. The clause passes
      only when the line was re-read from the working tree.
   5. For every identifier whose meaning, bound, or owning phase this change
      alters (a column, a caller-supplied parameter, a typed error, a
      setting, a phase or step number), list every other occurrence in the
      file and say whether each still holds.
2. Deliverable: one row per clause, PASS or FAIL with line numbers, plus the
   standard Verdict block.
3. Store the verdict in T2: mcp__plugin_conexus_nexus__memory_put(project="{repo}_rdr", title="{id}-fix-check-<sha>", ttl="permanent", tags="rdr,gate,fix-check"), where `<sha>` is the RDR file's tip commit as printed by the preamble.
4. Any FAIL: fix it, re-run the fix check on the new diff. Do not enter Layer 1
   or Layer 3 with a FAIL open. Zero FAIL: proceed. The fix check and the gate
   critique are never dispatched against the same commit in parallel: fix,
   then check, then Layer 1 and Layer 3.

Every re-gated record (one with a `prior:` chain) carries `fix_check:`: either
`{repo}_rdr/{id}-fix-check-<sha>` with the sha equal to the record's `commit:`, or
the literal `none (no change since <sha>)` when the preamble printed `Fix check:
not required`. A record with neither is a skipped fix check, not a clean one. The
preamble prints `Fix check missing`, `Fix check pointer mismatch` or `Fix check
record missing` on the next run, and accept refuses the record in all three cases.
The fix check is a precondition and never counts toward the round cap.

### Fixing findings

Fixes go through `/conexus:rdr-fix <id>` (the rdr-fix skill); its preamble prints the findings with their Sites, the diff range and the pre-edit research title. The rules, restated:

- A fix changes the fact the critic named and nothing else. New glosses,
  rationales, parentheticals and counts are a separate commit, gated separately.
- Every clause a fix adds carries either a tool-produced quote from its source
  or an explicit "inferred, not read" marker.
- Write the research entry BEFORE the edit (rdr-research skill § Pre-edit
  capture): the quote or enumeration lives there first; the RDR sentence is
  derived from it.
- A universal claim (never / always / only / nothing / every / the one / all)
  or a count needs a census of the surface, not a spot read of two sites.
- From round 3, fix only findings marked `Ship-blocker: yes`; every other
  Critical and Significant is a residual — record it, do not fix it in this
  change; it is dispositioned at accept, never re-gated.
- A Criterion 6 readability WARN is never closed inside a fix commit.

### Layer 1 — Structural Validation (no AI)

Read the RDR markdown file. Check that these sections are present AND non-empty (not just the heading with placeholder text):

- Problem / Problem Statement
- Context (with Background and Technical Environment subsections)
- Research Findings (with Investigation and Key Discoveries subsections)
- Proposed Solution / Proposed Design / Decision (with Approach and Technical Design subsections)
- Alternatives Considered (at least one alternative with Pros/Cons/Rejection reason)
- Trade-offs (with Consequences and Risks subsections)
- Implementation Plan / Approach / Steps / Phases (with at least one numbered Phase/Step/Stage)
- Finalization Gate / Success Criteria (must have written responses, not just template placeholders)

**Heading matching**: RDRs use varied heading names. Match any of the variants listed above (separated by `/`). If none of the variants match, report the section as missing — do NOT silently skip it.

**Gap-structure sub-check (post-65 RDRs only)**: the `## Problem Statement` (or `## Problem`) section must contain one or more `#### Gap N: <title>` headings (regex `^#{3,5} Gap \d+:`). The command preamble script emits a BLOCKED outcome automatically when this check fails — the gate skill should not attempt to run Layers 2 or 3 after that block. Legacy RDRs with `id < 65` are grandfathered and skip the gap check. Use `/conexus:rdr-gate <id> --skip-gaps` to override for the rare RDR where the structure does not fit; the override is recorded in the gate audit trail.

**If any section is missing or contains only placeholder text** (e.g., `[What is the specific challenge]`):
- Report which sections are incomplete
- STOP — do not proceed to Layer 2 or 3
- Status remains Draft

### Layer 2 — Assumption Audit (from T2, no AI)

mcp__plugin_conexus_nexus__memory_get(project="{repo}_rdr", title=""

Filter entries matching `NNN-research-*`. Analyze:

1. Count by classification: verified, documented, assumed
2. Count by verification method: source_search, spike, docs_only
3. Flag high-risk items: classification=assumed AND verification_method=docs_only

Display:
```
Assumption Audit for RDR NNN:
- 3 verified (2 source search, 1 spike)
- 1 documented (docs only)
- 2 assumed — ⚠ UNRESOLVED
  [seq 4] "Library X supports feature Y" (docs only) ← HIGH RISK
  [seq 6] "Latency under 100ms" (docs only) ← HIGH RISK
```

If assumed findings remain:
- Ask: "Proceed with 2 unverified assumptions? (recorded as acknowledged)"
- If yes: update T2 records with `acknowledged: true`
- If no: STOP — user should verify or remove assumptions first

### Layer 3 — AI Critique (substantive-critic agent)

The critique itself follows `resources/rdr/REGISTER.md` (clear, simple, concise; addressed to the author, not the machine). Include this question in the critic's brief as criterion 6, WARN-CLASS ONLY (a readability miss never fails or blocks the gate): could a smart reader who does not know this project's jargon follow the problem and the decision from this RDR alone? The useful answer is a list: each undefined term, each sentence assuming tribal knowledge. (REGISTER.md names the reader per stage; jargon is fine when defined on first use.)

Before dispatch, seed link-context so the gate critique auto-links to the RDR:
```
mcp__plugin_conexus_nexus__scratch(action="put", content='{"targets": [{"tumbler": "<rdr-tumbler>", "link_type": "relates"}], "source_agent": "rdr-gate"}', tags="link-context")
```

Dispatch the `substantive-critic` agent via Agent tool with this relay:

```markdown
## Relay: substantive-critic

**Task**: Critique RDR NNN for internal consistency, missing failure modes, scope creep, and proportionality.
**Bead**: none

### Input Artifacts
- nx store: none
- nx memory: {repo}_rdr/NNN (status and research records)
- Files: docs/rdr/NNN-*.md

### Deliverable
Structured critique with pass/warn/fail per finalization gate criterion:
1. Contradiction Check — pass/warn/fail
2. Assumption Verification — pass/warn/fail
3. Scope Verification — pass/warn/fail
4. Cross-Cutting Concerns — pass/warn/fail
5. Proportionality — pass/warn/fail
6. Register Readability — pass/warn ONLY (never fail, never blocks): could a smart reader who does not know this project's jargon follow the problem and the decision from this RDR alone? List each undefined term and each sentence assuming tribal knowledge (see resources/rdr/REGISTER.md). A warn here is guidance to the author, not a gate outcome. A term listed here never appears as a Critical or Significant.

Gate round: N (from the preamble). Ship-blocker for an RDR gate: "yes" iff an implementer executing the plan as written would build the wrong thing, or the decision rests on a refuted assumption. A documentation-accuracy defect (wrong attribution, wrong citation, a wrong count that changes no decision) is never a ship-blocker.

Every Critical and Significant carries a `Sites:` line listing every file:line where the fact lives, so the author sweeps an enumerated set rather than a remembered phrase.

### Quality Criteria
- [ ] Every fail has a specific section reference and fix suggestion
- [ ] Every Critical and Significant has a Sites: list
- [ ] Warns are actionable but non-blocking
- [ ] Prior RDR search attempted (may return empty on cold-start)
```

**Prior-art search** (within the agent): Use catalog if available, fall back to raw search:
- First, try catalog (structured metadata): `mcp__plugin_conexus_nexus-catalog__search(query="relevant terms from problem statement", content_type="rdr")`
  - If results found, use `mcp__plugin_conexus_nexus-catalog__links(tumbler="<result>", direction="both")` to discover related RDRs in the graph
- If catalog empty or not initialized, fall back to T3 semantic search:
  - Use store_list tool to enumerate collections, filter by the `rdr__` prefix (RDR-103 conformant names start `rdr__<owner>__voyage-context-3__v1`)
  - mcp__plugin_conexus_nexus__search(query="relevant query terms from RDR problem statement", corpus="{each_collection}", limit=5
If no collections found: "No prior RDRs indexed. Cross-project prior-art search will improve as RDRs are indexed and closed."

### Gate Aggregation

After storing the critique, run `nx rdr preamble rdr-verdict -- <id> <critique-title>`
and write the gate record it prints, field for field. Never compute the outcome
by hand. The rules it applies (`review-rounds.toml`, contract `rdr-gate`):

- Rounds 1 and 2: BLOCKED iff `critical_count > 0`. Significants never block.
- Round 3 onward: BLOCKED iff `ship_blockers > 0`. Every other Critical and
  Significant is a residual: the gate writes `outcome: PASSED` with one
  `residuals:` line per finding in the gate record, appends "Gate N residuals"
  to Revision History, and accept dispositions each one (rdr-accept skill).
- A Verdict with no `ship_blockers` line is read as `ship_blockers = critical_count`
  (the conservative default of `nexus.plans.audit_rounds`); never as zero.
- Criterion 6 output is never a finding and never counted.
- Warns only, or all pass → PASSED. Status remains Draft.

**Important**: The AI critique *supplements* but does not *replace* the author completing the Finalization Gate section with written responses. The gate should verify that the Finalization Gate section contains substantive written responses, not just "N/A" or placeholder text.

### On Pass

1. Store the critique in T2 FIRST: mcp__plugin_conexus_nexus__memory_put(content="{critique}", project="{repo}_rdr", title="{id}-gate-critique-{date}", ttl="permanent", tags="rdr,gate,critique"). Same-day re-gates append a letter (`{date}b`, `{date}c`). T2 is where the preamble reads; a T3 copy (collection="<subject>", title="gate-rdr-NNN-{date}") is optional and never the only copy.
2. Write gate result to T2: mcp__plugin_conexus_nexus__memory_put(content="outcome: PASSED\ndate: YYYY-MM-DD\ncritical_count: 0\nsignificant_count: N\nobservation_count: N\nship_blockers: 0\nsummary: One-sentence summary\ncritique: {repo}_rdr/{id}-gate-critique-{date}\ncommit: <git log -1 --format=%h -- <rdr file>>\nfix_check: <{repo}_rdr/{id}-fix-check-<sha>, sha equal to commit:, or 'none (no change since <sha>)'; mandatory on every re-gate>\nresiduals: <one line per residual finding, round 3+>\nprior: [<previous record id>] (<OUTCOME> <nC> <nS>), <the previous record's own prior chain>", project="{repo}_rdr", title="{id}-gate-latest", ttl="permanent", tags="rdr,gate"). `critique:`, `commit:` and `prior:` are what the re-gate block reads; `fix_check:` must equal `commit:`.
3. Append gate findings to the RDR's Revision History section
4. Print: `> Run '/conexus:rdr-accept <id>' to accept this RDR.`

Status remains **Draft** until the author explicitly accepts via `/conexus:rdr-accept`.

### On Fail

1. Store the critique in T2 first (same title scheme as On Pass), then write the gate result (same format, `outcome: "BLOCKED"`, with `critique:` and `commit:`). The next `nx rdr preamble rdr-gate` run turns these two fields into the Layer 0 block.
2. T3 copy optional (collection="<subject>", title="gate-rdr-NNN-{date}", tags="rdr,gate,critique,blocked")
3. Display the critique with specific sections to address
4. Status remains Draft

## Relay Template (Use This Format)

When dispatching the substantive-critic agent via Agent tool for Layer 3 critique, use this exact structure:

```markdown
## Relay: substantive-critic

**Task**: Critique RDR NNN for internal consistency, missing failure modes, scope creep, and proportionality.
**Bead**: [ID] (status: [status]) or 'none'

### Input Artifacts
- nx store: [prior RDR collections or "none"]
- nx memory: {repo}_rdr/NNN (status and research records)
- nx scratch: [scratch IDs from Layer 1/2 or "none"]
- Files: docs/rdr/NNN-*.md

### Deliverable
Structured critique with pass/warn/fail per finalization gate criterion:
1. Contradiction Check
2. Assumption Verification
3. Scope Verification
4. Cross-Cutting Concerns
5. Proportionality
6. Register Readability (pass/warn ONLY — never fail, never blocks; list undefined terms and tribal-knowledge sentences per resources/rdr/REGISTER.md)

### Quality Criteria
- [ ] Every fail has a specific section reference and fix suggestion
- [ ] Warns are actionable but non-blocking
- [ ] Prior RDR search attempted (may return empty on cold-start)
```

**Required**: All fields must be present. Agent will validate relay before starting.

For additional optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Success Criteria

- [ ] RDR directory resolved from `.nexus.yml` `indexing.rdr_paths[0]` (default `docs/rdr`)
- [ ] Layer 1 structural validation completed (all required sections present and non-empty)
- [ ] Layer 2 assumption audit completed (findings counted by classification and method)
- [ ] High-risk items flagged (classification=assumed AND verification_method=docs_only)
- [ ] Layer 3 AI critique dispatched and results aggregated
- [ ] Fix check run on the diff since the gated commit, verdict stored as `{id}-fix-check-<sha>`, before Layer 1
- [ ] Gate outcome computed by `nx rdr preamble rdr-verdict -- <id> <critique-title>`, never by hand
- [ ] Gate result written to T2 as `{id}-gate-latest` (both pass and fail), with `prior:` chain, `fix_check:` and `residuals:`
- [ ] On pass: gate findings appended to Revision History, accept prompt displayed
- [ ] On fail: specific sections to address displayed to user

## Agent-Specific PRODUCE

Outputs generated by the substantive-critic agent (Layer 3):

- **T2 memory**: the critique itself via memory_put tool: project="{repo}_rdr", title="{id}-gate-critique-{date}", tags="rdr,gate,critique" (the preamble reads this on a re-gate); T3 copy optional
- **T2 memory**: Gate result record via memory_put tool: project="{repo}_rdr", title="{id}-gate-latest", ttl="permanent", tags="rdr,gate" (outcome: PASSED or BLOCKED)
- **T1 scratch**: Layer 1/2 validation notes via scratch tool: action="put", content="Gate RDR NNN: Layer 1 structural check", tags="rdr,gate" (promoted to T2 on completion)

**Session Scratch (T1)**: Use scratch tool for ephemeral notes during multi-layer validation. Flagged items auto-promote to T2 at session end.

## Known Limitations

**T2 retrieval is O(N):** Layer 2's memory_get tool with project="{repo}_rdr", title="" returns all records. Client-side filtering by title pattern (`NNN-research-*`) is required. Validate that parsed records have `rdr_id` and `seq` fields before using them.
