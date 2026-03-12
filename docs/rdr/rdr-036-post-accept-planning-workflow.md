---
title: "Post-Accept Planning Workflow"
id: RDR-036
type: enhancement
status: accepted
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-12
accepted_date: 2026-03-12
related_issues:
  - "RDR-024 - RDR Process Guardrails"
---

# RDR-036: Post-Accept Planning Workflow

## Problem Statement

The current RDR workflow places bead decomposition at close time (`/rdr-close` Step 3), after implementation is already complete. This is backwards — by then the beads serve only as retroactive bookkeeping rather than actionable work items. In practice, planning happens after accept, not after close.

The actual workflow that has emerged through 35+ RDRs:

1. Accept the RDR (`/rdr-accept`)
2. Manually invoke `/create-plan` to build out implementation beads
3. Manually invoke `/plan-audit` to validate
4. Execute the plan
5. Close the RDR (`/rdr-close`)

Steps 2-3 should be an optional, integrated handoff from `/rdr-accept` rather than manual separate invocations. And the bead decomposition currently in `/rdr-close` should be removed since beads are already created during planning.

## Context

The RDR lifecycle is: draft → gate → accept → implement → close. The accept-to-implement transition is where planning naturally fits — you've validated the design, now you need to decompose it into executable work. The close transition is where you record what happened, not where you plan what to do.

Additionally, the current close skill creates beads from the Implementation Plan section text, which produces shallow task descriptions. The strategic-planner agent creates richer beads with proper dependency graphs, success criteria, and test strategies.

### Complexity Threshold

Not every RDR warrants full planning ceremony. RDR-035 (delete one frontmatter line from 14 files) needed zero planning. RDR-034 (MCP server with 8 tools, test strategy, documentation) absolutely benefited from it. The trigger should be:

- **Skip planning**: single-phase fix, implementation obvious from the RDR
- **Invoke planning**: multi-phase, cross-cutting, or ambiguous implementation path

## Research Findings

### F-01: Close-time bead decomposition is never used as intended
- **Classification**: Verified — Project History
- **Method**: Review of 35 closed RDRs
- **Detail**: Bead decomposition at close time produces beads that are immediately closed. The Implementation Plan section is parsed into tasks, but the work is already done. These beads have no lifecycle value.

### F-02: Strategic planner produces richer work items than text extraction
- **Classification**: Verified — Usage
- **Method**: Comparison of planner output vs close-time decomposition
- **Detail**: The strategic-planner agent creates beads with dependency graphs, success criteria per phase, test strategies, and proper descriptions. Close-time decomposition does shallow text extraction from markdown headings.

### F-03: Plan-auditor catches issues before implementation starts
- **Classification**: Verified — Usage
- **Method**: Project experience with RDR-034
- **Detail**: Running plan-auditor after strategic-planner but before implementation catches gaps, ordering issues, and missing dependencies. This is the right time for the audit — after planning, before coding.

### F-04: Strategic planner already mandates plan-auditor as successor
- **Classification**: Verified — Source Search
- **Method**: Read `nx/agents/strategic-planner.md` lines 165-170
- **Detail**: The strategic-planner agent has a "Successor Enforcement (MANDATORY)" section that always relays to plan-auditor after creating a plan. This means the accept skill only needs to dispatch strategic-planner — the auditor handoff is already built into the agent chain. No separate dispatch logic needed.

### F-05: Accept skill currently has no agent delegation
- **Classification**: Verified — Source Search
- **Method**: Read `nx/skills/rdr-accept/SKILL.md` line 31
- **Detail**: The accept skill states "This skill executes directly — no agent delegation." Adding the planning handoff would be the first agent dispatch from the accept skill. The skill needs to be updated to support optional agent delegation.

### F-06: RDR content provides complete input for strategic planner relay
- **Classification**: Verified — Source Search
- **Method**: Read strategic-planner relay reception requirements
- **Detail**: The strategic-planner relay requires: Task (from RDR title + problem statement), Bead (create new epic), Input Artifacts (RDR file path + T2 metadata), Deliverable (phased plan with beads), Quality Criteria (from RDR success criteria). All fields can be auto-populated from the accepted RDR without additional user input.

### F-07: Auto-detection heuristic can use Implementation Plan section structure
- **Classification**: Verified — Source Search
- **Method**: Analysis of RDR template and closed RDRs
- **Detail**: The Implementation Plan section uses `### Phase N:` headings. Counting these headings gives a reliable complexity signal: 1 phase = simple (skip planning), 2+ phases = complex (suggest planning). (Note: F-10 supersedes the RDR-type component originally considered here; phase count alone is used.)

### F-08: Strategic planner lacks an explicit enrichment phase after audit
- **Classification**: Verified — Source Search
- **Method**: Read `nx/agents/strategic-planner.md` Phase 3 (lines 107-114) and Bead Content Requirements (lines 118-150)
- **Detail**: Phase 3 says "Iterate based on audit feedback until the plan passes review" but this is aspirational — the planner dispatches the auditor as a successor and is done. There is no mechanism for the audit findings to flow back into the beads. The Bead Content Requirements template describes what beads should contain (context, prerequisites, execution instructions, parallelization guidance, continuation state, validation) but the audit-identified gaps, refined dependency ordering, missing test strategies, and codebase alignment issues are never folded back in.

### F-09: Feedback loop to strategic planner is architecturally impossible
- **Classification**: Verified — Source Search (Gate C-1)
- **Method**: Read plan-auditor Successor Enforcement (lines 174-177)
- **Detail**: Plan-auditor's successor enforcement relays to `architect-planner` or `developer` — not back to strategic-planner. Adding a Phase 4 to the strategic planner cannot work because the planner dispatches the auditor as a relay successor and is done by the time audit findings exist. The relay chain is one-directional: planner → auditor → developer. Solving this requires a new agent in the chain that receives audit findings and enriches beads as a forward step, not a feedback loop.

### F-10: Auto-detection heuristic should use phase count only
- **Classification**: Verified — Analysis (Gate C-2)
- **Method**: Review of heuristic edge cases
- **Detail**: The original heuristic conflated phase count and RDR type, producing inconsistent defaults for non-bugfix single-phase RDRs (e.g., an architecture RDR with 1 phase hits "otherwise → ask" instead of "default no"). Simplify to: phases ≥ 2 → default yes; phases ≤ 1 or no Implementation Plan section → default no. Drop RDR type from the decision entirely.

### F-11: Accept-to-planner path must satisfy RDR-024 guardrails
- **Classification**: Verified — Cross-reference (Gate S-3)
- **Method**: Cross-check RDR-024 Guardrail 2 against proposed accept flow
- **Detail**: RDR-024's Guardrail 2 (strategic-planner pre-check) warns if a referenced RDR is not yet "accepted." The accept skill must complete the T2 `status: accepted` write (Step 2) before dispatching the planner (Step 6) to avoid a false warning. This ordering is natural (Step 2 precedes Step 6) but must be explicitly stated. Additionally, the accept-to-planner path intentionally bypasses RDR-024's Guardrail 1 (brainstorming-gate) — this is correct since the RDR has already passed the gate.

### F-12: Close skill needs bead discovery mechanism
- **Classification**: Verified — Analysis (Gate S-5)
- **Method**: Analysis of close skill bead advisory requirements
- **Detail**: For the close skill to report bead status as an advisory, it needs to discover which beads belong to the RDR. The accept skill should store the epic bead ID in T2 at the end of the planning flow. Close reads that field and walks `bd show <epic-id>` to get child statuses. If no epic bead ID exists in T2 (user skipped planning), close skips the advisory.

### F-13: T1 scratch enables lightweight relay with rich shared context
- **Classification**: Verified — Architecture
- **Method**: Analysis of T1 session sharing (RDR-010) and agent chain context needs
- **Detail**: The accept-to-enricher chain involves 3 agents that each produce context the next one needs. Passing all of this through relay text makes relays bloated and fragile. T1 scratch is purpose-built for this scenario: session-scoped, shared across the agent tree via PPID chain, and semantically searchable. Each agent writes its outputs to T1 (plan structure, bead IDs, audit findings, gap analysis) and the next agent discovers them via `scratch search`. This keeps relays lightweight (task description + pointers) while T1 carries the heavy context.

### F-14: Plan-auditor needs explicit routing discriminant for conditional successor
- **Classification**: Verified — Architecture
- **Method**: Analysis of plan-auditor successor enforcement and relay context
- **Detail**: Plan-auditor's Successor Enforcement currently always relays to `architect-planner` or `developer`. Adding plan-enricher as a conditional successor requires a discriminant the auditor can check. The accept skill writes a T1 scratch entry tagged `rdr-planning-context` with the RDR ID and planning metadata. Plan-auditor searches T1 for this tag and checks that the RDR ID in the tag matches the RDR ID in the current relay context: if both match, relay to `plan-enricher`; if tag absent or RDR ID mismatch (standalone audit, or unrelated plan-audit in same session), relay to existing successors. The RDR ID correlation prevents false positives when a session runs `/rdr-accept` for one RDR followed by an unrelated `/plan-audit`.

## Proposed Solution

### New agent: plan-enricher

A new agent (`nx/agents/plan-enricher.md`) that sits at the end of the planning chain. It receives audit findings from plan-auditor and enriches every bead to be fully self-contained and ready for autonomous execution.

**Agent chain (forward-only, no feedback loops):**
```
strategic-planner → plan-auditor → plan-enricher → done
```

**What plan-enricher does:**
- Searches T1 scratch for audit findings, plan structure, bead IDs, and gap analysis written by predecessors in the chain
- Reads every bead created by the strategic planner (discovers epic bead ID from T1)
- For each bead, enriches with:
  - Audit-identified gaps and mitigations (from T1 audit findings)
  - Refined dependency ordering per audit recommendations
  - Test strategy specifics the auditor flagged as missing
  - Codebase alignment issues the auditor discovered
  - Full execution context (search keywords, memory pointers, prerequisite state)
- Updates each bead via `bd update <id> --description` with enriched content
- Writes epic bead ID directly to T2 (`memory_put` to `{repo}_rdr/NNN` with `epic_bead: <id>`) — the accept skill's execution context is gone by this point, so plan-enricher owns the T2 persistence (Gate S-2)
- **Degraded mode (T1 miss):** If T1 scratch yields no audit findings (standalone invocation without prior `/plan-audit` in session, or semantic search miss): warn the user that audit findings are unavailable, proceed to enrich beads with execution context only (codebase alignment, search keywords, prerequisite state), and skip audit-finding injection. Do not abort.
- Reports the final enriched plan to the user

**T1 scratch as the shared context bus (F-13):**

Each agent in the chain writes its outputs to T1 and reads predecessors' context from T1. Relays stay lightweight (task + RDR reference), T1 carries the heavy context:

| Agent | Writes to T1 | Reads from T1 |
|-------|-------------|---------------|
| Accept skill | RDR content, T2 metadata, planning context (tagged `rdr-planning-context`) | — |
| Strategic planner | Plan structure, bead IDs, dependency graph, design rationale | RDR context from accept |
| Plan-auditor | Audit findings, gap analysis, severity classifications | Plan structure, bead IDs, `rdr-planning-context` tag (for successor routing) |
| Plan-enricher | Epic bead ID, enrichment summary | All of the above |

Within the same session, `/enrich-plan` can be invoked standalone — if T1 has audit findings from a same-session `/plan-audit`, the enricher finds them via T1 scratch search without being part of a relay chain.

**Wiring (F-14):**
- Accept skill writes a T1 scratch entry tagged `rdr-planning-context` with the RDR ID and planning metadata before dispatching strategic-planner
- Update plan-auditor's Successor Enforcement: search T1 for `rdr-planning-context` tag and verify the RDR ID in the tag matches the RDR ID in the current relay — if both match, relay to `plan-enricher`; if tag absent or ID mismatch (standalone audit), relay to `developer`/`architect-planner` (existing behavior)
- plan-enricher has no mandatory successor — it reports to the user

**Skill and command:** `/enrich-plan` — can also be invoked standalone within the same session where `/plan-audit` was run (T1 scratch is session-scoped, so audit findings from the current session are available; cross-session standalone use requires the user to re-run `/plan-audit` first).

### Modified `/rdr-accept` flow

After the existing accept steps (verify gate, update T2, update file, regenerate README), add:

**Step 6 (new): Planning handoff prompt**

Note: Step 2 (T2 `status: accepted` write) must complete before Step 6 to satisfy RDR-024 Guardrail 2 (strategic-planner pre-check). This ordering is natural since Step 2 precedes Step 6 in the accept sequence. The accept-to-planner path intentionally bypasses RDR-024 Guardrail 1 (brainstorming-gate) since the RDR has already passed the gate.

Ask: "Invoke strategic planner to build execution beads? (y/n)"

Auto-detection heuristic for the prompt default (phase count only — F-10):
- If the RDR has an Implementation Plan with 2+ phases → default yes
- If the RDR has 0-1 phases or no Implementation Plan section → default no

**If yes:**
1. Dispatch `strategic-planner` agent with the full RDR content as input
2. Strategic planner creates epic + phase beads with dependencies
3. Strategic planner relays to `plan-auditor` (existing successor enforcement)
4. Plan-auditor validates and relays to `plan-enricher` (new successor)
5. Plan-enricher enriches every bead with audit findings → beads are execution-ready
6. Plan-enricher writes epic bead ID to T2 for close-time advisory (F-12, Gate S-2)

**If no:**
Continue as before — no beads created at accept time.

### Modified `/rdr-close` flow

Remove Step 3 (bead decomposition) from the close skill. Close becomes purely a state transition + archival:

1. Divergence notes (if any)
2. Post-mortem (if diverged)
3. ~~Decompose into beads~~ → removed
4. Update state (T2, file, README)
5. T3 archive (post-mortem only)

**Bead status advisory:** If T2 has an `epic_bead` field (set at accept time), close reads it and runs `bd show <epic-id>` to report child bead statuses. The human decides what to close; `/rdr-close` does not automatically mark beads complete. If no `epic_bead` exists (user skipped planning), close skips the advisory.

Update the close skill's frontmatter description to remove "bead decomposition" and its Success Criteria to remove the bead-creation checkbox and add the bead-status advisory checkbox.

## Alternatives Considered

### A1: Move planning to a separate `/rdr-plan` command
- **Pro**: Clean separation of concerns
- **Con**: Adds yet another command to the lifecycle; users already struggle to remember the sequence
- **Rejected**: The natural moment is right after accept — making it a sub-step is more ergonomic

### A2: Always invoke planning on accept (no prompt)
- **Pro**: Simpler logic
- **Con**: Wastes time on trivial RDRs (single-line fixes, doc-only changes)
- **Rejected**: The complexity threshold matters; ceremony should scale with risk

### A3: Keep bead decomposition in close, add planning to accept
- **Pro**: Belt and suspenders
- **Con**: Creates duplicate beads — planner beads at accept time, decomposition beads at close time
- **Rejected**: Confusing and redundant

### A4: Add Phase 4 (enrichment) to strategic planner instead of new agent
- **Pro**: No new agent to maintain
- **Con**: Architecturally impossible — plan-auditor relays to developer/architect-planner, not back to planner. The relay chain is one-directional (F-09). Making auditor a blocking sub-agent call breaks the existing relay pattern for all non-RDR uses of strategic-planner.
- **Rejected**: Cannot solve the feedback loop without breaking existing agent contracts

## Trade-offs

| Aspect | Before | After |
|--------|--------|-------|
| When beads are created | Close (retroactive) | Accept (actionable) |
| Bead quality | Shallow text extraction | Strategic planner + auditor + plan-enricher |
| Planning ceremony | Manual, separate invocations | Integrated prompt on accept |
| Simple RDRs | Same ceremony as complex | Skip planning with one keystroke |
| Close complexity | 5 steps | 4 steps (simpler) |

## Implementation Plan

### Phase 1: Create plan-enricher agent
- Create `nx/agents/plan-enricher.md` with:
  - Relay reception: lightweight relay with RDR reference; discovers context via T1 scratch search
  - T1 read pattern: search for audit findings, plan structure, bead IDs from predecessors
  - Bead enrichment logic: read each bead, update with audit findings, ensure self-contained
  - T1 write: epic bead ID and enrichment summary (for accept skill to persist to T2)
  - No mandatory successor — reports to user
  - Model: sonnet (structured enrichment, not deep reasoning)
- Create `/enrich-plan` skill (`nx/skills/enrich-plan/SKILL.md`)
- Register agent and skill in `nx/registry.yaml`

### Phase 2: Wire plan-auditor successor
- Update `nx/agents/plan-auditor.md` Successor Enforcement:
  - Search T1 for `rdr-planning-context` tag (written by accept skill) and verify RDR ID matches current relay — if both match, relay to `plan-enricher`
  - If tag absent or RDR ID mismatch (standalone audit) → relay to `developer`/`architect-planner` (existing behavior)
- Remove aspirational "iterate based on audit feedback" from strategic planner Phase 3

### Phase 3: Update `/rdr-accept` skill
- Add planning handoff prompt after existing Step 5
- Implement auto-detection heuristic (phase count only: ≥ 2 → yes, ≤ 1 → no)
- Write T1 scratch entry tagged `rdr-planning-context` with RDR ID and planning metadata before dispatching strategic-planner (F-14)
- Add strategic-planner relay template (auto-populated from RDR content)
- Ensure T2 `status: accepted` write (Step 2) precedes planner dispatch (Step 6) for RDR-024 compatibility
- Epic bead ID written to T2 by plan-enricher at chain end (Gate S-2)

### Phase 4: Update `/rdr-close` skill
- Remove Step 3 (bead decomposition)
- Add bead status advisory: read `epic_bead` from T2, walk dependencies via `bd show`, report statuses
- Update frontmatter description to remove "bead decomposition"
- Update Success Criteria to remove bead-creation checkbox, add bead-status advisory checkbox
- Skip advisory if no `epic_bead` in T2 (user skipped planning)
- Update `nx/registry.yaml` rdr-close entry to remove "bead decomposition" from description

### Phase 5: Update workflow documentation
- Update `docs/rdr-workflow.md` state machine and Accept/Close sections
- Update accept skill description to reflect agent delegation
- Update close skill to reflect simplified flow
- Add plan-enricher to agent documentation

## Success Criteria

- [ ] Plan-enricher agent created with relay reception and bead enrichment logic
- [ ] `/enrich-plan` skill and command registered
- [ ] Plan-auditor relays to plan-enricher when T1 `rdr-planning-context` tag present
- [ ] Strategic planner Phase 3 "iterate based on audit feedback" instruction removed
- [ ] `/rdr-accept` prompts for planning after acceptance
- [ ] Auto-detection heuristic defaults based on phase count (≥ 2 yes, ≤ 1 no)
- [ ] T2 `status: accepted` precedes planner dispatch (RDR-024 compatibility)
- [ ] Epic bead ID stored in T2 for close-time advisory
- [ ] Plan-enricher enriches every bead with audit findings → execution-ready
- [ ] `/rdr-close` reports bead status as advisory, does not auto-close beads
- [ ] `/rdr-close` frontmatter and success criteria updated
- [ ] Simple RDRs can skip planning with one keystroke
- [ ] Workflow documentation reflects the new flow

## Finalization Gate

- [ ] Structural: all sections filled
- [ ] Assumption audit: no unverified assumptions in load-bearing positions
- [ ] AI critique: substantive-critic review
