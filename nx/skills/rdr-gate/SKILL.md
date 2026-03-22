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
- User invokes `/nx:rdr-gate`
- User wants to validate an RDR before locking it as Final

## Input

- RDR ID (required) — e.g., `003`

## Path Detection

Resolve RDR directory from `.nexus.yml` `indexing.rdr_paths[0]`; default `docs/rdr`. Use the Step 0 snippet from the rdr-create skill, stored as `RDR_DIR`. All file paths below use `$RDR_DIR` in place of `docs/rdr`.

## Three Validation Layers (run in sequence)

### Layer 1 — Structural Validation (no AI)

Read the RDR markdown file. Check that these sections are present AND non-empty (not just the heading with placeholder text):

- Problem Statement
- Context (with Background and Technical Environment subsections)
- Research Findings (with Investigation and Key Discoveries subsections)
- Proposed Solution (with Approach and Technical Design subsections)
- Alternatives Considered (at least one alternative with Pros/Cons/Rejection reason)
- Trade-offs (with Consequences and Risks subsections)
- Implementation Plan (with at least one numbered Phase/Step)
- Finalization Gate (must have written responses, not just template placeholders)

**If any section is missing or contains only placeholder text** (e.g., `[What is the specific challenge]`):
- Report which sections are incomplete
- STOP — do not proceed to Layer 2 or 3
- Status remains Draft

### Layer 2 — Assumption Audit (from T2, no AI)

Use memory_get tool: project="{repo}_rdr", title=""

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

### Quality Criteria
- [ ] Every fail has a specific section reference and fix suggestion
- [ ] Warns are actionable but non-blocking
- [ ] Prior RDR search attempted (may return empty on cold-start)
```

**Prior-art search** (within the agent): enumerate RDR collections and search:
- Use store_list tool to enumerate collections, filter for `rdr__`
- Use search tool: query="relevant query terms from RDR problem statement", corpus="{each_collection}", n=5
If no collections found: "No prior RDRs indexed. Cross-project prior-art search will improve as RDRs are indexed and closed."

### Gate Aggregation

- Any **fail** → gate fails. Status remains Draft.
- **Warns only** → gate passes. Warns surfaced to user but do not block.
- All **pass** → gate passes.

**Important**: The AI critique *supplements* but does not *replace* the author completing the Finalization Gate section with written responses. The gate should verify that the Finalization Gate section contains substantive written responses, not just "N/A" or placeholder text.

### On Pass

1. Write gate result to T2: Use memory_put tool: content="outcome: PASSED\ndate: YYYY-MM-DD\ncritical_count: 0\nsignificant_count: N\nobservation_count: N\nsummary: One-sentence summary", project="{repo}_rdr", title="{id}-gate-latest", ttl="permanent", tags="rdr,gate"
2. Append gate findings to the RDR's Revision History section
3. Print: `> Run '/nx:rdr-accept <id>' to accept this RDR.`

Status remains **Draft** until the author explicitly accepts via `/nx:rdr-accept`.

### On Fail

1. Write gate result to T2 (same format, `outcome: "BLOCKED"`)
2. Display the critique with specific sections to address
3. Status remains Draft

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
- [ ] Gate result determined: pass (all pass or warns only) or fail (any fail)
- [ ] Gate result written to T2 as `{id}-gate-latest` (both pass and fail)
- [ ] On pass: gate findings appended to Revision History, accept prompt displayed
- [ ] On fail: specific sections to address displayed to user

## Agent-Specific PRODUCE

Outputs generated by the substantive-critic agent (Layer 3):

- **T3 knowledge**: Gate results via store_put tool: content="# Gate: RDR NNN\n{critique}", collection="knowledge", title="gate-rdr-NNN-{date}", tags="rdr,gate,critique"
- **T2 memory**: Gate result record via memory_put tool: project="{repo}_rdr", title="{id}-gate-latest", ttl="permanent", tags="rdr,gate" (outcome: PASSED or BLOCKED)
- **T1 scratch**: Layer 1/2 validation notes via scratch tool: action="put", content="Gate RDR NNN: Layer 1 structural check", tags="rdr,gate" (promoted to T2 on completion)

**Session Scratch (T1)**: Use scratch tool for ephemeral notes during multi-layer validation. Flagged items auto-promote to T2 at session end.

## Known Limitations

**T2 retrieval is O(N):** Layer 2's memory_get tool with project="{repo}_rdr", title="" returns all records. Client-side filtering by title pattern (`NNN-research-*`) is required. Validate that parsed records have `rdr_id` and `seq` fields before using them.
