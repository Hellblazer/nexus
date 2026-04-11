---
id: RDR-066
title: "Composition Smoke Probe at Coordinator Beads"
type: process
status: draft
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-10
reissued: 2026-04-11
accepted_date:
related_issues: ["RDR-065", "RDR-067", "RDR-068", "RDR-069"]
supersedes_scope: "Enrichment-Time Contract Pre-Flight (original 2026-04-10 scope bundled contracts + probe + coordinator concept)"
---

# RDR-066: Composition Smoke Probe at Coordinator Beads

> **Reissued 2026-04-11.** This RDR replaces the original 2026-04-10
> scope ("Enrichment-Time Contract Pre-Flight") which bundled three gaps
> in the wrong priority order: dimensional contracts + composition probe
> + coordinator-bead concept. The nexus audit (`rdr_process/nexus-audit-2026-04-11`)
> found that of 4 confirmed ART silent-scope-reduction incidents,
> **4/4 would be caught by the composition probe** while only **1/4 would
> be caught cleanly by text-level dimensional contracts** (3 of the 4 are
> "unwiring" failures — building blocks correctly implemented but not
> wired to production — which the probe catches at runtime but static
> contracts cannot). The probe is the high-leverage preventive
> intervention. Dimensional contracts are secondary belt-and-suspenders
> and have moved to RDR-068. The coordinator-bead concept is resolved
> for free via `bd metadata.coordinator=true` (CA-3 from the prior
> iteration, still verified).

## Problem Statement

The silent-scope-reduction failure mode (canonical: `~/git/ART/docs/rdr/meta/RDR-PROCESS-FAILURE-MODE-silent-scope-reduction.md`) has a consistent structural shape: per-bead verification is isolationist (stubs, null collaborators, default constructors). The first time composition is actually exercised against real data is the final integration bead, by which point N-1 beads are already merged. Any failure discovered at that point surfaces at the highest possible sunk cost, and the rational local option is a workaround.

**The audit** (`rdr_process/nexus-audit-2026-04-11`) confirms this is the dominant pattern in real ART incidents. 4 of 4 confirmed incidents involve a coordinator or integration bead that composes earlier work:

| Incident | Coordinator bead | What failed at composition |
|---|---|---|
| RDR-073 | IMPL-04 (GroundedLanguageSystem.dialog) | 312D phonemic vs 65D Binder grounding dim mismatch at runtime |
| RDR-075 | DevelopmentalCurriculumIntegration | InstarLearning had zero production callers; the coordinator used the no-rule factory |
| RDR-036 | FactualTeacher.query | HashMap lookup short-circuited the entire resonance path; maxIterations=1 bypassed the BU/TD loop |
| RDR-031 | Pipeline wiring (Step 5) | Production classes weren't swapped for the Grossberg ODE implementations; building blocks ran in isolation only |

In **every one of these cases**, a 30-50 line end-to-end smoke test against the coordinator's entry point — run AFTER the coordinator bead is enriched and BEFORE the next bead begins — would have surfaced the failure at M+1 instead of N, where M-N is small. The reopen cost would be bounded by the delta.

**Today's problem**: there is no such probe. Plan-enricher produces structural enrichment (files, methods, acceptance criteria) but there is no skill that generates and runs a composition smoke test from a coordinator bead's description. Coordinator beads are not even identified as a class — today they're implicit in the plan graph, not explicit.

### Enumerated gaps to close

#### Gap 1: No "coordinator bead" identification mechanism

A coordinator bead is one whose implementation composes outputs from ≥2 prior beads in the plan. Today the plan graph does not tag these. A probe skill has nothing to fire on. The concept must be expressed via bead metadata (or naming convention) that downstream tools can read.

#### Gap 2: No `nx:composition-probe` skill exists

No skill currently exists that (a) takes a coordinator bead ID, (b) reads its description + the declared outputs of its dependency beads, (c) generates a minimal end-to-end smoke test (30-50 lines) against the coordinator's stated entry point, (d) runs the smoke test against real (not stub) data, and (e) reports a pass/fail with citation to the failing dependency bead. Every one of these sub-tasks is reachable with existing nexus + subagent tooling; the skill is the missing glue.

#### Gap 3: No integration with plan execution

Even if the coordinator tag and the probe skill exist, there is no hook that fires the probe between bead execution steps. The probe must run **after** the coordinator bead's enrichment lands and **before** the next downstream bead begins. This requires either a workflow convention (plan-enricher emits a probe-run step into the implementation plan) or a hook on bead state transitions.

**Scope honest note (2026-04-11)**: Phase 1 of this RDR closes Gap 3 via the workflow-convention path (text instruction in the enriched bead description) — **not** via a structural hook. An agent or user executing the plan sees "run `/nx:composition-probe <id>`" in the bead description and chooses to run it. If the step is skipped, the probe doesn't fire and there is no recovery path within Phase 1 beyond RDR-067's audit loop at 90-day cadence. A future RDR may add a `bd` state-transition hook that fires the probe structurally. This is a known limitation of Phase 1, not a gap that's been mechanically closed.

## Context

### Background

This RDR is **Phase 1** of the four-RDR silent-scope-reduction remediation. Phase 0 (RDR-069, the critic safety net) is the backstop that catches failures this probe misses. This RDR is the preventive layer: catch composition failures **before** they reach the close gate, where the retcon mechanism kicks in. Catching them earlier means catching them at the responsible bead, not at the integration bead, which bounds reopen cost.

### Why this supersedes the original scope

The original RDR-066 (2026-04-10) bundled three gaps:
1. Plan enricher produces structural plans, not dimensional contracts (INT-1)
2. No coordinator bead concept (substrate for the probe)
3. No composition probe skill (INT-2)

And ranked them effectively equally (all three as Phase 1). The audit inverts this:

- **INT-2 composition probe catches 4/4 incidents *conditional on correct coordinator tagging*** — all of RDR-073, 075, 036, 031 would surface under a runtime composition smoke test IF the coordinator bead in each had `metadata.coordinator=true` set at enrichment time. The 4/4 figure is a ceiling, not a guarantee. Actual catch rate = 4/4 × (coordinator tag precision) × (probe step not skipped). See CA-4, CA-5, and §Gap 3 for the tagging and trigger mechanism risks.
- **INT-1 dimensional contracts catches 1/4 incidents cleanly** — only RDR-073's dim mismatch. The "unwiring" incidents (RDR-036, RDR-075, RDR-031) have **correct** static contracts; the classes exist, their signatures match, the declared types are right. What's missing is that the production code path doesn't actually INVOKE them. Text-level contracts cannot detect "method declared but not called."

The probe is the primary intervention. Contracts are an additive layer. They belong in separate RDRs with separate priority, not bundled.

### Technical Environment

- **`nx:enrich-plan`** (`nx/skills/enrich-plan/SKILL.md`) — the skill plan-enricher dispatches. Today it produces structural enrichment only.
- **`plan-enricher` agent** (`nx/agents/plan-enricher.md`) — the agent that walks beads and populates descriptions with file paths, symbols, test commands.
- **`bd metadata` JSON field** — verified available as of bd 1.0.0 (CA-3 from prior iteration, retained). `bd create --metadata '{"coordinator":true}'` roundtrips via `bd show --json`.
- **`bd --waits-for --waits-for-gate all-children`** — also verified. Native bd coordinator semantic for "wait until all children complete before unblocking." The composition probe would consume this gate to know when a coordinator bead is ready to test.
- **Subagent dispatch from skills** — the probe skill can dispatch a short-lived subagent (general-purpose or developer) to generate the smoke test. The subagent reads the coordinator bead, reads the dependencies' declared outputs, writes a minimal test, runs it, reports.

## Research Findings

### Finding 1 (2026-04-11): bd 1.0.0 provides the coordinator substrate for free

Source: `rdr_process/nexus-audit-2026-04-11` and prior CA-3 spike (nexus_rdr/066-research-1-ca3-verified, from the superseded iteration).

`bd create --metadata '{"coordinator": true, ...}'` stores arbitrary JSON at top-level `metadata` in `bd show --json` output. `--waits-for-gate all-children` (default) provides the "wait until all children complete" semantic natively. Gap 1 is therefore essentially free — the convention is to set `metadata.coordinator=true` on any bead that composes ≥2 dependency beads' outputs, and the probe queries `bd list --json | jq '.[] | select(.metadata.coordinator == true)'`.

### Finding 2 (2026-04-11): LLM can generate grounded smoke tests from a bead description alone (easy case)

Source: `nexus_rdr/066-research-2-ca2-spike-verified` (from the superseded iteration). A Sonnet-class subagent produced a fully line-grounded 11-field contract for `src/nexus/frecency.py:compute_frecency` from a single `Read()` call, with HIGH self-confidence. The same capability applies to smoke-test generation: the subagent can read the coordinator's stated entry point, the dependency beads' declared outputs, and write a minimal end-to-end test in a single pass for self-contained functions with plain types.

**What's unverified**: the hard case. Cross-file generics, protocols, third-party library types. Phase 1 of this RDR must retry the spike on a hard case (e.g., a coordinator that composes two generic types across files) to find the Read-to-Serena boundary. If Read alone is insufficient, the probe skill must delegate to Serena for symbol resolution.

### Finding 3 (2026-04-11): 4/4 audit incidents have probe-catchable structure

Source: `rdr_process/nexus-audit-2026-04-11`.

In each of the 4 confirmed ART incidents, the failure would surface under a runtime composition smoke test:

- **RDR-073**: probe against `GroundedLanguageSystem.dialog().process("ball dog tree")` with the trained pipeline → `IllegalArgumentException: input length 312 != state size 65`. Surfaces the dim mismatch immediately.
- **RDR-075**: probe against `DevelopmentalCurriculumIntegration` with a held-out vocab → verifies InstarLearning weights change during Phase 0. Would catch "learning rule structurally dead" because the weights don't move.
- **RDR-036**: probe against `FactualTeacher.query("what is a dog")` for a phrase not in the HashMap → "I don't know" regardless of neural path state. Surfaces the HashMap short-circuit.
- **RDR-031**: probe against a Grossberg pipeline end-to-end with a real input → isolates whether the mechanism is running or a deprecated pre-swap path is still live.

Each of these probes is ≤50 lines of test code *for dim-mismatch-class failures*. Unwiring failures (RDR-036, RDR-075, RDR-031) may require heavier setup (e.g., RDR-075's InstarLearning probe needs a full training loop to observe weight changes — more than 50 lines). The 30-50 line budget applies to the dim-mismatch sub-class; unwiring probes are sized to whatever realistic composition exercise is required. **Additionally, the 4/4 catch claim is conditional on correct coordinator tagging** (see CA-4 and CA-5) — the probe only runs if `metadata.coordinator=true` is set on the bead at enrichment time.

### Retained findings from prior iteration (verified CAs)

Two findings from the 2026-04-11 first iteration of this RDR are carried forward as verified prerequisites. They are not re-numbered because the iteration history predates this reissue's CA set; they are tracked here so any gate checking CA status sees the complete picture.

- [x] **Retained CA: `bd metadata.coordinator=true` substrate works** — `bd create --metadata '{"coordinator":true}'` roundtrips via `bd show --json`; `--waits-for-gate all-children` provides the native coordinator wait semantic. **Status**: VERIFIED (2026-04-11). **Method**: Source Search + live roundtrip test. **T2**: `nexus_rdr/066-research-1-ca3-verified` (id 714).
- [x] **Retained CA: LLM-driven contract generation works for the easy case** — a Sonnet-class subagent produced a fully line-grounded 11-field contract for a self-contained 132-line Python function using one Read call. HIGH self-confidence on all fields. **Status**: VERIFIED for the easy case (2026-04-11). **Method**: Spike. **Scope**: plain types, self-contained functions, same-file helpers only. Hard case (cross-file generics) remains open as CA-1 below. **T2**: `nexus_rdr/066-research-2-ca2-spike-verified` (id 715).

### Critical Assumptions

- [ ] **CA-1**: A composition probe can be generated automatically from a coordinator bead description + its dependency beads' declared outputs, for "hard" cases (cross-file generics, protocols, third-party types). The easy case (plain types in self-contained functions) is verified (see Retained CA above). The hard case is unverified.
  — **Status**: Unverified — **Method**: Spike on a real cross-file generic composition (e.g., take a nexus or ART coordinator bead that composes two generic types and dispatch the probe-generation subagent against it)
  — **Implication for §Technical Design**: if CA-1 fails, the subagent architecture shifts from Read-only to Read+Serena (see "Design-branches on CA-1 outcome" note in §Technical Design).

- [ ] **CA-2**: Probe failure messages are interpretable enough to reopen the correct dependency bead. A failure that says "something went wrong at line X" isn't useful — the probe must surface "bead M's declared output shape does not match bead N's expected input shape at call site X."
  — **Status**: Unverified — **Method**: Same spike as CA-1; evaluate the generated probe's failure output quality
  — **Fallback design**: if CA-2 fails — i.e., the probe reliably detects failures but attribution is unintelligible (e.g., NullPointerException deep in a call stack) — the probe degrades to **unattributed failure mode**: surface the raw test output, flag the coordinator bead for manual investigation, do not auto-attribute. See §Failure Modes.

- [ ] **CA-3**: The probe can run in a bounded amount of time (~30-120 seconds) without requiring full test-suite setup, database seeds, or other heavy infrastructure. A probe that takes 10 minutes is unusable.
  — **Status**: Unverified — **Method**: Spike; measure generation + execution latency on a real target

- [ ] **CA-4**: The coordinator-bead identification convention (`metadata.coordinator=true`) is reliably applied by plan-enricher or by users. If it's missed, the probe doesn't fire and the 4/4 catch claim degrades proportionally to the miss rate.
  — **Status**: Unverified — **Method**: update plan-enricher prompt; test on a sample 10-20 bead plan; measure the miss rate against a manually-labeled ground truth
  — **Mitigation, not backstop**: the 90-day RDR-067 audit loop catches missed tags eventually but not before a specific close ships without a probe. A stronger mitigation is CA-5 below.

- [ ] **CA-5** (added 2026-04-11 from substantive-critic finding): Plan-enricher can reliably detect that a bead composes outputs from ≥2 prior beads **via cross-bead method-ownership lookup**. The detection rule is "if this bead's description names methods defined in ≥2 prior beads in the plan, tag as coordinator" — this requires the enricher to hold method-to-bead ownership mappings across the full walk, not just inspect the current bead in isolation.
  — **Status**: Unverified — **Method**: test plan-enricher on a 10-20 bead plan with a mix of coordinator and non-coordinator beads; measure the miss rate and false-positive rate
  — **Fallback**: a simpler heuristic that does NOT depend on cross-bead lookup — any bead with ≥2 `--waits-for` dependencies is a coordinator candidate regardless of method content. May over-tag (false positives) but under-misses.

## Proposed Solution

### Approach

Three components, built in order:

1. **Coordinator convention**: plan-enricher agent prompt gains a rule — "any bead whose implementation composes outputs from ≥2 prior beads (e.g., calls methods defined in bead M and bead N where M,N < this bead) must be tagged `metadata.coordinator=true` when the bead is updated." This is prompt-level, no new infrastructure. Enforced by the prompt, verified by the audit loop (RDR-067).

2. **`nx:composition-probe` skill**: new skill at `nx/skills/composition-probe/SKILL.md`. Takes a coordinator bead ID. Reads the bead description + the declared outputs of its dependencies. Dispatches a subagent (general-purpose, sonnet-class) with a fixed prompt: "generate a 30-50 line minimal end-to-end test against `<entry_point>` that exercises the composition with realistic input. Include assertions on the output shape and the dimensionality of intermediate values. Fail fast on any exception." The subagent generates the test, writes it to a temp file, runs it via the existing test runner (project-dependent — `mvn test`, `uv run pytest`, etc.), reports pass/fail + the test output.

3. **Workflow integration**: plan-enricher agent, when it closes enrichment on a coordinator bead, emits a required probe-run step in the plan description. The step is: "Before beginning the next bead, run `/nx:composition-probe <this-bead-id>` and verify PASS." Agents executing the plan see the step and run the probe. If the probe fails, the agent cannot proceed until the failing dependency bead is reopened (detected by the probe's output identifying which dependency's contract broke).

### Technical Design

> **Design branches on CA-1 outcome.** The skill shape and subagent prompt below describe the **CA-1 passes** branch (Read-only subagent, no Serena). If the CA-1 hard-case spike in Phase 1 shows that cross-file generics / protocols / third-party types require symbol resolution, the architecture shifts: the subagent prompt gains `jet_brains_find_symbol` instructions, the tool budget increases, and the output contract may change. The design below is conditional. Do not lock Phase 3 implementation against it until Phase 1 resolves.

**Probe skill shape** (`nx/skills/composition-probe/SKILL.md`):

```markdown
---
name: composition-probe
description: Use to run a composition smoke test against a coordinator bead before its downstream beads begin
---

## When to Use
- User invokes `/nx:composition-probe <coordinator-bead-id>`
- Plan execution reaches a step tagged `probe: <bead-id>`

## Behavior
1. Read coordinator bead via `bd show <id> --json`. Extract description, entry point, dependency bead IDs.
2. Read each dependency bead's declared output (from its own description).
3. Dispatch general-purpose subagent with:
   - Coordinator entry point
   - Dependencies' declared outputs
   - Instruction: generate a 30-50 line test exercising the composition
4. Subagent writes test to `/tmp/probe-<bead-id>.{py|java|...}` and runs it
5. Parse test output; return pass/fail + the subagent's diagnosis of which dependency bead's contract broke (if failure)

## Output
- On PASS: brief confirmation, coordinator bead unblocks
- On FAIL: structured report citing the failing dependency bead with suggested reopen
```

**Subagent prompt** (used by the skill):

> Generate a minimal (30-50 line) end-to-end smoke test against `<entry_point>` that exercises the composition of dependencies {list of dep bead IDs + their declared outputs}. The test should:
> - Use realistic input data (not mocks, not stubs, not defaults)
> - Assert on output shape AND intermediate value dimensionality
> - Fail fast on any exception
> - Print which dependency's contract was violated if the composition fails
> Write the test to /tmp/probe-{bead_id}.{ext} and run it. Report pass/fail + which dependency (if any) violated its declared output shape.

**Integration with plan-enricher**: prompt update. Add a rule to the enrichment walk: "If this bead's description names methods defined in ≥2 prior beads in the plan, set `metadata.coordinator=true` via `bd update --metadata` AND add a final implementation step 'Run `/nx:composition-probe <this-id>` and verify PASS before beginning the next bead.'"

### Alternatives Considered

**Alternative 1: Wait for integration tests (status quo)**

No probe; rely on the final integration bead to surface composition failures.

**Rejection**: this is the failure mode the audit documents. By the time the integration bead runs, N-1 beads are merged and the sunk cost forces a workaround. Catching the failure earlier is the entire point.

**Alternative 2: Textual contracts instead of a runtime probe (ART's INT-1)**

Have plan-enricher populate a `## Contracts` section per bead with declared input/output shapes; gate on matching contracts across beads.

**Rejection**: catches only 1/4 audit incidents (RDR-073's dim mismatch). The 3 "unwiring" incidents have correct declared contracts — classes exist, signatures match — what's missing is that the production code path doesn't actually invoke the composed mechanisms. A runtime probe surfaces this immediately; a textual contract does not. RDR-068 (Dimensional Contracts at Enrichment) adds this as a belt-and-suspenders layer on top of the probe, not as a replacement.

**Alternative 3: Full test-suite run after coordinator bead**

Run the existing test suite against main after each coordinator bead lands.

**Rejection**: too slow (minutes to hours), too noisy (unrelated test failures), and doesn't specifically exercise the composition. The probe is targeted: ~30-120 seconds, against the specific composition point, generating a fresh test specific to the coordinator.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| Coordinator bead tag | `bd metadata` (bd 1.0.0) | **Reuse** — no new fields needed |
| Wait-gate semantic | `bd --waits-for-gate all-children` | **Reuse** — native |
| Probe skill | `nx/skills/composition-probe/SKILL.md` | **New** (create) |
| Test generator subagent | general-purpose / developer agent | **Reuse** — standard agent |
| Test runner | project-specific (`mvn test`, `uv run pytest`, etc.) | **Reuse** — shell out |
| Plan-enricher prompt | `nx/agents/plan-enricher.md` | **Extend** — add coordinator tagging rule |

### Decision Rationale

The audit shows 4/4 incidents are coordinator-bead failures. A probe at the coordinator boundary catches all 4 at the lowest sunk-cost point. No other intervention has this breadth (INT-1 catches 1/4, INT-3 is speculative, INT-6 is superseded). This is the highest-leverage preventive intervention available.

The build effort is bounded: one new skill (composition-probe), one prompt update (plan-enricher), one workflow integration (probe-run step in generated plans). No new infrastructure, no new bead types, no new T2 schemas.

## Briefly Rejected Alternatives

(Main alternative analysis is in §Proposed Solution §Alternatives Considered above — these are the one-liners that didn't merit the full treatment.)

- **Coordinator beads as a new first-class type in bd**: unnecessary — metadata suffices
- **Probe per bead, not per coordinator**: waste — most beads don't touch composition points
- **Human-authored probes instead of agent-generated**: doesn't scale — would require rewriting every plan

## Trade-offs

### Consequences

- **Positive**: catches 4/4 audit incidents at bounded reopen cost
- **Positive**: surfaces the identity of the failing dependency bead, enabling targeted reopen rather than coordinator rewrite
- **Positive**: composable with RDR-065 (close-time replay), RDR-068 (contracts), RDR-069 (critic) — none overlap, all layer
- **Negative**: adds ~30-120 seconds per coordinator bead (not per bead; only coordinators)
- **Negative**: requires plan-enricher prompt discipline (CA-4 mitigation); missed coordinator tags mean missed probes
- **Negative**: probe subagent may generate flaky or low-quality tests on hard cases until CA-1/CA-2 are verified and the prompt is tuned

### Risks and Mitigations

- **Risk**: Probe false-positive rate is high on noisy targets (e.g., flaky integration points). **Mitigation**: the probe runs the subagent-generated test up to 3 times; only consistent failures block.
- **Risk**: Probe takes too long on hard cases. **Mitigation**: CA-3 spike; if latency unacceptable, narrow probe scope to dimensional assertions only (skip runtime execution).
- **Risk**: Plan-enricher misses coordinator tagging (CA-4). **Mitigation**: RDR-067 audit loop detects plans with undetected coordinators by cross-referencing bead dependency graphs.

### Failure Modes

- Probe generation times out → fall back to advisory ("probe could not be generated; manual composition check required")
- Probe execution times out → surface as failure with the timeout as the reason
- Probe flaky (passes 2/3 runs) → surface ambiguity to user; do not block but warn
- Coordinator tagging missed → no probe runs; caught later by RDR-067 audit loop; no data loss but reduced effectiveness until corrected
- **Probe fails but failure output is unintelligible** (CA-2 fallback) → the probe surfaces the raw test output, does not attempt auto-attribution, flags the coordinator bead for manual investigation. The subagent prompt explicitly instructs "If you cannot attribute the failure to a specific dependency bead, say so explicitly rather than guessing."
- **Probe step skipped** (user or agent ignores the text instruction in the enriched bead description) → no probe runs; caught only by RDR-067 audit loop or by RDR-069 critic at close time. Acknowledged as a known limitation of the convention-based Gap 3 closure; a future RDR may add a structural hook.

## Implementation Plan

### Prerequisites

- [ ] RDR-069 (Phase 0) shipped — the critic is the safety net if this probe misses; build the net first
- [ ] CA-1 through CA-5 verified via a hard-case spike (CA-5 is the cross-bead method-ownership detection heuristic for plan-enricher)
- [ ] **Bead-level ordering enforcement**: when implementation beads are created for this RDR, use `bd dep add <066-impl-epic> <069-impl-epic>` so the 066 implementation is mechanically blocked until 069 ships. Prose prerequisite alone is not sufficient — see the substantive-critic finding in `nexus_rdr/069-research-2-ca1-ca3-critic-determinism-spike` (run 1 Significant issue) and the underlying concern that parallel acceptance of 066 and 069 would allow 066 implementation to start without the safety net

### Minimum Viable Validation

Run the probe against a synthetic coordinator bead constructed from ART's RDR-073 IMPL-04 retrospectively. The probe should generate a test against `GroundedLanguageSystem.dialog().process("ball")` with the trained pipeline, run it, and surface `IllegalArgumentException: input length 312 != state size 65`. This is a known-failure target with a known-correct answer.

### Phase 1: Hard-case spike

- Pick a cross-file generic composition from the nexus or ART codebase (e.g., a coordinator that composes `Pipeline<T>` and `Projection<U>`)
- Dispatch the proposed probe-generation subagent with a fixed prompt
- Measure: did the subagent need Serena / symbol lookup? How long did generation take? Was the generated test accurate? Was the failure message interpretable?
- Outcome: decide Read-only vs. Read+Serena architecture; document in CA-1/CA-2 dispositions

### Phase 2: Coordinator convention + plan-enricher update

- Document the `metadata.coordinator=true` convention in `nx/agents/plan-enricher.md`
- Update plan-enricher prompt: detection rule, tagging rule, probe-step emission rule
- Test: run plan-enricher on a small test plan with a known coordinator; verify tag and probe step are emitted

### Phase 3: `nx:composition-probe` skill

- Create `nx/skills/composition-probe/SKILL.md` with the shape in §Technical Design
- Subagent prompt authored and pinned
- Test runner integration: support py/java/ts targets (the common nexus + ART cases)
- Test: run probe against the RDR-073 MVV target, confirm it catches

### Phase 4: Plugin release

- 3.8.3 (or whatever the sequence is at that point, after RDR-069's release)
- Update changelogs, reinstall, smoke test

### Phase 5: Recursive self-validation

- **5a**: synthetic composition failure injected into a test coordinator, verify probe catches
- **5b**: independent code review (substantive-critic on this RDR pre-close)
- **5c**: real self-close of RDR-066 via the new close flow (RDR-069 active)

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
|---|---|---|---|---|---|
| `nx/skills/composition-probe/` | `ls` | `cat SKILL.md` | `rm` | smoke test | git |
| `/tmp/probe-*.{py|java|...}` | N/A — ephemeral | N/A | auto-cleanup | N/A | N/A |

## Test Plan

- **Scenario 1** (Phase 1 spike): cross-file generic composition; does the subagent need Serena; is the probe accurate; is latency bounded
- **Scenario 2** (RDR-073 retrospective): known-failure target; verify probe catches the 312D/65D mismatch
- **Scenario 3** (clean coordinator): a coordinator bead whose composition actually works; verify probe returns PASS without false positive
- **Scenario 4** (RDR-036 retrospective): verify probe would have caught the HashMap short-circuit
- **Scenario 5** (CA-4 — missed tag): run plan-enricher on a plan with a coordinator that plan-enricher doesn't recognize; measure the miss rate
- **Scenario 6** (recursive 5a): synthetic retcon in a test coordinator → probe catches → this RDR's close passes

## Validation

### Testing Strategy

The recursive self-validation step (Phase 5) is the load-bearing test. Every phase's acceptance is measurable: Phase 1 produces a spike report; Phase 2 produces a plan-enricher diff with tests; Phase 3 produces a skill file with a demonstrable pass/fail run; Phase 4 produces a plugin release.

### Performance Expectations

Probe latency: ~30-120 seconds per coordinator bead. Measured in Phase 1 spike. If unacceptable, Phase 3 narrows scope to static assertions (no runtime execution) as a fallback.

## Finalization Gate

### Contradiction Check

No contradiction with RDR-065, 067, 068, 069 — the probe is a specific preventive layer at the coordinator boundary, complementary to the close-time replay (RDR-065), the critic dispatch (RDR-069), the audit loop (RDR-067), and the enrichment-time contracts (RDR-068). Each addresses a distinct point in the lifecycle.

### Assumption Verification

CA-1, CA-2, CA-3, CA-4 must be verified in Phase 1 spike before the gate passes.

### Scope Verification

MVV is the RDR-073 retrospective (known-failure target, known-correct probe output). In scope, executable in Phase 3.

### Cross-Cutting Concerns

- **Versioning**: plugin release bundled with RDR-069 or separate
- **Build tool compatibility**: probe runs project-native test runners
- **Licensing**: AGPL-3.0, no new dependencies
- **Deployment model**: plugin reinstall
- **IDE compatibility**: N/A
- **Incremental adoption**: probe is opt-in per coordinator bead; absence of tag means no probe
- **Secret/credential lifecycle**: N/A
- **Memory management**: /tmp probe files ephemeral; no accumulation

### Proportionality

Right-sized. One new skill, one prompt update, one workflow integration. No new infrastructure.

## References

- `rdr_process/failure-mode-silent-scope-reduction` — ART canonical writeup
- `rdr_process/nexus-audit-2026-04-11` — nexus historical audit (load-bearing evidence)
- `nexus_rdr/066-research-1-ca3-verified` — CA-3 bd metadata roundtrip (prior iteration, retained)
- `nexus_rdr/066-research-2-ca2-spike-verified` — CA-2 easy-case spike (prior iteration, retained)
- `~/git/ART/docs/rdr/post-mortem/073-cogem-training-deployment-and-dialog-runtime-integration.md` — RDR-073 retrospective target
- `~/git/ART/docs/rdr/post-mortem/036-grossberg-language-understanding.md` — RDR-036 unwiring evidence
- `~/git/ART/docs/rdr/post-mortem/075-semantic-learning-and-generalization.md` — RDR-075 unwiring evidence
- `~/git/ART/docs/rdr/post-mortem/031-equation-first-grossberg.md` — RDR-031 unwiring evidence
- `nx/skills/enrich-plan/SKILL.md`, `nx/agents/plan-enricher.md` — the pieces to extend
- RDR-069 (Phase 0 — critic dispatch) — the backstop

## Revision History

- 2026-04-10 — Stub created as "Enrichment-Time Contract Pre-Flight" with 3 bundled gaps (contracts + probe + coordinator concept).
- 2026-04-11 (first iteration) — CA-2 and CA-3 verified; scope narrowed to "contracts + probe"; merged into PR #143.
- 2026-04-11 (second iteration) — **Reissued with new scope** based on nexus historical audit. Evidence shows the composition probe catches 4/4 audit incidents while dimensional contracts catch 1/4 cleanly. Probe is the primary intervention; contracts moved to RDR-068 as belt-and-suspenders. Coordinator-bead concept resolved via `bd metadata.coordinator=true` (free). New scope: Composition Smoke Probe at Coordinator Beads. Priority stays P2. Phase 1 of the 4-RDR remediation (depends on RDR-069 Phase 0). See `rdr_process/nexus-audit-2026-04-11` for evidence and bead `nexus-640` for the cycle.
- 2026-04-11 (third iteration — **this one**) — **Critic-driven fixes** from the RDR-069 CA-1 determinism spike (`nexus_rdr/069-research-2-ca1-ca3-critic-determinism-spike`). Two runs of `nx:substantive-critic` against this RDR surfaced issues the second iteration missed. Fixes applied: (a) retained-CA section added to track the prior-iteration findings (bd metadata, easy-case contracts) that were verified but not numbered; (b) CA-5 added for the coordinator detection heuristic's cross-bead method-ownership lookup requirement; (c) §Technical Design marked as conditional on CA-1 outcome (Read-only vs Read+Serena branches); (d) CA-2 fallback design added for the unattributed-failure case; (e) §Failure Modes extended with probe-step-skipped and unintelligible-failure cases; (f) §Gap 3 acknowledges convention-not-hook closure explicitly; (g) Implementation Plan Prerequisites adds bead-level ordering enforcement via `bd dep add`; (h) Phase 5 step labels renamed 6a/6b/6c → 5a/5b/5c; (i) 4/4 claim qualified as "conditional on correct tagging"; (j) duplicate §Alternatives Considered renamed to §Briefly Rejected Alternatives. Bead: nexus-57j.
