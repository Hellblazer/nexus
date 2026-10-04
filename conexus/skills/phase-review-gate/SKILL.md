---
name: phase-review-gate
description: Use when a phase boundary is being closed — cross-walk RDR §Approach against closing beads to catch silent scope reduction before it is discovered mid-implementation
effort: low
---

# Phase Review Gate Skill

A manual checklist for the phase boundary. Before a phase is declared closed, every numbered item in the RDR's phase structure (§Approach, or the `### Phase N` / `#### Step N` headings under §Implementation Plan) has an evidence pointer: a closing bead ID, or an explicit deferral.

**Root cause it prevents**: silent scope reduction found mid-implementation. RDR-112 Phase 1 (nexus-52lb, 2026-05-15) shipped T2-only work and §Approach item 2 (T3 daemon) was dropped without a word. A bead acceptance criterion (`mcp_infra.get_t3() -> T3Client`) could not compile three closed phases later. Cost: 2-3 days of replanning. The cross-walk takes about 15 minutes.

## When to Use

- A phase-review bead (title contains "P{N}.review" or "Phase {N} gate") is about to close.
- The implementation plan changed during the phase, or a phase was paused and resumed weeks later.

## Steps

1. Read the RDR's phase structure and list every numbered item for the phase being closed.
2. For each item, find the closing bead (`bd show <id>`) whose acceptance criteria cover it. Write `Item N = <bead-id>`.
3. An item with no closing bead is either unfinished or deferred. Finish it, or write `Item N = none` with a one-line reason (for example "T1 stays put, no work needed").
4. Do not close the phase while an item has no pointer and no stated deferral.

Nothing enforces this. No command checks the list and nothing blocks the close. The reviewer is accountable for every `none`.

## Limits

- It checks coverage, not correctness: a bead pointer passes even when that bead's acceptance criteria are weak. Open the evidence and read it.
- It does not check that code paths between accounted-for items work together. RDR-112 Phase 3 (2026-05-18) covered every item and still shipped a production regression, caught only by a second code review that named boundary-spanning suspect categories. Where the phase ships integration-seam code (process spawning, supervisor interaction, RPC wiring, env-var resolution), also dispatch `code-review-expert` with the suspect categories from `/conexus:code-review` § Prompt rigour.

## Relationship to Other Gates

| Gate | Scope | When |
|------|-------|------|
| `/conexus:rdr-gate` | Whole RDR structure, assumptions, AI critique | Before RDR acceptance |
| `/conexus:rdr-close` | Status flip, post-mortem, T3 archival | At RDR close |
| this checklist | Phase items against closing beads | At each phase boundary |
