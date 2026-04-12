---
id: RDR-068
title: "Dimensional Contracts at Enrichment"
type: process
status: closed
closed_date: 2026-04-11
closed_reason: won't-ship
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

This RDR is **belt-and-suspenders** on top of RDR-066's composition smoke probe, with a revised priority rationale as of 2026-04-11 (RDR-066 Phase 1b finding):

- **RDR-066 probe** catches **3/4** audit incidents (RDR-073, RDR-075, RDR-031 — the inter-bead composition failures). The 4th (RDR-036 FactualTeacher.query HashMap short-circuit) is out of the probe framework's scope — it's an **intra-class** failure mode where the method signature is satisfied but the implementation silently delegates to a HashMap lookup instead of the composed resonance path. Source: `nexus_rdr/066-research-4-ca5b-retrospective` (id 734).
- **Dimensional contracts** catch **at least 1/4 cleanly** (RDR-073 dim mismatch) and **possibly 2/4** if RDR-036's intra-class short-circuit can be expressed as a declared-return-type contract mismatch (e.g., declared "resonance-cascade output" vs actual "raw HashMap value"). The 2/4 extension is contingent on contract expressiveness for return-shape-vs-return-value semantics — see CA-068-5 (pending verification) for the hard case.
- Net effect: **the probe addresses the inter-bead failure class (3 incidents), contracts address the intra-class failure class (1 incident, possibly 2)**. The two layers are complementary, not duplicative. Priority remains P3 because the probe is the larger payoff on absolute catch count, but contracts now have a non-trivial standalone value justification (catch a class the probe cannot) beyond the "belt-and-suspenders on the 1/4 overlap" framing.

Dimensional contracts provide a cheap additional layer that surfaces errors earlier — at plan time, not at coordinator-bead probe time — and target the intra-class failure class that the probe structurally cannot address.

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

### Finding 1 (2026-04-11): Easy-case contract generation verified — retained from prior iteration

> **Label disambiguation (critic-driven fix 2026-04-11)**: the source T2 entry for this finding is titled `nexus_rdr/066-research-2-ca2-spike-verified` (id 715) — that spike was authored against RDR-066's CA numbering, where CA-2 was the easy-case contract generation question. **In RDR-068's CA numbering, the same evidence satisfies CA-068-1** (the easy case of hard-case contract generation — see renumbered CAs below). The spike content is valid; the label was re-purposed without re-numbering when this RDR was reissued. See Critical Issue 1 / CA namespace collision in the 2026-04-11 critic findings (`nexus_rdr/069-research-5-ca1-rdr068-n4`).

Source: `nexus_rdr/066-research-2-ca2-spike-verified` (written during the prior iteration; retained because the finding is still valid under the new scope).

A Sonnet-class subagent produced a fully line-grounded 11-field contract for `src/nexus/frecency.py:compute_frecency` (132-line self-contained Python function) from a single `Read()` call. HIGH confidence on all fields except "propagating exceptions" (correctly self-flagged as lowest confidence — inferred from absence-of-handler).

Template refinements surfaced by that spike (applied here):
1. Split "Error modes" into sub-bullets: **caught** (with handler citation) and **propagates** (uncaught, propagates to caller)
2. Add "Default values resolved" field — inline constant values not just symbolic references
3. Retain "Tools used to ground" and "Hallucination self-check" fields for auditability

### Finding 2 (2026-04-11, from the audit): 1/4 incidents cleanly caught by dimensional contracts

Source: `rdr_process/nexus-audit-2026-04-11`.

Of the 4 confirmed ART incidents (coverage updated 2026-04-11 per RDR-066 Phase 1b re-attribution):

- **RDR-073** (312D/65D dim mismatch): A dimensional contract on `SemanticGroundingLayer.process` (input_shape: `(batch, DEFAULT_SEM_DIM=65)`) and on `PhonemicWordPipeline.lastProcessedWordVector()` (output_shape: `(312,)`) would have surfaced the mismatch at enrichment time. ✓ **Caught cleanly** by contracts AND by the probe. Overlap case.
- **RDR-075** (InstarLearning structurally dead): The classes and method signatures were correct. The contract would say `InstarLearning.apply(state) -> void` and be satisfied — it IS applied, just in the wrong factory. ✗ **Not caught by text contracts** (signature OK). Caught by the probe (inter-bead composition failure — the production factory doesn't invoke InstarLearning).
- **RDR-036** (HashMap short-circuit): The `FactualTeacher.query` method signature is `String -> String`. The basic contract is satisfied — it returns a string. **Per RDR-066 Phase 1b (T2 `066-research-4-ca5b-retrospective` id 734), this is an intra-class failure that the probe framework CANNOT catch** (FactualTeacher's bead description names methods within FactualTeacher itself, not from other beads; no inter-bead composition to probe). It re-attributes to this RDR as a **candidate for extended-contract coverage**: if contracts express "declared return semantics" (e.g., "returns a resonance-cascade output") beyond "declared return type" (e.g., "returns String"), the HashMap short-circuit becomes a contract violation. Whether this is feasible is tracked as **CA-068-5** (intra-class semantic contracts). ✓/✗ **Contingent on CA-068-5** — feasibility pending verification.
- **RDR-031** (building blocks only, pipeline not swapped): Same pattern as RDR-075 — the contracts on individual methods are correct; the integration point (the place where the pipeline should have swapped) is below the contract level. ✗ **Not caught by text contracts**. Caught by the probe (inter-bead composition — the pipeline wiring is what a probe exercises).

**Revised coverage counts** (post RDR-066 Phase 1b):

- **Probe** (RDR-066) catches **3/4** inter-bead composition failures (RDR-073, RDR-075, RDR-031). RDR-036 is out-of-scope for the probe framework entirely.
- **Contracts** (this RDR) catch **1/4 definitively** (RDR-073 — the dim mismatch) with potential coverage of **2/4 contingent on CA-068-5** (if RDR-036's intra-class short-circuit can be expressed as a declared-semantics contract).
- **Complementary, not overlapping**: the probe and contracts address **disjoint failure classes** (inter-bead composition vs. intra-class semantic contracts). The overlap is only the RDR-073 dim mismatch.

Contracts remain a **belt-and-suspenders layer** on the RDR-073-class overlap AND a **primary intervention** on the RDR-036-class intra-class failure mode (contingent on CA-068-5). Priority remains P3 because the absolute catch count is smaller than RDR-066's, but the unique coverage of the RDR-036 class gives contracts standalone value beyond the overlap.

### Finding 3 (2026-04-11): CA-068-1 FAILS for Read-only — hard case requires Serena

Source: `nexus_rdr/068-research-3-ca1-hard-case-fails-read-only` (T2 id 763).

Five hard-case targets in `src/nexus/` tested — all require symbol resolution:

| Method | File:Line | Hard-case reason |
|--------|-----------|-----------------|
| `search_cross_corpus()` | `search_engine.py:149` | Returns cross-file `SearchResult`, opaque `t3: Any` wrapping chromadb |
| `_fetch_embeddings_for_results()` | `search_engine.py:261` | `np.ndarray` with `(N, emb_dim)` shape, dynamic import |
| `_build_chunk_metadata()` | `pipeline_stages.py:119` | Opaque `chunk: Any` (TextChunk from another module), 26+ key dict |
| `extractor_loop()` | `pipeline_stages.py:53` | Cross-file `ExtractionResult` dataclass, `PipelineDB` composition |
| `get_embeddings()` | `db/t3.py:336` | `np.ndarray` with `(N, D)` shape contract, chromadb wrapping |

**Design branch taken: Read+Serena.** Plan-enricher subagent needs `jet_brains_find_symbol` for cross-file type resolution, third-party schema lookup, and implicit shape contract grounding.

### Finding 4 (2026-04-11): CA-068-3 latency extrapolation confirmed — scope narrowing required

Source: `nexus_rdr/068-research-4-ca3-latency-realistic-extrapolation` (T2 id 764).

Codebase analysis of 697 functions: 68% easy-case, 31% hard-case. But enriched beads skew hard (integration points like `search_engine.py` are 62% hard). Scenario estimates at 60% hard-case weighting:

| Plan Size | Methods | Estimated Time |
|-----------|---------|----------------|
| 5 beads × 5 methods | 25 | ~40 min |
| 10 beads × 6 methods | 60 | ~95 min (1.6h) |
| 20 beads × 8 methods | 160 | ~253 min (4.2h) |

CA-068-1 failure (Read+Serena) adds unknown Serena latency on top. Phase 1 spike must measure on a real 5-10 bead plan. Scope narrowing options: cross-bead methods only (~50% reduction), easy-case only, caching, or batch/parallel generation.

### Finding 5 (2026-04-11): CA-068-2/4/5 cross-bead analysis — dependency chain identified

Source: `nexus_rdr/068-research-5-ca2-ca4-ca5-cross-bead-analysis` (T2 id 765).

**CA-068-2 (cross-bead comparability)**: CONTINGENT on CA-068-4. Enricher architecture supports it (Step 2 reads all beads, ~20K tokens for contract registry fits in-context). Numerical dim mismatches are mechanically detectable. But without provenance fields, method-to-method mapping requires LLM inference (same problem as RDR-066 CA-5).

**CA-068-4 (provenance fields)**: LIKELY PASSES. Three auto-population sources: bead dependency edges, method names from descriptions, "Calls out to" contract field. Mechanical linking path exists.

**CA-068-5 (semantic contracts)**: CONTINGENT — reframed. Three nexus analogues confirm the semantic field CAN express path distinctions (`_embed_with_fallback`, `resolve_path`, `_prefilter_from_catalog`). But verification still requires reading the method body. **Reframe**: semantic contracts are verification specifications for the critic/probe, not standalone detectors. The 2/4 catch rate claim becomes "provides detection specifications" not "detects independently."

**Cross-CA dependency chain**: CA-068-4 enables CA-068-2 (provenance → mechanical comparison). CA-068-5 is orthogonal (different failure class). CA-068-2 + CA-068-4 are load-bearing for the 1/4 definitive catch.

### Critical Assumptions

**Namespace note (critic-driven fix 2026-04-11)**: CAs are prefixed with the RDR number to avoid collision with RDR-066's and RDR-069's CA labels. The same spike evidence (T2 id 715) verifies a different CA in each RDR because the RDRs ask different questions. When referencing "CA-1 verified" across RDRs, always include the RDR prefix.

- [x] **CA-068-0 — Easy-case contract generation** (retained from prior iteration): LLM can produce grounded dimensional contracts for self-contained plain-type functions from a single `Read()` call. **Status**: VERIFIED (2026-04-11) via T2 id 715. **Scope**: plain types, self-contained functions, same-file helpers only. Hard case tracked as CA-068-1 below.

- [x] **CA-068-1** (hard case): LLM can produce grounded dimensional contracts for cross-file generics, protocols, and third-party library types without requiring runtime symbol resolution. The easy case (CA-068-0) is verified; the hard case is not.
  — **Status**: FAILS FOR READ-ONLY (2026-04-11) via T2 id 763. All 5 hard-case targets tested (`search_cross_corpus`, `_fetch_embeddings_for_results`, `_build_chunk_metadata`, `extractor_loop`, `get_embeddings`) require symbol resolution for cross-file types, third-party library schemas (chromadb, numpy, voyageai), and implicit shape contracts. **Design branch taken: Read+Serena.** The plan-enricher subagent prompt must gain `jet_brains_find_symbol` instructions; tool budget increases.
  — **Implication for §Technical Design**: CA-068-1 fails → the subagent architecture shifts from Read-only to Read+Serena. See the "Design branches on CA-068-1 outcome" note in §Technical Design. The hard-case latency multiplier (CA-068-3) is now higher than the 2-5x estimate because Serena calls add unknown overhead.

- [ ] **CA-068-2**: The contracts surfacing the dim-mismatch pattern are discoverable in a cross-bead comparison — i.e., a tool (or plan-auditor agent, or human reader) can look at bead 4's contract and bead 5's contract and see the mismatch without being told where to look. If contracts are only comprehensible in isolation, they don't help — and contracts become the ceremonial boilerplate failure mode the §Trade-offs section warns against.
  — **Status**: CONTINGENT ON CA-068-4 (2026-04-11) via T2 id 765. The enricher architecture supports cross-bead comparison: Step 2 reads ALL beads before Step 3 enriches sequentially, so a contract registry (~20K tokens for 20 beads × 5 methods) fits in-context. Numerical dim mismatches are mechanically detectable via string comparison on Shape constraint fields. **But**: without provenance fields (CA-068-4), the enricher knows "bead B depends on bead A" but NOT "bead B's method Y consumes bead A's method X's output" — same cross-bead-lookup problem as RDR-066 CA-5. False-positive risk: LOW for target failure class (shape constraint adds specificity beyond type). **Still needs retrospective test on RDR-073 to move to VERIFIED.**
  — **Implication for §Technical Design**: this is the load-bearing assumption for RDR-068's entire value proposition. If CA-068-2 fails, the template ships as boilerplate and the 1/4 catch rate collapses to 0/4. The Technical Design has a design branch on this assumption — see "Design branches on CA-068-2 outcome" below.
  — **Phase sequencing consequence**: **Phase 2 (template + skill update) must NOT ship before CA-068-2 is verified.** This is enforced via `bd dep add <068-phase-2-bead> <068-phase-1-bead>` in the Implementation Plan — prose prerequisite alone is not sufficient.

- [ ] **CA-068-3**: Contracts add an acceptable amount of enrichment time. The easy-case spike (id 715) took **~50 seconds for a 132-line function** with a single Read call. **Realistic extrapolation**: for a 20-bead plan with 5-10 methods per bead (100-200 contract generations), easy-case alone is **83-167 minutes** (1.4-2.8 hours). At the 2-5x hard-case multiplier, the upper bound is **5-14 hours**. The RDR previously stated "~5 minutes for 20 beads" as a cap — this is unrealistic by a factor of 17-40x.
  — **Status**: UNVERIFIED AT REALISTIC SCALE, extrapolation confirmed (2026-04-11) via T2 id 764. Codebase analysis: 697 functions in src/nexus/, 68% easy-case, 31% hard-case — but enriched beads skew hard (search_engine.py is 62% hard). Scenario estimates at 60% hard-case: 5-bead plan ~40min, 10-bead ~95min (1.6h), 20-bead ~253min (4.2h). CA-068-1 failure (Read+Serena required) adds unknown Serena latency multiplier on top. **Phase 1 spike must measure on 5-10 bead plan.** Scope narrowing options: (a) cross-bead methods only (~50% reduction), (b) easy-case only (defer hard), (c) cache generations, (d) batch/parallel generation.

**Added CA-068-4 (cross-bead provenance fields — from critic-driven finding 2026-04-11)**:

- [ ] **CA-068-4**: The contracts template has explicit cross-bead provenance fields — `**Provided by**: <bead-id>` on each input and `**Consumed by**: <bead-id>` on each output — that make mechanical mismatch detection possible without requiring plan-enricher to infer which beads compose which outputs. Without these fields, mismatch detection requires LLM inference and re-introduces the cross-bead-lookup problem from RDR-066's CA-5.
  — **Status**: LIKELY PASSES (2026-04-11) via T2 id 765. Analysis shows three info sources for auto-population: (1) bead dependency edges from `bd show --json` give bead-level graph, (2) method names from bead descriptions give cross-bead method references, (3) "Calls out to" contract field gives method-to-method linking. Auto-population path: for each input in B's contracts with a method reference → search blocking-dependency beads' contracts for matching method → annotate Provided by/Consumed by. Mechanical, no LLM inference needed. Risk: depends on bead description quality naming cross-bead method deps explicitly. **Still needs Phase 1 template test to move to VERIFIED.**

**Added CA-068-5 (intra-class semantic contracts — from RDR-066 Phase 1b re-attribution)**:

- [ ] **CA-068-5**: Dimensional contracts can express "declared return semantics" (what the function should *do*) beyond "declared return type" (what the function should *return*), so that intra-class short-circuit failures like RDR-036's HashMap lookup bypass become contract violations. The current 11-field contract template captures `Signature`, `Inputs`, `Outputs` with dimensional shape but NOT semantic-level contracts like "this method should reach the resonance path" or "this method should not short-circuit to a lookup table."
  — **Source**: RDR-066 Phase 1b (T2 `066-research-4-ca5b-retrospective` id 734) surfaced RDR-036 as an intra-class failure mode outside the composition probe's framework scope. RDR-066 §Finding 3 notes: "RDR-036 re-attributes to RDR-068 dimensional contracts as the appropriate intervention" if contracts can be extended to express declared semantics, not just declared types. This CA tests that claim.
  — **Status**: CONTINGENT — REFRAMED (2026-04-11) via T2 id 765. Three nexus analogues tested: `_embed_with_fallback` (doc_indexer.py:127, Voyage API vs zero-vector), `resolve_path` (catalog.py:480, 9 paths), `_prefilter_from_catalog` (search_engine.py:104, 8 paths). The semantic field CAN express the distinction between intended and short-circuit paths. **But verification requires reading the method body** — same cost as code review. **Critical reframe**: semantic contracts reduce verification from an open-ended question ("what should this do?") to a closed comparison ("does this match its contract?"). They are **verification specifications for the critic/probe**, not standalone detectors. The 2/4 catch rate claim should read: "contracts provide detection specifications for 2/4 incidents that enable other tools to detect them" — not "contracts detect 2/4 independently."
  — **Implication**: if CA-068-5 passes (in the reframed sense), RDR-068's catch rate rises from **1/4 definitive + 3/4 not caught** to **1/4 definitive + 1/4 specification-assisted + 2/4 not caught** on the historical target set. If CA-068-5 fails even in the reframed sense (semantic contracts too vague to constrain critic verification), contracts remain at 1/4.
  — **Fallback**: if CA-068-5 cannot be made robust (semantic contracts are too vague to be mechanically checkable), the RDR-036 re-attribution is rejected and the incident remains "caught by neither" — a known gap in the 4-RDR remediation cycle that a future RDR can address via a different intervention (e.g., mutation testing or runtime path assertions).

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

**Plan-enricher prompt update**: for each bead in the plan, after populating file paths and method names, walk each method and produce the contracts block using the template. If a method's contracts conflict with an earlier bead's contracts (e.g., bead 5 expects shape X but bead 3's contract says it produces shape Y), emit a `CONTRACT MISMATCH` **advisory warning** in the enrichment output. Mismatch warnings do NOT block enrichment completion by default — they are surfaced to the user for review, and the user decides whether to resolve before proceeding. Hard-blocking behavior may be added later once CA-068-2 is verified and false-positive rate is measured.

> **Design branches on CA-068-1 outcome.** The skill shape below describes the "CA-068-1 passes" branch (Read-only subagent, no Serena). If the Phase 1 hard-case spike shows cross-file generics / protocols / third-party types require symbol resolution, the architecture shifts: the subagent prompt gains `jet_brains_find_symbol` instructions, the tool budget increases. The design below is conditional.
>
> **Design branches on CA-068-2 outcome.** The mismatch detection mechanism below assumes contracts are cross-comparable. If CA-068-2 fails — contracts are interpretable individually but not cross-comparable — **Phase 3 mismatch detection is demoted to advisory human review only**, and §Trade-offs' "contracts become ceremonial" failure mode becomes the default. In that case, the RDR's value proposition reduces to "structured contracts as documentation aid" — which may not justify the enrichment-time cost (CA-068-3). If both CA-068-2 and CA-068-3 fail, the RDR should be closed as `partial` and its value scoped down to "contracts template exists, no automated detection."

**Mismatch detection (CA-068-2)**: during enrichment, after all beads have contracts, plan-enricher walks the contracts and produces a cross-bead summary: "Bead 3 produces `list[ChunkResult]`; Bead 7 expects `dict[str, ChunkResult]`; mismatch." **The walk requires cross-bead provenance fields on the contracts template** (see CA-068-4): each input has a `**Provided by**: <bead-id>` annotation and each output has a `**Consumed by**: <bead-id>` annotation. Without these explicit links, the walk requires LLM inference about which beads compose which outputs, which re-introduces the cross-bead-lookup problem from RDR-066's CA-5. Phase 1 adds these fields to the template before Phase 2 ships.

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

- [ ] RDR-066 (Phase 1 of the 4-RDR remediation) shipped — the probe is the primary layer; contracts are belt-and-suspenders on top
- [ ] **CA-068-2 verified via Phase 1 retrospective test on RDR-073 BEFORE Phase 2 (template + skill update) begins.** Enforced via `bd dep add <068-phase-2-bead> <068-phase-1-bead>` at plan time. Prose prerequisite alone is not sufficient — without bead-level enforcement, an implementer could ship Phase 2's template without the mismatch detection that justifies contracts existing at all, producing exactly the ceremonial failure mode §Trade-offs warns against.
- [ ] **CA-068-3 latency extrapolation** completed at realistic plan size (5-10 beads minimum) BEFORE Phase 2 ships, since the prior "~50s per function" easy-case number extrapolates to hours at realistic plan scale and may make the whole intervention cost-prohibitive.
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
- **4a**: synthetic dim mismatch injected into a test plan; verify plan-enricher catches via mismatch detection
- **4b**: substantive-critic on RDR-068
- **4c**: real self-close of RDR-068 via RDR-069 close flow

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
- **Scenario 5** (recursive 4a): inject a mismatch into a test plan; verify the new plan-enricher catches it

## Validation

### Testing Strategy

The RDR-073 retrospective MVV is the load-bearing test. Phase 1 spike determines whether the hard case is in scope for initial shipment or deferred.

### Performance Expectations

Easy-case contract generation: ~50 seconds per method (from T2 id 715 spike in prior iteration). Hard case: unknown, measured in Phase 1. **Realistic extrapolation** for a 20-bead plan with 5-10 methods per bead = 100-200 contract generations × 50s = **83-167 minutes** easy-case; 250-830 minutes (4-14 hours) at 2-5x hard-case multiplier. The prior "~5 minutes for 20 beads" cap was unrealistic by 17-40x. If the realistic-scale latency is prohibitive, Phase 2 must narrow scope (e.g., contracts only on methods that cross bead boundaries, skipping intra-bead private methods) OR cache contract generations across reruns OR scope the entire RDR to small plans (≤5 beads) only.

## Finalization Gate

### Contradiction Check

No contradictions. Contracts are a plan-time layer beneath the probe (RDR-066) and the critic (RDR-069); they catch a specific sub-pattern earlier.

### Assumption Verification

CA-068-1, CA-068-2, CA-068-3, CA-068-4 verified in Phase 1 spike. CA-068-0 retained from prior iteration as already verified. Cross-reference must use the RDR-068-prefixed CA labels to avoid collision with RDR-066's and RDR-069's CA numbering.

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

## Post-Mortem: Won't-Ship

**Closed 2026-04-11.** RDR-068 is closed as `won't-ship` after CA research killed the value proposition.

### Root cause: the proposal reduces to formal contract checking, which LLMs cannot do

Dimensional contracts at enrichment are a dressed-up version of a well-known hard problem: verifying that composed software components satisfy interface contracts at their boundaries. This is the domain of formal verification, dependent type systems, and contract-based design (Eiffel, Design by Contract, refinement types). Decades of PL research have produced tools for this — none of them are "have an LLM write a text specification and have another LLM check it later."

The RDR proposed exactly that: LLM-generated text contracts checked by LLM-mediated comparison. The enforcement chain has no mechanical ground truth at any point:

1. **Generation**: an LLM writes contracts grounded by reading code — but the grounding is best-effort, not proven
2. **Comparison**: string matching on shape fields works for the trivial numerical case (312 vs 65) but not for type compatibility, semantic equivalence, or behavioral contracts
3. **Verification**: checking an implementation against a semantic contract requires reading the method body — the same cost as a code review, with no reduction in effort

This should have been caught at proposal time. The "dimensional contracts" framing obscured the fact that the proposal was asking an LLM to do formal verification by another name. The easy case (CA-068-0, plain types) succeeded because it's trivial — a 132-line function with `float` and `int` parameters. The hard case (CA-068-1) failed for exactly the reason formal verification is hard: cross-module type resolution, third-party library schemas, and implicit shape invariants require a formal model of the program, not a text summary.

### What the research showed

- **CA-068-1**: All 5 hard-case targets require Serena symbol resolution. Read-only contract generation fails on real code.
- **CA-068-3**: 1.6-4.2 hours of enrichment time per plan, plus unknown Serena overhead. Cost is prohibitive relative to the 1/4 catch rate.
- **CA-068-5**: Semantic contracts can express path distinctions but cannot mechanically verify them. Reframed as "verification specifications" — but a specification that requires an LLM to check is not a contract, it's a comment.

### What survives

Nothing from this RDR ships. The probe (RDR-066) catches 3/4 incidents at runtime. The critic (RDR-069) catches what the probe misses at close time. Together they cover the space without the ceremony of LLM-generated contracts.

The 1 incident contracts uniquely target (RDR-073 dim mismatch) is also caught by the probe. The 1 incident contracts might extend to (RDR-036 intra-class short-circuit) requires semantic verification that reduces to code review.

### Lesson

Don't propose LLM-mediated enforcement for problems that require formal guarantees. If the enforcement mechanism is "an LLM checks text written by another LLM," the intervention is advisory documentation, not a contract system. Call it what it is and evaluate the cost accordingly — in this case, hours of enrichment time for advisory comments that the critic already produces for free.

## Revision History

- 2026-04-11 (closed) — **Won't-ship.** CA research confirmed the proposal reduces to formal contract checking via LLM, which is a known hard problem LLMs cannot solve. Cost (1.6-4.2h enrichment per plan) is prohibitive for a 1/4 catch rate on a failure class already covered by the probe (RDR-066) and critic (RDR-069). The easy case succeeds trivially; the hard case fails for the same reasons formal verification is hard. Post-mortem recorded.
- 2026-04-10 — Stub created as "Composition Failure Detection (Research)" targeting INT-3 (workaround gating) with a regex-bank research goal.
- 2026-04-11 — **Reissued with new scope**. The regex-bank research is obsoleted by the 2026-04-11 nexus audit (LLM classification beats regex for this task) and INT-3 is deferred indefinitely (retcon mechanism makes real-time detection harder than ART assumed). The RDR-068 number is repurposed for ART's INT-1 (dimensional contracts at enrichment) — the cheapest belt-and-suspenders layer on top of RDR-066's composition probe. Priority P3 (lowest of the four) because the probe catches 4/4 while contracts catch 1/4 cleanly. See `rdr_process/nexus-audit-2026-04-11` for evidence and bead `nexus-640` for the 4-RDR cycle.
- 2026-04-11 (third iteration) — **CA research spike**. All 5 open CAs investigated against the nexus codebase. CA-068-1: FAILS for Read-only (all 5 hard-case targets require Serena symbol resolution; design branch taken: Read+Serena). CA-068-2: CONTINGENT on CA-068-4 (enricher architecture supports it but needs provenance for method-to-method mapping). CA-068-3: extrapolation confirmed at 1.6-4.2h for 10-20 bead plans; scope narrowing needed. CA-068-4: LIKELY PASSES (three auto-population sources identified). CA-068-5: CONTINGENT — reframed as verification specifications for critic/probe, not standalone detectors. T2 ids 763-765.
- 2026-04-11 (second iteration) — **Critic-driven fixes** from RDR-069's CA-1 spike on this target (`nexus_rdr/069-research-5-ca1-rdr068-n4`, T2 id 722). Two runs of `nx:substantive-critic` against RDR-068 found 3 stable issues across runs and 4 additional correct single-run findings. Fixes applied: (a) CA namespace isolation — CAs renumbered to `CA-068-0` through `CA-068-4` to prevent collision with RDR-066's and RDR-069's CA labels; (b) new CA-068-4 added for the cross-bead provenance fields (`Provided by` / `Consumed by`) required for mechanical mismatch detection; (c) §Technical Design given explicit design branches on CA-068-1 (Read vs Read+Serena) and CA-068-2 (automated mismatch detection vs advisory human review); (d) §Finding 1 label disambiguated — the T2 spike at id 715 was authored under RDR-066's CA numbering but verifies RDR-068's CA-068-0; (e) `CONTRACT MISMATCH` behavior softened from "blocks enrichment" to "advisory warning" to match §Trade-offs; (f) Phase 4 sub-step labels renamed `6a/6b/6c` → `4a/4b/4c`; (g) §Prerequisites strengthened with `bd dep add` enforcement of CA-068-2 verification before Phase 2 ships (prevents shipping ceremonial template); (h) CA-068-3 latency extrapolation made realistic — "~5 minutes for 20 beads" replaced with "83-167 minutes easy case, 4-14 hours at hard-case multiplier" forcing Phase 2 to narrow scope or cache; (i) §Performance Expectations updated with the realistic extrapolation. Bead: nexus-sia.
