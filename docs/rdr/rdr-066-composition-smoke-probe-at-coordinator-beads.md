---
id: RDR-066
title: "Composition Smoke Probe at Coordinator Beads"
type: process
status: accepted
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-10
reissued: 2026-04-11
accepted_date: 2026-04-11
gate_iteration: 5
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

### Finding 3 (2026-04-11, revised post-Phase-1b): 3/4 audit incidents are inter-bead composition failures the probe framework addresses; 1/4 is an out-of-scope intra-class failure mode

Source: `rdr_process/nexus-audit-2026-04-11` (original claim), refined by `nexus_rdr/066-research-4-ca5b-retrospective` (id 734, Phase 1b CA-5b retrospective).

The original nexus audit claimed "4/4 confirmed ART incidents would be caught by the composition probe." Phase 1b CA-5b retrospective discovered the 4/4 figure was **over-scoped** by conflating two distinct failure modes:

**Probe-catchable (3/4 — inter-bead composition failures)**:

- **RDR-073** (`ART-sift`, `GroundedLanguageSystem coordinator`): probe against `GroundedLanguageSystem.dialog().process("ball dog tree")` with the trained pipeline → `IllegalArgumentException: input length 312 != state size 65`. 2 declared `bd` dependencies (`ART-n160`, `ART-nm8w`) — fallback heuristic tags it. ✓
- **RDR-075** (`ART-cam3.5`, `DevelopmentalCurriculum.runPhase2 + install CogEMEmotionalModulator + RDR-075 non-regression`): probe would verify InstarLearning weights change during Phase 0 under a held-out vocab. 2 declared `bd` dependencies (`ART-cam3.3`, `ART-cam3.4`) — fallback heuristic tags it. ✓
- **RDR-031** (`ART-5iry`, `3.4 Full pipeline wiring: SSMF -> GM -> DriveRep -> Heterarchy`): probe against a Grossberg pipeline end-to-end isolates whether production classes are swapped for Grossberg ODE implementations or a pre-swap path is still live. 3 declared `bd` dependencies (`ART-eny6`, `ART-mjbd`, `ART-o26j`) — fallback heuristic tags it. ✓

Each of these is a **composition across multiple beads' outputs** — exactly what a runtime probe at the coordinator boundary is designed to catch.

**Not probe-catchable (1/4 — intra-class short-circuit, out of probe framework scope)**:

- **RDR-036** (`ART-9z2p`, `P2-T2.2: FactualTeacher implementation`): `FactualTeacher.query` was supposed to delegate to the resonance path but instead did a local HashMap lookup and returned. This is an **intra-class failure** — the short-circuit is inside FactualTeacher's own implementation boundary. There is no composition of ≥2 prior beads' outputs happening. The coordinator probe framework is designed for inter-bead composition failures; this is a different failure mode entirely. Only 1 declared `bd` dependency (`ART-ms10`, its unit tests) — fallback heuristic does NOT tag it. ✗

Critically, **neither the fallback heuristic NOR the full CA-5 method-ownership lookup would catch RDR-036**. FactualTeacher.query's description names methods defined *within FactualTeacher itself*, not methods from other beads. There is no cross-bead method-ownership edge for the full lookup to discover. Un-deferring CA-5 full would not change the catch rate on this target.

**The correct catch-rate claim is 3/4** — three inter-bead composition failures the probe framework addresses, and one intra-class short-circuit that belongs to a different intervention (likely RDR-068 dimensional contracts, which could catch "FactualTeacher.query declared return type = resonance-cascade output" vs "actual return = HashMap raw value" as a contract violation).

**The 3/4 catch claim is conditional on four distinct factors**, not one:

1. **Coordinator tag precision** (CA-4): plan-enricher writes `metadata.coordinator=true` when it should
2. **Probe step adherence**: the agent/user running the plan actually executes `/nx:composition-probe <id>` (Gap 3 is convention-based, not structural)
3. **Scope alignment** (CA-5b): the failure is an inter-bead composition failure, not intra-class — verified for 3/3 in-scope historical targets in Phase 1b
4. **Probe run integrity** (CA-1/CA-2/CA-3): the generated probe actually surfaces the failure rather than false-passing — verified by Phase 1a hard-case spike against `search_engine.search_cross_corpus`

**RDR-036 attribution shift**: the nexus audit should be re-read with a distinction between inter-bead and intra-class failures. The INT-1 (dimensional contracts) catch rate of "1/4" may actually be higher if RDR-036 is correctly attributed to contracts (2/4 if RDR-073's dim mismatch AND RDR-036's return-type mismatch are both contract-catchable). This re-attribution is relevant to RDR-068's scope justification and should be raised there.

### Finding 5 (2026-04-11, Phase 1a spike): CA-1/CA-2/CA-3 all VERIFIED — Read-only subagent is sufficient for nexus coordinator targets

Source: `nexus_rdr/066-research-5-ca1-ca2-ca3-hard-case-spike` (Phase 1a runtime hard-case spike). Dispatched `nx:codebase-deep-analyzer` against `src/nexus/search_engine.py:149` (`search_cross_corpus`) with a fixed-shape minimal relay. The target composes outputs from 4 source modules (`db/t3.py`, `types.py`, `scoring.py`, plus the search_engine itself) — a real cross-file hard case.

- **CA-1** `READ-ONLY-SUFFICIENT`: probe generated from `Read`/`Grep`/`Glob` only. No Serena symbol resolution needed. The `Any` type at injection boundaries (`t3: Any`) is the dominant nexus pattern — contracts are expressed as runtime dict-key presence, not typed generics requiring inference. Conftest-fixture discovery heuristic handles test isolation.
- **CA-2** `INTERPRETABLE`: failure messages name the specific dependency by file:line. Exercised three failure modes (missing key, wrong type, silent coercion). The probe's explicit `isinstance` assertions catch silent coercion paths that natural exceptions would miss — a probe design requirement, not a framework gap.
- **CA-3** `WITHIN-BUDGET`: execution 1.93 seconds (5 tests, real EphemeralClient + ONNX MiniLM, no mocks). Well under the 30-120s ceiling. Generation ~8 minutes wall-clock for a hard-case target.

**Phase 3 architecture decision**: Read-only subagent is sufficient. Tool budget for the composition-probe skill: `Read + Grep + Glob + conftest-search heuristic`. Serena escalation is reserved for `typing.Protocol` / `TypeVar` cases not present in nexus. Phase 3 ships with the minimal tool budget per the "Read-only branch" of the CA-1 design decision.

### Finding 4 (2026-04-11): plan-enricher CA-4/CA-5 feasibility — Phase 2 is near-zero-cost

Source: `nexus_rdr/066-research-3-ca4-ca5-enricher-feasibility` (id 730). Dispatched `nx:codebase-deep-analyzer` against `nx/agents/plan-enricher.md` and `nx/skills/enrich-plan/SKILL.md` with a fixed-shape minimal relay. Purely code-analytic — no runtime spike required.

**The enricher walks beads one at a time with zero cross-bead state.** The per-child loop at `plan-enricher.md:98-100` processes beads serially with no accumulator, no map, no session memory of prior walks. Tool budget: Write, `/beads:show`, `/beads:update --body-file`, scratch, memory_get/put, sequential-thinking. No Serena, no catalog, no symbol-resolution tools.

**CA-4 disposition**: `FEASIBLE-WITH-DIFF`. The enricher's only mutation call at `plan-enricher.md:134` is `/beads:update <id> --body-file ...` — it never passes `--metadata`. Verified against `bd show nexus-ctq.1 --json`: `"metadata": {}` despite being well-enriched. The `bd` CLI supports `--metadata` roundtrips (retained CA, id 714). The diff is prompt-only: extend the update call to include `--metadata '{"coordinator": true}'` when the detection heuristic fires. **Silent-omission failure mode**: the enricher can successfully write the body and silently omit the metadata flag with no error. Mitigation: add a post-write verification step asserting `.metadata.coordinator == true` persisted.

**CA-5 full disposition**: `REQUIRES-REWORK`. Full method-to-bead ownership lookup needs (a) an explicit accumulator phase before the per-bead loop, and (b) Serena added to the tool budget. Not a bounded prompt diff. **Deferred to a follow-on bead tracked under the RDR-066 implementation epic.** Not required for shipping Phase 2.

**CA-5 fallback disposition**: `ZERO-COST`. `bd show <id> --json` already returns a `dependencies` array. The enricher already calls `/beads:show <id>` for each bead at line 99 — the dependency count is available in the existing read path with **no additional tool calls**. The check `len(dependencies) >= 2 → coordinator candidate` is a single prompt sentence. No cross-bead state, no tool budget change, no architectural rework.

**Trade-off of the fallback — resolved post-Phase-1b**:

- **Over-tagging** (false positives): fan-in beads (e.g., "final integration test" beads that depend on many siblings but don't actually compose outputs in failure-prone ways) get tagged as coordinators and run probes they don't need. Wasted probe runtime, no missed failures. Correction channel: RDR-067 audit loop flags coordinator-tagged beads whose probes never catch real failures at 90-day cadence.

- **Under-tagging** (false negatives within scope): was the hypothesized failure mode in the original CA-5 full argument — a bead composing ≥2 prior beads with only 1 declared `bd` dependency. Phase 1b CA-5b retrospective verified this does NOT occur on the historical target set: all 3 in-scope inter-bead composition coordinators (RDR-073 `ART-sift`, RDR-075 `ART-cam3.5`, RDR-031 `ART-5iry`) have ≥2 declared `bd` dependencies. **The fallback heuristic achieves 3/3 on in-scope targets.**

- **Out-of-scope failures** (framework scope boundary, not a detection gap): Phase 1b surfaced RDR-036 (FactualTeacher.query HashMap short-circuit) as a 4th historical incident that neither the fallback heuristic nor the full CA-5 method-ownership lookup can catch, because the failure is intra-class rather than inter-bead. This is a framework scope boundary — the coordinator probe targets inter-bead composition failures. Intra-class short-circuits require a different intervention (likely RDR-068 dimensional contracts, which could catch "declared return type vs actual return type" as a contract violation). See §Finding 3 for the full framing and the RDR-036 re-attribution note.

**CA-5 full deferral is doubly justified post-Phase-1b**: (1) cost — architectural rework is required to support cross-bead method-ownership lookup (Finding 4), (2) scope — even if CA-5 full were implemented, it would not improve the catch rate on the historical target set because RDR-036 is out-of-scope for both detection rules. The deferral can be closed as "not needed" rather than "deferred pending cost amortization" — though it remains valuable as an optional refinement for future plan structures where authors may forget to declare all semantic composition edges via `bd dep add`.

**Material implication**: Phase 2 of the Implementation Plan (coordinator convention + plan-enricher update) is now a near-zero-cost prompt diff, not a moderate rewrite. The risk profile of RDR-066 shifts: Phase 1 hard-case spike (CA-1/CA-2/CA-3) remains the gating expensive work; Phase 2 is trivially cheap.

### Retained findings from prior iteration (verified CAs)

Two findings from the 2026-04-11 first iteration of this RDR are carried forward as verified prerequisites. They are not re-numbered because the iteration history predates this reissue's CA set; they are tracked here so any gate checking CA status sees the complete picture.

- [x] **Retained CA: `bd metadata.coordinator=true` substrate works** — `bd create --metadata '{"coordinator":true}'` roundtrips via `bd show --json`; `--waits-for-gate all-children` provides the native coordinator wait semantic. **Status**: VERIFIED (2026-04-11). **Method**: Source Search + live roundtrip test. **T2**: `nexus_rdr/066-research-1-ca3-verified` (id 714).
- [x] **Retained CA: LLM-driven contract generation works for the easy case** — a Sonnet-class subagent produced a fully line-grounded 11-field contract for a self-contained 132-line Python function using one Read call. HIGH self-confidence on all fields. **Status**: VERIFIED for the easy case (2026-04-11). **Method**: Spike. **Scope**: plain types, self-contained functions, same-file helpers only. Hard case (cross-file generics) remains open as CA-1 below. **T2**: `nexus_rdr/066-research-2-ca2-spike-verified` (id 715).

### Critical Assumptions

- [x] **CA-1**: A composition probe can be generated automatically from a coordinator bead description + its dependency beads' declared outputs, for "hard" cases (cross-file generics, protocols, third-party types).
  — **Status**: `READ-ONLY-SUFFICIENT` — verified by Phase 1a hard-case spike (Finding 5). Target: `src/nexus/search_engine.py:149` (`search_cross_corpus`) composing 4 source modules. Probe generated from `Read`/`Grep`/`Glob` only, no Serena symbol resolution required. The `Any` type at injection boundaries is the dominant nexus pattern — contracts are expressed as runtime dict-key presence, not typed generics. **T2**: `nexus_rdr/066-research-5-ca1-ca2-ca3-hard-case-spike`.
  — **Implication for §Technical Design**: CA-1 Read-only branch selected. Phase 3 ships with minimal tool budget (Read + Grep + Glob + conftest-fixture heuristic). Read+Serena branch is reserved for `typing.Protocol` / `TypeVar` cases not present in nexus.

- [x] **CA-2**: Probe failure messages are interpretable enough to reopen the correct dependency bead.
  — **Status**: `INTERPRETABLE` — verified by Phase 1a spike (Finding 5). Probe failure messages name the specific dependency by file:line (e.g., `"Result[0].distance not float: <class 'str'> — scoring.min_max_normalize (scoring.py:35) will fail on non-float window elements"`). Three failure modes exercised: missing dict key, wrong type, silent coercion path. The probe's explicit `isinstance` assertions catch silent coercion that natural exceptions would miss — a probe design requirement, not a framework gap.
  — **Fallback design** (retained for edge cases not yet encountered): if CA-2 fails on a future target — i.e., the probe reliably detects failures but attribution is unintelligible (e.g., NullPointerException deep in a call stack) — the probe degrades to **unattributed failure mode**: surface the raw test output, flag the coordinator bead for manual investigation, do not auto-attribute. See §Failure Modes.

- [x] **CA-3**: The probe can run in a bounded amount of time (~30-120 seconds) without requiring full test-suite setup, database seeds, or other heavy infrastructure.
  — **Status**: `WITHIN-BUDGET` — verified by Phase 1a spike (Finding 5). Execution: **1.93 seconds** for a 5-test probe against `search_cross_corpus` using real `EphemeralClient` + ONNX MiniLM (no mocks, no API keys, no seeds). Well under the 30-120s ceiling. Generation latency ~8 minutes wall-clock for reading 6 source files and writing the probe, with one round of correction for a `store()` → `put()` API mismatch discovered mid-generation.

- [x] **CA-4**: The coordinator-bead identification convention (`metadata.coordinator=true`) can be reliably applied by plan-enricher. **Status**: `FEASIBLE-WITH-DIFF` — verified by code analysis (see Finding 4). The `bd update --metadata` path is available and unused today; Phase 2 adds a single prompt sentence to invoke it when the coordinator heuristic fires. **Silent-omission mitigation**: Phase 2 also adds a post-write verification step asserting `.metadata.coordinator == true` was persisted, so the enricher cannot silently drop the tag without surfacing to the user. **Method**: Code analysis + post-write verification pattern. **T2**: `nexus_rdr/066-research-3-ca4-ca5-enricher-feasibility` (id 730).

- [x] **CA-5 (fallback — shipping path)**: Plan-enricher can detect coordinator candidates via the cheap heuristic "any bead with ≥2 `bd` dependencies is a coordinator candidate." **Status**: `ZERO-COST` — verified by code analysis (see Finding 4). `bd show <id> --json` already returns `.dependencies`; the enricher already calls `/beads:show <id>` in its existing read path at `plan-enricher.md:99`. The check is a single prompt sentence consuming data already in the read path. **False-positive risk**: over-tags fan-in beads that don't actually compose outputs. Over-tagging wastes probes, does not miss failures. Correction channel: RDR-067 audit loop flags coordinator-tagged beads whose probes never catch real failures. **T2**: `nexus_rdr/066-research-3-ca4-ca5-enricher-feasibility` (id 730).

- [~] **CA-5 (full — deferred)**: The richer heuristic "detect coordinators via cross-bead method-ownership lookup in the enricher's walk" **requires architectural rework** (accumulator phase + Serena addition to tool budget), confirmed by code analysis (Finding 4). The current enricher at `plan-enricher.md:98-100` walks beads one at a time with zero cross-bead state; full method-ownership detection is not a bounded prompt diff. **Status**: `DEFERRED` — out of scope for Phase 2 shipping. Tracked as a follow-on bead under the RDR-066 implementation epic, to be created at plan time. The fallback heuristic above ships first; the richer detection is an optional upgrade after shipping. **No longer gating** the RDR's acceptance, but see CA-5b below — the catch-rate equivalence claim between fallback and full is itself an unverified assumption that needs retrospective verification.

- [x] **CA-5b** (added 2026-04-11 from gate Layer 3 critic finding SIG-1, resolved Phase 1b): The fallback heuristic (≥2 declared `bd` dependencies → coordinator candidate) tags each of the historical ART incident coordinator beads that are in-scope for the probe framework.
  — **Status**: `STRUCTURALLY ADEQUATE — 3/3 on in-scope targets, 1/4 original incident out-of-scope`. Verified by Phase 1b retrospective lookup against `~/git/ART/.beads/dolt/ART/` (916 issues, queried via `dolt sql`). **T2**: `nexus_rdr/066-research-4-ca5b-retrospective` (id 734).
  — **Per-coordinator result**: RDR-073 `ART-sift` 2 blocking deps ✓ | RDR-075 `ART-cam3.5` 2 blocking deps ✓ | RDR-031 `ART-5iry` 3 blocking deps ✓ | **RDR-036 `ART-9z2p` 1 blocking dep ✗**.
  — **Deeper finding**: the 1/4 miss is NOT a detection gap between fallback and full lookup — it is a **framework scope boundary**. RDR-036 is an *intra-class* HashMap short-circuit (FactualTeacher.query delegating to a local HashMap instead of the resonance path). Neither the fallback heuristic nor the full CA-5 method-ownership lookup would catch it, because there is no composition of ≥2 prior beads' outputs happening. FactualTeacher's description names methods defined *within FactualTeacher itself*, not methods from other beads. Un-deferring CA-5 full would NOT improve the catch rate on this target — the gap is in the failure mode taxonomy, not the detection heuristic.
  — **Implication for 4/4 claim**: the nexus audit's "4/4 catch" ceiling was **over-scoped** by conflating inter-bead composition failures with intra-class short-circuits. The correct framework ceiling on the historical target set is **3/4** (RDR-073 + RDR-075 + RDR-031 as inter-bead composition failures the probe addresses; RDR-036 as a distinct intervention target). See §Research Findings §Finding 3 for the revised catch-rate claim and the §Trade-offs §Consequences update.
  — **Re-attribution note**: RDR-036 should re-attribute to RDR-068 dimensional contracts. If contracts declare `FactualTeacher.query` returns "resonance-cascade output" and the static type check fails on the HashMap's direct return, RDR-068 catches the failure. This re-attribution is relevant to RDR-068's scope justification and should be raised there.
  — **CA-5 full disposition confirmed**: deferral remains valid. CA-5 full was deferred on cost grounds (requires architectural rework) — Phase 1b confirms it is also *structurally unhelpful* for the remaining in-scope failure mode, so the deferral is doubly justified. No un-deferral needed.

## Proposed Solution

### Approach

Three components, built in order:

1. **Coordinator convention**: plan-enricher agent prompt gains a rule — "any bead whose implementation composes outputs from ≥2 prior beads (e.g., calls methods defined in bead M and bead N where M,N < this bead) must be tagged `metadata.coordinator=true` when the bead is updated." This is prompt-level, no new infrastructure. Enforced by the prompt, verified by the audit loop (RDR-067).

2. **`nx:composition-probe` skill**: new skill at `nx/skills/composition-probe/SKILL.md`. Takes a coordinator bead ID. Reads the bead description + the declared outputs of its dependencies. Dispatches a subagent (general-purpose, sonnet-class) with a fixed prompt: "generate a 30-50 line minimal end-to-end test against `<entry_point>` that exercises the composition with realistic input. Include assertions on the output shape and the dimensionality of intermediate values. Fail fast on any exception." The subagent generates the test, writes it to a temp file, runs it via the existing test runner (project-dependent — `mvn test`, `uv run pytest`, etc.), reports pass/fail + the test output.

3. **Workflow integration**: plan-enricher agent, when it closes enrichment on a coordinator bead, emits a required probe-run step in the plan description. The step is: "Before beginning the next bead, run `/nx:composition-probe <this-bead-id>` and verify PASS." Agents executing the plan see the step and run the probe. If the probe fails, the agent cannot proceed until the failing dependency bead is reopened (detected by the probe's output identifying which dependency's contract broke).

### Technical Design

> **Design locked on Read-only branch (post-Phase-1a)**. The Phase 1a hard-case spike (Finding 5, `nexus_rdr/066-research-5-ca1-ca2-ca3-hard-case-spike`) verified that a Read-only subagent is sufficient for nexus coordinator targets. `Any` at injection boundaries is the dominant nexus pattern — contracts are expressed as runtime dict-key presence, not typed generics requiring inference. Tool budget for the Phase 3 composition-probe skill: `Read + Grep + Glob + conftest-fixture heuristic`. Serena escalation is reserved for `typing.Protocol` / `TypeVar` cases that may appear in future targets but do not appear in the nexus + ART coordinator corpus verified so far.

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

- **Positive**: catches **3/4 historical audit incidents** at bounded reopen cost — the in-scope inter-bead composition failures (RDR-073 GroundedLanguageSystem coordinator, RDR-075 DevelopmentalCurriculum Phase 2 + CogEMEmotionalModulator, RDR-031 Full pipeline wiring). The 4th historical incident (RDR-036 FactualTeacher.query HashMap short-circuit) is an intra-class failure mode outside the probe framework's scope and should re-attribute to RDR-068 dimensional contracts. Actual catch rate is bounded below by (coordinator tag precision) × (probe step adherence) × (probe run integrity). Probe run integrity is VERIFIED (Phase 1a spike, Finding 5). See §Research Findings §Finding 3 for the revised 3/4 framing and the RDR-036 re-attribution
- **Positive**: surfaces the identity of the failing dependency bead, enabling targeted reopen rather than coordinator rewrite
- **Positive**: composable with RDR-065 (close-time replay), RDR-068 (contracts), RDR-069 (critic) — none overlap, all layer
- **Negative**: adds ~30-120 seconds per coordinator bead (not per bead; only coordinators)
- **Negative**: requires plan-enricher prompt discipline (CA-4 mitigation); missed coordinator tags mean missed probes
- **Negative**: probe subagent may generate flaky or low-quality tests on hard cases until CA-1/CA-2 are verified and the prompt is tuned
- **Negative** (fallback-heuristic specific): the `≥2 declared dependencies` fallback may under-tag coordinators whose plan did not declare all composition edges via `bd dep add`. CA-5b quantifies this structurally against the historical 4/4 target set

### Risks and Mitigations

- **Risk**: Probe false-positive rate is high on noisy targets (e.g., flaky integration points). **Mitigation**: the probe runs the subagent-generated test up to 3 times; only consistent failures block.
- **Risk**: Probe takes too long on hard cases. **Mitigation**: CA-3 spike; if latency unacceptable, narrow probe scope to dimensional assertions only (skip runtime execution).
- **Risk**: Plan-enricher misses coordinator tagging (CA-4). **Mitigation**: RDR-067 audit loop detects plans with undetected coordinators by cross-referencing bead dependency graphs.
- **Risk** (OBS-3 from gate Layer 3): **RDR-067 is the correction channel for fallback over-tagging, but RDR-067 is itself draft with unverified CAs.** If RDR-067 is deferred or de-scoped, there is no correction channel for coordinator over-tagging by the `≥2 deps` fallback heuristic — the false-positive rate is unbounded in that case, and the wasted probe runtime accumulates indefinitely. **Mitigation**: the CA-5b retrospective in Phase 1 narrows the expected false-positive rate by confirming the historical baseline. If CA-5b passes, the fallback is precise-enough that the RDR-067 dependency is soft. If CA-5b fails or partially passes, RDR-067 becomes a harder dependency and the user should consider whether to ship RDR-066 at all before RDR-067 has gated.
- **Risk** (SIG-1 from gate Layer 3): **The fallback heuristic may not reach the full lookup's coordinator set on the historical 4/4 target.** The RDR silently conflated structural dependency-count and semantic method-ownership as if they produced the same coordinator tagging in all cases. CA-5b is the test. **Mitigation**: the Phase 1 retrospective ART plan graph lookup is the verification path; the disposition rule in CA-5b spells out what to do if the fallback achieves 3/4 or 2/4 (consider un-deferring CA-5 full or narrowing the catch claim).

### Failure Modes

- Probe generation times out → fall back to advisory ("probe could not be generated; manual composition check required")
- Probe execution times out → surface as failure with the timeout as the reason
- Probe flaky (passes 2/3 runs) → surface ambiguity to user; do not block but warn
- Coordinator tagging missed → no probe runs; caught later by RDR-067 audit loop; no data loss but reduced effectiveness until corrected
- **Probe fails but failure output is unintelligible** (CA-2 fallback) → the probe surfaces the raw test output, does not attempt auto-attribution, flags the coordinator bead for manual investigation. The subagent prompt explicitly instructs "If you cannot attribute the failure to a specific dependency bead, say so explicitly rather than guessing."
- **Probe step skipped** (user or agent ignores the text instruction in the enriched bead description) → no probe runs; caught only by RDR-067 audit loop or by RDR-069 critic at close time. Acknowledged as a known limitation of the convention-based Gap 3 closure; a future RDR may add a structural hook.

## Implementation Plan

### Prerequisites

- [x] RDR-069 (Phase 0) shipped — the critic is the safety net if this probe misses; build the net first. **Satisfied 2026-04-11** (PR #147 merged to main).
- [x] CA-1, CA-2, CA-3 verified via Phase 1a runtime hard-case spike. CA-1 `READ-ONLY-SUFFICIENT`, CA-2 `INTERPRETABLE`, CA-3 `WITHIN-BUDGET` (1.93s execution). Target: `src/nexus/search_engine.py:149` (`search_cross_corpus`, cross-file composition). Phase 3 architecture decision: Read-only subagent (minimal tool budget). **T2**: `nexus_rdr/066-research-5-ca1-ca2-ca3-hard-case-spike`.
- [x] CA-5b resolved via Phase 1b retrospective ART plan graph lookup. `STRUCTURALLY ADEQUATE — 3/3 on in-scope targets`. RDR-036 identified as out-of-scope (intra-class failure mode, not inter-bead composition). 4/4 claim revised to 3/4. **T2**: `nexus_rdr/066-research-4-ca5b-retrospective` (id 734).
- [x] CA-4 `FEASIBLE-WITH-DIFF` verified by code analysis (Finding 4, id 730). Verified outcome feeds into Phase 2 as the prompt-diff specification. Phase 2 implementation still must demonstrate end-to-end runtime correctness including the silent-omission negative test (Scenario 5b).
- [x] CA-5 fallback `ZERO-COST` verified by code analysis (Finding 4, id 730). Feeds Phase 2.
- [x] **Bead-level ordering enforcement**: Satisfied trivially — RDR-069 shipped to main before RDR-066 implementation began (PR #147, commit 5a7fa60). The pattern-level discipline it expresses remains documented for future arcs where RDRs are developed in parallel.

### Minimum Viable Validation

Run the probe against a synthetic coordinator bead constructed from ART's RDR-073 IMPL-04 retrospectively. The probe should generate a test against `GroundedLanguageSystem.dialog().process("ball")` with the trained pipeline, run it, and surface `IllegalArgumentException: input length 312 != state size 65`. This is a known-failure target with a known-correct answer.

### Phase 1: Hard-case spike + CA-5b retrospective

Phase 1 has two parallel sub-tasks. Both feed Phase 2/3 implementation decisions.

**Phase 1a — Runtime hard-case spike (CA-1, CA-2, CA-3)**

- Pick a cross-file generic composition from the nexus or ART codebase (e.g., a coordinator that composes `Pipeline<T>` and `Projection<U>`)
- Dispatch the proposed probe-generation subagent with a fixed prompt
- Measure: did the subagent need Serena / symbol lookup? How long did generation take? Was the generated test accurate? Was the failure message interpretable?
- Outcome: decide Read-only vs. Read+Serena architecture; document in CA-1/CA-2/CA-3 dispositions

**Phase 1b — CA-5b retrospective (fallback-to-full equivalence on the historical 4/4 target)**

- For each of the four historical ART coordinator beads (IMPL-04 from RDR-073, DevelopmentalCurriculumIntegration from RDR-075, FactualTeacher.query from RDR-036, Step 5 pipeline from RDR-031), determine how many declared `bd` dependencies the coordinator bead had at the time of the incident
- Sources (in order of preference): (a) `~/git/ART/.beads/` archive + `bd show <id> --json` if the bead IDs can be recovered from the post-mortems, (b) grep the ART post-mortems for `--waits-for` declarations around the coordinator bead, (c) read the original plan document if preserved in `~/git/ART/docs/plans/` or equivalent
- Outcome: record counts per coordinator in a CA-5b disposition entry in T2 (`nexus_rdr/066-research-4-ca5b-retrospective`). Apply the disposition rule from the CA-5b definition — if all four have ≥2 declared deps, CA-5b passes and the 4/4 claim holds under the fallback. If 1-2 have <2, adjust the catch rate claim in §Trade-offs §Consequences and reconsider whether CA-5 full should be un-deferred
- Latency: this is a cheap task — no runtime spike, no subagent dispatch, just CLI + grep. Estimate ≤30 minutes total.

### Phase 2: Coordinator convention + plan-enricher update

**Scope shift (per Finding 4)**: Phase 2 is a near-zero-cost prompt diff, not a moderate rewrite. The enricher already reads `.dependencies` from `bd show --json` in its existing per-bead walk; the detection heuristic is a single prompt sentence consuming data already in the read path.

- Add the detection rule to `nx/agents/plan-enricher.md` near line 100: "After reading the bead via `/beads:show <id> --json`, inspect `.dependencies`. If the count is ≥2, treat this bead as a coordinator candidate." (The `--waits-for` edge-count fallback heuristic — ships the 80% value at zero marginal cost.)
- Extend the `/beads:update` call near line 134 to append `--metadata '{"coordinator": true}'` when the coordinator candidate heuristic fires.
- Add a post-write verification step: after `/beads:update`, call `/beads:show <id> --json` and assert `.metadata.coordinator == true` was persisted. On failure, surface to the user. (CA-4 silent-omission mitigation — the enricher can successfully write the body update and silently omit the metadata flag with no error.)
- Emit the probe-run step in the enriched bead description: "Before beginning the next bead, run `/nx:composition-probe <this-bead-id>` and verify PASS."
- Document the `metadata.coordinator=true` convention in the enricher prompt header so future readers understand the semantic.
- **Happy-path test**: run plan-enricher on a small test plan with a known coordinator (≥2 deps); verify tag, post-write verification, and probe step are all emitted.
- **Negative test (SIG-2 mitigation)**: run plan-enricher in a failure-injection harness where the `--metadata` flag is deliberately stripped from the generated `bd update` call (simulate the silent-omission failure mode by mocking the update command, patching it out at the shell layer, or overriding the prompt with a variant that omits the flag). **Expected outcome**: the post-write verification step catches the missing tag via `bd show --json | .metadata.coordinator` and surfaces failure to the user explicitly. **Gate**: this test must fail loudly if the mitigation step is stripped from the prompt — it is the load-bearing negative test for CA-4. Without this test, the post-write verification could ship as dead code (present in the prompt text but never exercised), which is exactly the "building blocks correctly implemented but not wired" failure mode RDR-066 is designed to prevent.

**Deferred**: the full CA-5 cross-bead method-ownership lookup heuristic is out of scope for Phase 2. Tracked as a follow-on bead under the RDR-066 implementation epic. The fallback heuristic ships first; the richer detection is an optional upgrade after shipping.

**Acknowledged false-positive risk**: the edge-count heuristic over-tags fan-in beads that depend on many siblings without actually composing their outputs in failure-prone ways. Over-tagging causes wasted probes, not missed failures — the 4/4 catch claim is unchanged. Correction channel: RDR-067 audit loop catches coordinator-tagged beads whose probes never catch real failures.

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

- **Scenario 1** (Phase 1a spike): cross-file generic composition; does the subagent need Serena; is the probe accurate; is latency bounded
- **Scenario 2** (RDR-073 retrospective): known-failure target; verify probe catches the 312D/65D mismatch
- **Scenario 3** (clean coordinator): a coordinator bead whose composition actually works; verify probe returns PASS without false positive
- **Scenario 4** (RDR-036 retrospective): verify probe would have caught the HashMap short-circuit
- **Scenario 5** (CA-4 — missed tag happy path): run plan-enricher on a plan with a coordinator that plan-enricher doesn't recognize; measure the miss rate
- **Scenario 5b** (CA-4 silent-omission negative test — SIG-2 from gate Layer 3): run plan-enricher in a failure-injection harness where the `--metadata` flag is deliberately stripped from the generated `bd update` call. **Expected**: the post-write verification step catches the missing `metadata.coordinator` field via `bd show --json` and surfaces failure loudly to the user. **Gate**: if the mitigation step is dead code (present in prompt text, never exercised), this test must fail. This is the load-bearing negative test for the CA-4 silent-omission mitigation — without it, the mitigation could ship broken.
- **Scenario 5c** (CA-5b retrospective — SIG-1 from gate Layer 3): for each of the four historical ART coordinator beads (RDR-073 IMPL-04, RDR-075 DevelopmentalCurriculumIntegration, RDR-036 FactualTeacher.query, RDR-031 Step 5 pipeline), count declared `bd` dependencies at the time of the incident. **Expected**: each coordinator has ≥2 declared deps (confirming fallback-to-full equivalence on the historical target set) OR record the failing coordinators and apply the CA-5b disposition rule (narrow the 4/4 claim or un-defer CA-5 full). Cheap — no runtime spike, just `bd list --json` + grep or ART post-mortem read.
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

- **Phase 1a** (runtime hard-case spike): CA-1, CA-2, CA-3 must be verified before Phase 3 implementation begins
- **Phase 1b** (retrospective ART plan graph lookup): CA-5b must be verified before Phase 2 implementation; its outcome feeds the catch-rate claim language
- **Phase 2** (implementation): CA-4 is verified end-to-end by Scenario 5 (happy path) AND Scenario 5b (silent-omission negative test). CA-5 fallback is verified end-to-end by Scenario 5.
- **Gate**: CA-1, CA-2, CA-3, CA-4 (analysis), CA-5 fallback (analysis), CA-5b (retrospective) must all be verified before the RDR-066 close flow accepts `close_reason: implemented`. CA-5 full remains deferred throughout; CA-5b failure triggers un-deferral on 1-2 targets rather than a full-scope expansion.

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
- 2026-04-11 (third iteration) — **Critic-driven fixes** from the RDR-069 CA-1 determinism spike (`nexus_rdr/069-research-2-ca1-ca3-critic-determinism-spike`). Two runs of `nx:substantive-critic` against this RDR surfaced issues the second iteration missed. Fixes applied: (a) retained-CA section added to track the prior-iteration findings (bd metadata, easy-case contracts) that were verified but not numbered; (b) CA-5 added for the coordinator detection heuristic's cross-bead method-ownership lookup requirement; (c) §Technical Design marked as conditional on CA-1 outcome (Read-only vs Read+Serena branches); (d) CA-2 fallback design added for the unattributed-failure case; (e) §Failure Modes extended with probe-step-skipped and unintelligible-failure cases; (f) §Gap 3 acknowledges convention-not-hook closure explicitly; (g) Implementation Plan Prerequisites adds bead-level ordering enforcement via `bd dep add`; (h) Phase 5 step labels renamed 6a/6b/6c → 5a/5b/5c; (i) 4/4 claim qualified as "conditional on correct tagging"; (j) duplicate §Alternatives Considered renamed to §Briefly Rejected Alternatives. Bead: nexus-57j.
- 2026-04-11 (fourth iteration — Finding 4) — **CA-4/CA-5 feasibility analysis** via `nx:codebase-deep-analyzer` against current `plan-enricher.md` and `enrich-plan/SKILL.md`. Outcome: CA-4 `FEASIBLE-WITH-DIFF` (prompt-only, silent-omission mitigation needed); CA-5 full `REQUIRES-REWORK` (deferred to follow-on bead); CA-5 fallback `ZERO-COST` (data already in enricher's read path). Phase 2 scope shifts from "moderate rewrite" to "near-zero-cost prompt diff". T2: `nexus_rdr/066-research-3-ca4-ca5-enricher-feasibility` (id 730).
- 2026-04-11 (fifth iteration) — **Finalization gate run** (`/nx:rdr-gate 066`) returned PASSED (0 Critical) but surfaced 2 Significant findings and 4 Observations via Layer 3 critic dispatch. Fixes applied: (a) §Research Findings §Finding 3 4/4 qualification tightened to four conditional factors including the new CA-5b; (b) §Finding 4 "fallback reaches same coordinator set" claim acknowledged as structurally unvalidated — added explicit under-tagging failure mode analysis; (c) CA-5b added to gating CA set ("the four historical ART coordinator beads each have ≥2 declared `bd` dependencies"); (d) §Trade-offs §Consequences "catches 4/4" unconditionally → "catches up to 4/4" with full conditional chain; (e) §Risks extended with RDR-067-deferred-no-correction-channel risk (OBS-3) + fallback-heuristic coordinator-set under-tagging risk (SIG-1); (f) Phase 1 split into 1a (runtime hard-case spike for CA-1/CA-2/CA-3) and 1b (retrospective ART plan graph lookup for CA-5b); (g) Phase 2 happy-path test + SIG-2 negative test for CA-4 silent-omission mitigation made explicit; (h) Test Plan gains Scenario 5b (silent-omission negative test) and Scenario 5c (CA-5b retrospective lookup); (i) §Finalization Gate §Assumption Verification fixes CA-4 phase misattribution (was "Phase 1 spike", now "Phase 2 implementation + Scenario 5b"); (j) Prerequisites now reflects RDR-069 shipped (satisfied) and CA-4 + CA-5 fallback verified by analysis, with remaining unchecked CAs explicit. Gate T2 record: `nexus_rdr/066-gate-latest`. Re-gate against the 5th iteration returned `justified` (0 Critical, 0 Significant, 3 informational Observations, all explicitly non-blocking).
- 2026-04-11 (sixth iteration — **this one**, Phase 1a/1b findings integration) — **Phase 1 execution landed two material findings** via parallel runtime spike (Phase 1a, Finding 5) and retrospective ART plan graph lookup (Phase 1b, CA-5b resolution). Fixes applied: (a) §Research Findings §Finding 3 rewritten — the nexus audit's "4/4 catch" ceiling was over-scoped by conflating inter-bead composition failures (RDR-073, RDR-075, RDR-031 — probe-catchable) with intra-class short-circuits (RDR-036 FactualTeacher.query HashMap — out of probe framework scope). Revised ceiling is 3/4. RDR-036 re-attributed to RDR-068 dimensional contracts as the appropriate intervention; (b) new §Finding 5 (Phase 1a Read-only sufficiency spike against `search_engine.search_cross_corpus`) documenting the CA-1/CA-2/CA-3 verification and the Phase 3 tool-budget decision; (c) CA-1 flipped from Unverified → `READ-ONLY-SUFFICIENT`, CA-2 → `INTERPRETABLE`, CA-3 → `WITHIN-BUDGET` (1.93s execution), all verified by Finding 5; (d) CA-5b flipped from Unverified → `STRUCTURALLY ADEQUATE — 3/3 on in-scope targets` with explicit deeper finding about intra-class vs inter-bead failure mode taxonomy; (e) §Finding 4 trade-off language updated to reflect that CA-5 full is now *doubly* deferred (cost + scope — it wouldn't help on the historical targets); (f) §Trade-offs §Consequences first bullet: "up to 4/4" → "3/4 historical audit incidents" with the RDR-036 re-attribution note; (g) §Technical Design note locked on Read-only branch (was conditional on CA-1 outcome, now decided); (h) §Prerequisites checklist — all CAs now checked, with Phase 1a/1b outcomes and T2 record references inline. T2 research records: `nexus_rdr/066-research-4-ca5b-retrospective` (id 734), `nexus_rdr/066-research-5-ca1-ca2-ca3-hard-case-spike`. Beads closed: nexus-9ps, nexus-ssp. Ready set: nexus-3k9 (Phase 2) + nexus-2pl (Phase 3) now both unblocked.
