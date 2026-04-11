---
id: RDR-068
title: "Dimensional Contracts at Enrichment"
type: process
status: draft
priority: P3
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-10
reissued: 2026-04-11
accepted_date:
related_issues: ["RDR-065", "RDR-066", "RDR-067", "RDR-069"]
supersedes_scope: "Composition Failure Detection (Research) (original 2026-04-10 scope proposed a regex bank + precision measurement for INT-3)"
---

# RDR-068: Dimensional Contracts at Enrichment

> **Reissued 2026-04-11.** This RDR replaces the original 2026-04-10
> scope ("Composition Failure Detection — Research") which proposed
> mining ART incidents to build a regex bank for detecting composition
> failures in test output, measured for precision, to enable INT-3
> (workaround gating). The nexus audit (`rdr_process/nexus-audit-2026-04-11`)
> made the regex-bank research **unnecessary** in two ways:
>
> 1. LLM classification via `deep-research-synthesizer` beats regex
>    banks at this task. The audit proved it — it classified 4 incidents
>    from 20 post-mortems accurately with no false positives, using
>    reasoning, not patterns.
> 2. INT-3 (workaround gating) depends on detecting that the agent is
>    ABOUT to commit a workaround. The RDR-073 live transcript shows
>    the retcon mechanism: the agent doesn't experience itself as
>    committing a workaround — it experiences itself as fixing a plan
>    error. Detection of an act the actor doesn't recognize is much
>    harder than the stub assumed, and is deferred.
>
> **The RDR-068 number is repurposed** for a different intervention:
> **dimensional contracts at enrichment time** (ART's INT-1). This is
> the belt-and-suspenders layer on top of RDR-066's composition probe.
> It catches the dim-mismatch sub-class of failures at plan time, before
> any code is written. The audit shows this catches 1/4 incidents cleanly
> (RDR-073) and provides cheap additional coverage for the other 3 when
> the probe is also running.

## Problem Statement

The silent-scope-reduction failure mode (canonical: `~/git/ART/docs/rdr/meta/RDR-PROCESS-FAILURE-MODE-silent-scope-reduction.md`) has a specific sub-pattern: dimensional mismatch between composed components. RDR-073 is the canonical example — a 312D phonemic vector fed into a 65D Binder-trained grounding layer produced `IllegalArgumentException: input length 312 != state size 65` at the final integration bead. Five minutes of dimensional analysis during IMPL-04 enrichment would have surfaced this: *"what dimension does `SemanticGroundingLayer.process` expect? what dimension does `PhonemicWordPipeline.lastProcessedWordVector()` produce?"* The enrichment didn't ask those questions because the enrichment template doesn't require them.

**Today's problem**: plan-enricher produces structural enrichment (files, classes, methods, acceptance criteria) without dimensional contracts. Every enriched bead describes WHAT classes and methods exist without describing WHAT SHAPE data flows through them. The dimensional question is exactly the question that would catch this class of failure in 5 minutes, before a single line of code is written, and it isn't asked because the template doesn't force it.

This RDR is **belt-and-suspenders** on top of RDR-066's composition smoke probe. The probe catches 4/4 audit incidents including dim mismatches. Dimensional contracts catch 1/4 cleanly (the dim mismatch sub-class) and provide a cheap additional layer that surfaces the error earlier — at plan time, not at coordinator-bead probe time.

### Enumerated gaps to close

#### Gap 1: No `## Contracts` section in the enrichment template

`nx/skills/enrich-plan/SKILL.md` does not prompt plan-enricher to populate per-method dimensional contracts. Enriched beads have file paths and method names but no shape declarations. The gap is in the template — the agent will produce whatever the template asks for.

#### Gap 2: Plan-enricher agent doesn't populate contracts

Even with a template update, the plan-enricher agent prompt must be extended to walk each bead's named methods and produce the contracts block. The agent capability exists (CA-2 spike verified, prior iteration) — the prompt extension is the work.

#### Gap 3: No hard-case verification (cross-file generics, protocols, third-party types)

The CA-2 spike from the prior iteration verified the easy case: self-contained function with plain types (`Path`, `float`, `int`). The hard case — cross-file generics (`Pipeline<T>`), protocols, third-party library types (`numpy.ndarray`, `chromadb.Collection`) — is unverified. Read alone may not suffice; Serena/symbol-lookup may be required for some cases. Phase 1 of this RDR must spike the hard case to find the Read-to-Serena boundary before the template is locked.

## Context

### Background

This RDR is **Phase 3** of the four-RDR silent-scope-reduction remediation. Phases 0 (RDR-069 critic), 1 (RDR-066 probe), and 2 (RDR-067 audit loop) ship first. Phase 3 is the lowest-priority preventive layer because:
- The probe (Phase 1) catches a superset of what contracts catch (runtime > static)
- The critic (Phase 0) catches what the probe misses
- Contracts add a THIRD layer that catches dim-mismatches at plan time, before any code runs — the earliest possible catch
- But the value over Phases 0-1 is marginal: contracts catch a specific sub-pattern (1/4 of audit incidents), while Phase 1 + Phase 0 together catch all 4

Contracts belong in the plan, not as the first layer of defense. Ship them after the proven interventions are in place.

### Why this supersedes the original scope

The original RDR-068 (2026-04-10) was a research RDR targeting INT-3 (mid-session workaround gating). Three gaps:
1. No vocabulary for "composition failure"
2. No regex bank or pattern classifier
3. No mechanism for measuring detector precision

The 2026-04-11 audit makes all three obsolete:

- **Gap 1 (vocabulary)**: FILLED by the audit. The vocabulary is `unwiring` (building blocks shipped, production not wired — RDR-031, RDR-036, RDR-075), `dim mismatch` (shape incompatibility between composed components — RDR-073), and `deferred integration` (in-scope step punted to follow-on — RDR-031). These are the real categories. The regex bank is implicit in the vocabulary but unnecessary.

- **Gap 2 (regex bank)**: OBSOLETE. The audit subagent classified 4 incidents accurately using LLM reasoning, not patterns. Regex banks are useful for real-time gating (where a subagent call is too expensive) but INT-3 requires detecting the retcon — which is cognitive, not pattern-matchable in test output. RDR-073 line 529 shows the agent framed the workaround as a plan fix, not as an error. No regex on test output would have caught that.

- **Gap 3 (precision measurement)**: MOOT. Only relevant if we ship real-time gating. INT-3 is deferred indefinitely (see "What gets abandoned" below).

The research target (a regex bank for detecting composition failures in test output) produced no deliverable because the research question was superseded by a cheaper proven mechanism (LLM classification at audit time + the substantive-critic at close time).

**Abandoning INT-3 real-time gating**: the retcon mechanism (documented in RDR-073 session transcript lines 526-529) shows the detection problem is harder than ART's writeup anticipated. The agent doesn't recognize the workaround AS a workaround. Detection at the moment of agent action would require either user-in-the-loop intercepts (friction-heavy) or an independent critic observing the session (essentially: run the substantive-critic mid-session, which is expensive and speculative). Neither is worth building at current evidence.

**The RDR-068 number is repurposed**. Since this RDR stays in the nexus RDR series, reissuing it with a different scope preserves the numbering while acknowledging the research goal was met by a different mechanism. The new scope is ART's INT-1 (dimensional contracts at enrichment) — the belt-and-suspenders layer.

### Technical Environment

- **`nx:enrich-plan`** (`nx/skills/enrich-plan/SKILL.md`) — the skill that drives enrichment
- **`plan-enricher` agent** (`nx/agents/plan-enricher.md`) — the agent to extend
- **CA-2 spike findings** (from the prior iteration, still valid) — easy-case verification that a Sonnet subagent can produce grounded contracts from Read alone
- **Template refinements** surfaced by the CA-2 spike:
  1. Split "Error modes" into `caught` / `propagates` sub-bullets
  2. Inline default constant values (`_DEFAULT_DECAY_RATE` → `0.01`, not symbolic)
  3. Keep "Tools used to ground" + "Hallucination self-check" fields

## Research Findings

### Finding 1 (2026-04-11): CA-2 easy case still holds — retained from prior iteration

Source: `nexus_rdr/066-research-2-ca2-spike-verified` (written during the prior iteration; retained because the finding is still valid under the new scope).

A Sonnet-class subagent produced a fully line-grounded 11-field contract for `src/nexus/frecency.py:compute_frecency` (132-line self-contained Python function) from a single `Read()` call. HIGH confidence on all fields except "propagating exceptions" (correctly self-flagged as lowest confidence — inferred from absence-of-handler).

Template refinements surfaced by that spike (applied here):
1. Split "Error modes" into sub-bullets: **caught** (with handler citation) and **propagates** (uncaught, propagates to caller)
2. Add "Default values resolved" field — inline constant values not just symbolic references
3. Retain "Tools used to ground" and "Hallucination self-check" fields for auditability

### Finding 2 (2026-04-11, from the audit): 1/4 incidents cleanly caught by dimensional contracts

Source: `rdr_process/nexus-audit-2026-04-11`.

Of the 4 confirmed ART incidents:

- **RDR-073** (312D/65D dim mismatch): A dimensional contract on `SemanticGroundingLayer.process` (input_shape: `(batch, DEFAULT_SEM_DIM=65)`) and on `PhonemicWordPipeline.lastProcessedWordVector()` (output_shape: `(312,)`) would have surfaced the mismatch at enrichment time. ✓ Caught cleanly.
- **RDR-075** (InstarLearning structurally dead): The classes and method signatures were correct. The contract would say `InstarLearning.apply(state) -> void` and be satisfied — it IS applied, just in the wrong factory. ✗ Not caught by text contracts.
- **RDR-036** (HashMap short-circuit): The `FactualTeacher.query` method signature is `String -> String`. The contract is satisfied — it returns a string. The failure is that a HashMap lookup short-circuits the neural path; contracts don't express "these components should all be reached." ✗ Not caught by text contracts.
- **RDR-031** (building blocks only, pipeline not swapped): Same pattern — the contracts on individual methods are correct; the integration point (the place where the pipeline should have swapped) is below the contract level. ✗ Not caught by text contracts.

**Contracts catch 1/4 cleanly, probe catches 4/4.** Contracts are a belt-and-suspenders layer, not a primary intervention. Priority is therefore P3.

### Critical Assumptions

- [ ] **CA-1** (hard case): LLM can produce grounded dimensional contracts for cross-file generics, protocols, and third-party library types without requiring runtime symbol resolution. The easy case is verified (Finding 1); the hard case is not.
  — **Status**: Unverified — **Method**: Spike on a real hard-case target (e.g., a nexus class composing `llama_index_core.schema.TextNode` with `chromadb.Collection` and a generic return type)

- [ ] **CA-2**: The contracts surfacing the dim-mismatch pattern are discoverable in a cross-bead comparison — i.e., a tool (or plan-auditor agent, or human reader) can look at bead 4's contract and bead 5's contract and see the mismatch without being told where to look. If contracts are only comprehensible in isolation, they don't help.
  — **Status**: Unverified — **Method**: retrospective test against RDR-073 — would a reader of IMPL-04 enrichment + IMPL-05 enrichment with contracts have noticed the 312D/65D mismatch?

- [ ] **CA-3**: Contracts add an acceptable amount of enrichment time. The CA-2 easy-case spike took ~50 seconds for a 132-line function with a single Read call. Hard case is probably 2-5x. For a 20-bead plan with 5-10 contracts per bead, enrichment latency could climb significantly.
  — **Status**: Unverified — **Method**: Phase 1 hard-case spike; measure latency

## Proposed Solution

### Approach

Extend `nx/skills/enrich-plan/SKILL.md` to require a `## Contracts` section per enriched bead that names methods the bead touches. Update `nx/agents/plan-enricher.md` prompt to populate the section using the 11-field template (refined per Finding 1).

### Technical Design

**Contracts template per method** (one block per named method):

```markdown
### Contract — <module>.<method_name>

**Signature** (file:line): `<verbatim def/method line>`

**Inputs**:
- `<param>` (`<type>`, <kind>, default=<resolved-value or REQUIRED>)
  - Semantic: <one sentence>
  - Shape constraint: <runtime shape or "none">
  - Grounded by: <Read file:line | Serena find_symbol | ...>

**Output**:
- Type: `<verbatim annotation>`
- Semantic: <one sentence>
- Shape constraint: <e.g., "non-negative float", "list of 65 floats", "dict with keys X,Y,Z">
- Grounded by: <tool reference>

**Preconditions** (caller invariants):
- <bulleted, each grounded in a specific line>

**Postconditions**:
- <bulleted, grounded>

**Side effects**:
- <I/O, subprocess, state mutation; "none observed" if verified>

**Error modes**:
- **Caught**: <exceptions caught internally; handler file:line>
- **Propagates**: <exceptions propagating to caller; inferred if no handler>

**Calls out to**: <other methods/modules invoked; cite line numbers>

**Tools used to ground**: <Read/Grep/Serena calls>

**Hallucination self-check**: <which fields, if any, less than 100% confident>
```

**Plan-enricher prompt update**: for each bead in the plan, after populating file paths and method names, walk each method and produce the contracts block using the template. If the method's contracts conflict with an earlier bead's contracts (e.g., bead 5 expects shape X but bead 3's contract says it produces shape Y), emit a `CONTRACT MISMATCH` warning in the enrichment output. The warning blocks enrichment completion until resolved.

**Mismatch detection (CA-2)**: during enrichment, after all beads have contracts, plan-enricher walks the contracts and produces a cross-bead summary: "Bead 3 produces `list[ChunkResult]`; Bead 7 expects `dict[str, ChunkResult]`; mismatch." This is the payoff — the contracts become useful when a comparison surfaces the mismatch.

### Alternatives Considered

**Alternative 1: No contracts, rely on probe + critic**

Ship RDR-066 (probe) + RDR-069 (critic) without RDR-068 (contracts).

**Rejection**: catches 4/4 incidents at runtime / close-time but misses the opportunity to catch 1/4 at plan time. The cost of contracts is low (template extension + prompt update) and the benefit is the EARLIEST possible catch for dim mismatches. Worth shipping as the lowest-priority layer.

**Alternative 2: Informal prose contracts instead of structured template**

Let plan-enricher write contracts in free prose rather than a 11-field template.

**Rejection**: free prose is not grep-able for mismatch detection. CA-2 (cross-bead mismatch surfacing) requires structure.

**Alternative 3: Contracts only for coordinator beads (align with RDR-066)**

Only populate contracts on beads tagged `metadata.coordinator=true`.

**Rejection**: misses the plan-time catch. The value of contracts is surfacing mismatches BEFORE the coordinator runs — which means contracts must exist on the dependency beads whose outputs the coordinator composes. Scoping to coordinators only means the contracts exist but can't be cross-checked.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| Enrichment template | `nx/skills/enrich-plan/SKILL.md` | **Extend** — add contracts section |
| Plan-enricher agent | `nx/agents/plan-enricher.md` | **Extend** — add contracts walk + mismatch detection |
| Mismatch detection | N/A | **New** — inline in plan-enricher prompt |
| Contracts template | N/A | **New** — document at `nx/resources/rdr/CONTRACTS-TEMPLATE.md` |

### Decision Rationale

Contracts are the lowest-priority preventive layer because the probe (RDR-066) is a superset. But they're cheap, they catch the dim-mismatch subclass at the earliest possible point, and they layer cleanly on top of the other interventions. Ship them after Phases 0-2 are in place and working.

## Alternatives Considered

See §Proposed Solution §Alternatives Considered.

### Briefly Rejected

- **Contracts only when CA-1 hard case is verified**: we can ship easy-case contracts now and add hard-case support later; no reason to block
- **Contracts as acceptance criteria instead of separate section**: acceptance criteria are pass/fail checks; contracts are declarations. Different purpose.

## Trade-offs

### Consequences

- **Positive**: earliest possible catch for dim mismatches (plan time)
- **Positive**: contracts become a cross-bead type-checking mechanism via mismatch detection
- **Positive**: auditable — every contract cites its grounding source
- **Negative**: adds enrichment latency (proportional to bead count and method count); hard cases may be significantly slower
- **Negative**: contracts can become ceremonial if CA-2 (mismatch detection) doesn't work — authors fill in boilerplate, nothing checks, noise accumulates
- **Negative**: covers only 1/4 audit incidents cleanly; redundant with RDR-066 for most of the failure space

### Risks and Mitigations

- **Risk**: Hard case (CA-1) can't be grounded from Read alone and requires Serena. **Mitigation**: Phase 1 spike; if Serena is needed, extend plan-enricher to use it; if Serena is still insufficient, narrow the contracts to plain-type methods only and document the gap.
- **Risk**: Mismatch detection (CA-2) fires too many false positives. **Mitigation**: start mismatches as advisory warnings, not blocking; tune based on real-world FP rate.
- **Risk**: Contracts become boilerplate-only. **Mitigation**: the "Hallucination self-check" field catches agents that fill in without grounding; audit the self-check field distribution over time.

### Failure Modes

- Hard case fails CA-1 → contracts are easy-case-only; documented as a known limitation
- Mismatch detection false positives block enrichment → advisory mode instead; relies on human review
- Contracts drift from reality (method signature changes, contract not updated) → detected by Phase 3 probe (RDR-066) catching a failure the contracts missed; post-mortem classifies as "stale contract" drift

## Implementation Plan

### Prerequisites

- [ ] RDR-066 (Phase 1) shipped — probe is the primary layer; contracts are belt-and-suspenders on top
- [ ] CA-1 (hard case) verified OR explicitly scoped as easy-case-only

### Minimum Viable Validation

Retrospective test against RDR-073: populate contracts for IMPL-04 (GroundedLanguageSystem.dialog) and IMPL-05 (integration test) as they would have been written with the new template. Verify that cross-bead mismatch detection surfaces the 312D/65D gap at enrichment time, before any code is written.

### Phase 1: Hard-case spike

- Pick a real hard-case target (cross-file generic, protocol, or third-party library type)
- Dispatch plan-enricher-class subagent with the contracts template
- Measure: Serena needed? latency? accuracy? is the contract interpretable?
- Outcome: decide Read-only vs. Read+Serena; document CA-1 disposition

### Phase 2: Template + skill update

- Create `nx/resources/rdr/CONTRACTS-TEMPLATE.md` with the 11-field block from Finding 1
- Update `nx/skills/enrich-plan/SKILL.md` to require contracts per enriched bead
- Update `nx/agents/plan-enricher.md` prompt: walk each bead's methods, produce contracts, emit mismatch warnings on cross-bead conflicts

### Phase 3: Mismatch detection

- Extend plan-enricher to walk the aggregate contracts across all beads in a plan
- For each method declared as an output by one bead and as an input by another, compare shapes and surface mismatches
- Start mismatch surface as advisory (warning in enrichment output); upgrade to blocking if false-positive rate is low

### Phase 4: Plugin release + recursive self-validation

- Bump version, reinstall, smoke test
- **6a**: synthetic dim mismatch injected into a test plan; verify plan-enricher catches via mismatch detection
- **6b**: substantive-critic on RDR-068
- **6c**: real self-close of RDR-068 via RDR-069 close flow

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
|---|---|---|---|---|---|
| `nx/resources/rdr/CONTRACTS-TEMPLATE.md` | `ls` | `cat` | `rm` | N/A | git |
| Enrichment output with contracts | `bd show <bead>` | `bd show --json` | N/A | retrospective audit | beads dolt backup |

## Test Plan

- **Scenario 1** (RDR-073 retrospective MVV): populate contracts for IMPL-04/05 as they would have been written; verify mismatch detection surfaces 312D/65D
- **Scenario 2** (clean plan): enrich a plan with no dimensional mismatches; verify no false positives
- **Scenario 3** (CA-1 hard case): cross-file generic / protocol / third-party target; measure grounding quality + latency
- **Scenario 4** (mismatch detection): inject a synthetic mismatch into a plan; verify detection
- **Scenario 5** (recursive 6a): inject a mismatch into a test plan; verify the new plan-enricher catches it

## Validation

### Testing Strategy

The RDR-073 retrospective MVV is the load-bearing test. Phase 1 spike determines whether the hard case is in scope for initial shipment or deferred.

### Performance Expectations

Easy-case contract generation: ~20-50 seconds per method (from CA-2 spike in prior iteration). Hard case: unknown, measured in Phase 1. If enrichment latency exceeds ~5 minutes for a 20-bead plan, narrow scope or cache contracts between beads.

## Finalization Gate

### Contradiction Check

No contradictions. Contracts are a plan-time layer beneath the probe (RDR-066) and the critic (RDR-069); they catch a specific sub-pattern earlier.

### Assumption Verification

CA-1, CA-2, CA-3 verified in Phase 1 spike.

### Scope Verification

MVV is the RDR-073 retrospective. Concrete, measurable.

### Cross-Cutting Concerns

- **Versioning**: plugin release
- **Build tool compatibility**: N/A
- **Licensing**: AGPL-3.0
- **Deployment model**: plugin reinstall
- **IDE compatibility**: N/A
- **Incremental adoption**: contracts are opt-in; easy-case shipped first
- **Secret/credential lifecycle**: N/A
- **Memory management**: contracts live in bead descriptions; no separate store

### Proportionality

Right-sized for a P3 belt-and-suspenders layer.

## References

- `rdr_process/failure-mode-silent-scope-reduction` — ART canonical writeup (INT-1)
- `rdr_process/nexus-audit-2026-04-11` — audit evidence showing 1/4 clean catches
- `nexus_rdr/066-research-2-ca2-spike-verified` — CA-2 easy-case spike (retained from prior iteration)
- `~/git/ART/docs/rdr/post-mortem/073-cogem-training-deployment-and-dialog-runtime-integration.md` — RDR-073 retrospective target for MVV
- `nx/skills/enrich-plan/SKILL.md`, `nx/agents/plan-enricher.md` — pieces to extend
- RDR-066 (Phase 1 — composition probe) — the primary layer this supplements
- RDR-069 (Phase 0 — critic at close) — the backstop

## Revision History

- 2026-04-10 — Stub created as "Composition Failure Detection (Research)" targeting INT-3 (workaround gating) with a regex-bank research goal.
- 2026-04-11 — **Reissued with new scope**. The regex-bank research is obsoleted by the 2026-04-11 nexus audit (LLM classification beats regex for this task) and INT-3 is deferred indefinitely (retcon mechanism makes real-time detection harder than ART assumed). The RDR-068 number is repurposed for ART's INT-1 (dimensional contracts at enrichment) — the cheapest belt-and-suspenders layer on top of RDR-066's composition probe. Priority P3 (lowest of the four) because the probe catches 4/4 while contracts catch 1/4 cleanly. See `rdr_process/nexus-audit-2026-04-11` for evidence and bead `nexus-640` for the 4-RDR cycle.
