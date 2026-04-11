---
id: RDR-069
title: "Evidence-Chain Gate Beads"
type: process
status: draft
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-10
accepted_date:
related_issues: ["RDR-065"]
---

# RDR-069: Evidence-Chain Gate Beads

> Stub. Created in response to the substantive-critique gate round 0
> finding that RDR-065 had "indefinitely deferred" INT-6 with no
> tracking artifact — exactly the parking-lot pattern RDR-065 claims
> to fix. This stub converts the deferral into a named, tracked,
> drift-conditioned work item so the failure mode is not applied to
> the fix itself.

## Problem Statement

ART's INT-6 is the highest-payoff structural intervention against the
silent scope reduction failure mode. Its mechanism: "gate beads close
only by a specific verification artifact (test output hash, file
content hash, agent attestation signed against a recorded prompt)."
INT-6 decouples the agent's self-report of gate passage from the
evidence that the gate was actually satisfied, which is the structural
fix for ART's RC-4 (gate assertions ≠ delivery verification) and RC-6
(close reason chosen from recent-context bias).

Without INT-6, RDR-065's close-time interventions (INT-4 problem-
statement replay, INT-7 divergence-language honesty hook, INT-5
follow-up commitment metadata) are only as strong as agent honesty
under composition pressure. The replay requires the agent to provide a
`file:line` pointer; the honesty hook requires the user to recognize
gamed post-mortems; the bead commitment requires the agent to name
fields it can dodge with a subagent dispatch. Each gate is a forcing
function that creates an auditable record, but none of them
independently verify that the work was done. INT-6 would add the
independent verification.

RDR-065 initially deferred INT-6 "indefinitely" with the rationale
"evidence-chain definition is unbounded." The substantive-critique
gate round 0 found this claim was unsupported and exhibited exactly
the parking-lot pattern the wedge was built to eliminate. This stub is
the minimum meta-recursion fix: convert the indefinite deferral to a
named, tracked work item with an explicit drift condition.

### The evidence-chain definition is NOT unbounded

ART's original INT-6 description provides a concrete starting point:

> "[gate beads close only by a] specific verification artifact (test
> output hash, file content hash, agent attestation signed against a
> recorded prompt)."

These are concrete, tractable artifact types:

- **Test output hash**: run the gate's acceptance test, hash the
  output bytes, store the hash in the gate bead's close metadata.
  Re-runs produce the same hash if the gate is truly passed.
- **File content hash**: for gates that assert the presence of
  specific code, hash the file content at the anchored `file:line`.
- **Agent attestation against recorded prompt**: the agent runs a
  structured prompt ("does file X at line Y contain code Z?") and
  the attestation is logged with the prompt used. The attestation is
  not independent of agent honesty, but recording the prompt
  constrains the agent to a specific question.

The definition space is neither infinite nor exotic. A Phase 1 design
pass would produce a workable taxonomy of evidence artifact types in
a week or two. The "unbounded" claim was scope avoidance, not scope
analysis.

### Enumerated gaps to close

#### Gap 1: No gate-bead concept exists in the nexus RDR workflow

Today, RDR acceptance gates are prose checks in the Finalization Gate
section of each RDR (see `nx/resources/rdr/TEMPLATE.md` lines 243-294).
The gates are advisory: the agent reads them, asserts they pass, and
the close skill trusts the assertion. There is no concept of "gate
bead" — a first-class trackable artifact whose close requires an
evidence artifact rather than an agent assertion.

#### Gap 2: No evidence-chain taxonomy or storage mechanism

Even with a gate-bead concept, there is no defined set of evidence
artifact types (test output hash, file content hash, attestation),
no mechanism for storing them (bead metadata? T2 entry? T3 archive?),
and no way to re-verify them after the fact (does the hash still
match the file?). Phase 1 of this RDR would produce the taxonomy.

#### Gap 3: No integration with `nx:rdr-accept` or `nx:rdr-close`

Gate beads must be auto-generated during `rdr-accept` (one bead per
acceptance gate in the RDR's Finalization Gate section) and must
block the close until each has an evidence artifact attached. Neither
integration exists today.

## Context

### Background

This RDR is one of the sibling RDRs responding to the ART-filed
"silent scope reduction under composition pressure" failure mode:

- **RDR-065** (close-time funnel): INT-4, INT-7, INT-5 wrapper,
  template change. Primary active wedge.
- **RDR-066** (enrichment-time): INT-1 contracts + INT-2 composition
  smoke probe + coordinator-bead concept. Stub, deferred.
- **RDR-067** (cross-project observability): 5 metrics + `rdr_process`
  collection + `nx:rdr-audit` skill. Stub, deferred.
- **RDR-068** (composition failure detection): INT-3 research for
  regex bank. Stub, deferred.
- **RDR-069** (this RDR, evidence-chain gate beads): INT-6. Stub,
  deferred — but named and tracked, not orphaned.

### Why deferred

INT-6 is higher-effort than RDR-065's close-time interventions. It
requires a new concept (gate beads), a new taxonomy (evidence
artifacts), and integration with two skills (`rdr-accept`, `rdr-close`).
Phase 1 alone is probably 2-3 days of design work, and Phase 2
implementation would add several more. It is genuinely too large to
bundle into RDR-065.

The honest framing: RDR-065 ships the cheap-and-measurable
interventions first, produces data on whether agent honesty under
pressure is the dominant failure path or whether the gates are
sufficient, and then this RDR is prioritized based on that evidence.
If RDR-065's gates fire but get gamed, INT-6 is urgent. If they fire
and produce real signal, INT-6 can wait longer.

### Drift condition

**If RDR-069 has not moved from `draft` to `accepted` within 90 days
of RDR-065 closing as `implemented`, re-open RDR-065 and re-evaluate
its `close_reason`.** This is the explicit anti-parking-lot commit.
The drift condition exists because INT-6 is the structural fix that
makes RDR-065's interventions robust rather than advisory-in-disguise;
leaving INT-6 unstarted indefinitely means RDR-065's close claim is
based on incomplete foundations.

If RDR-065's first 6 months of operation reveal evidence that the
gates are being gamed (e.g., agents providing plausible-but-wrong
`file:line` pointers that user review later finds inaccurate), this
RDR's priority should escalate from P2 to P1 and the drift condition
should trigger immediately rather than at 90 days.

### Technical Environment

- **`nx:rdr-accept`** (`nx/skills/rdr-accept/`): the skill that
  transitions RDRs from draft to accepted. Candidate insertion point
  for gate-bead auto-generation.
- **`nx:rdr-close`** (`nx/skills/rdr-close/` + `nx/commands/rdr-close.md`):
  the close skill RDR-065 is also modifying. Must not regress RDR-065's
  changes.
- **Beads (`bd`)**: external task tracker. Gate beads would be regular
  beads with a `gate_of: RDR-NNN` field in description or tags. We
  cannot modify `bd` schema directly.
- **T2 memory**: could store evidence artifacts with the RDR's metadata
  record.
- **T3 storage**: could archive larger evidence artifacts (full test
  output files, diff hashes with source context).

## Research Findings

[Pending. Phase 1 = design a workable evidence-chain taxonomy.]

### Investigation plan (Phase 1)

1. **Survey existing gate structures.** Read the Finalization Gate
   section of 5 recent nexus RDRs (RDR-060 through RDR-064) plus the
   TEMPLATE.md. Enumerate the types of assertions gates make:
   contradiction checks, assumption verification, API verification,
   scope verification, cross-cutting concern coverage. Classify by
   whether each can plausibly be backed by a mechanical evidence
   artifact.
2. **Draft the evidence-artifact taxonomy.** For each assertion type,
   propose a concrete artifact type. Examples:
   - "API X verified" → artifact is the URL of the source file
     searched + content hash of the relevant function + attestation
     prompt + agent response.
   - "Assumption Y verified by spike" → artifact is the spike
     script, its output, and the exit code.
   - "Scenario Z tested" → artifact is the pytest test name + run
     output hash + pass/fail.
3. **Measure feasibility.** For each artifact type, estimate the
   implementation cost: storage bytes per artifact, computation cost
   per verification, agent tooling required.
4. **Decide the minimum viable subset.** Probably 2-3 artifact types
   that cover 80% of gate assertions. The rest can fall back to
   "agent attestation against recorded prompt" as a last resort.

### Critical Assumptions

- [ ] **CA-1**: An evidence-chain taxonomy exists that covers ≥80% of
  the assertion types in the Finalization Gate section of the nexus
  RDR template. If not, INT-6 may need to be narrower than ART
  described.
  — **Status**: Unverified — **Method**: Phase 1 survey
- [ ] **CA-2**: Gate beads can be auto-generated during `rdr-accept`
  without modifying the bead schema. (We cannot modify `bd` internals.)
  — **Status**: Unverified — **Method**: Design spike
- [ ] **CA-3**: Re-verification of evidence artifacts is cheap enough
  to run at close time and on a cron (for aging verification). If
  re-verification costs minutes per artifact, the approach is too
  expensive.
  — **Status**: Unverified — **Method**: Phase 1 cost estimation
- [ ] **CA-4**: Agent attestation against recorded prompts is a
  meaningful weaker fallback, not just self-report. Unclear whether
  the recording constrains the agent enough to be worth the overhead
  vs. accepting plain self-report.
  — **Status**: Unverified — **Method**: Phase 1 design analysis

## Proposed Solution

[Pending. Phase 1 deliverable is the taxonomy; Phase 2 implementation
is gated on the taxonomy being workable.]

### Sketched approach

1. **Phase 1 design pass** (~2-3 days): produce the evidence-artifact
   taxonomy, feasibility measurement, and minimum viable subset.
   Phase 1 output is a design doc appended to this RDR.
2. **Phase 2 implementation** (~3-5 days, conditional on Phase 1):
   modify `nx:rdr-accept` to auto-generate one gate bead per
   acceptance gate in the RDR's Finalization Gate section; modify
   `nx:rdr-close` to read gate-bead statuses and block on unclosed
   ones; build the evidence-artifact storage (T2 record field or
   new T3 collection).
3. **Phase 3 backfill** (~1 day, conditional on Phase 2): for
   recently-closed RDRs (post-RDR-065), generate retrospective gate
   beads and score their evidence against the new taxonomy. This
   measures whether the existing RDRs have verifiable gates or
   whether the pattern has already shipped.

## Alternatives Considered

### Alternative 1: Accept the status quo — gates are advisory, period.

**Description**: do nothing. Rely on RDR-065's close-time interventions
and trust agent honesty for the rest.

**Pros**: zero implementation cost.

**Cons**: leaves the structural fix for RC-4 and RC-6 unaddressed.
RDR-065's gates remain advisory-in-disguise. The failure mode persists
for any agent motivated or pressured to game them.

**Reason for rejection**: ART explicitly called INT-6 "higher effort,
but the payoff is very high." The rejection is tempting because the
cost is real and the payoff is structural-not-visible, but accepting
the status quo is exactly the pattern the fix is supposed to eliminate.

### Alternative 2: Attestation-only (weaker subset)

**Description**: implement only the "agent attestation against
recorded prompt" evidence type, skip test output and file content
hashes.

**Pros**: low implementation cost, ~1 day of work. Catches some
gaming where the recorded prompt constrains the agent.

**Cons**: still relies on agent honesty — the attestation is the
agent's self-report, just with a recorded prompt. Not a meaningful
structural decoupling from RC-4/RC-6.

**Reason for tentative acceptance as fallback**: if Phase 1 reveals
that test output and file content hashes are impractical for most
gate types, attestation-only is better than nothing. Should be
explicitly documented as a weaker subset, not silently adopted.

## Trade-offs

[Pending Phase 1.]

## Implementation Plan

[Pending. Phase 1 is the current scope: design the taxonomy.]

## References

- ART canonical writeup (INT-6 description):
  `~/git/ART/docs/rdr/meta/RDR-PROCESS-FAILURE-MODE-silent-scope-reduction.md`
- T2 cross-project: `rdr_process/failure-mode-silent-scope-reduction`
- RDR-065 (close-time funnel — this RDR unblocks its structural
  strength)
- RDR-066, RDR-067, RDR-068 (other sibling RDRs)
- Existing gate structures: `nx/resources/rdr/TEMPLATE.md` §Finalization
  Gate (lines 243-294)
- Skills to modify (Phase 2): `nx/skills/rdr-accept/`,
  `nx/skills/rdr-close/`, `nx/commands/rdr-close.md`

## Revision History

- 2026-04-10 — Stub created in response to RDR-065 substantive-critique
  gate round 0 Critical Finding 3. Previous deferral of INT-6 was
  "indefinitely deferred with no artifact" — that status is retracted
  and replaced with this tracked stub. Drift condition: if RDR-069 has
  not reached `accepted` within 90 days of RDR-065 closing, re-open
  RDR-065 and re-evaluate its `close_reason`.
