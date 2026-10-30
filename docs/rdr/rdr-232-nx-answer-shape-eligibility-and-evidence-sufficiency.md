---
title: "nx_answer Answer-Shape Eligibility and Evidence Sufficiency"
id: RDR-232
type: Architecture
status: accepted
priority: high
author: Sam
reviewed-by: self
created: 2026-10-23
accepted_date: 2026-10-25
related_issues: [nexus-l2vam, nexus-tko2w]
related: [RDR-078, RDR-080, RDR-091, RDR-097, RDR-189, RDR-196, RDR-200, RDR-203]
---

# RDR-232: nx_answer Answer-Shape Eligibility and Evidence Sufficiency

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

## Problem Statement

`nx_answer` chooses a saved retrieval plan before it executes that plan and
reduces the retrieved evidence into an answer. Today a plan can be semantically
similar to a question yet produce the wrong kind of answer. The evidence it
retrieves can also be plentiful without covering what the caller asked.

On 2026-10-23, a request asked for a synthesis of ART RDR-084: its locked
architecture, numerical conventions, evidence, gate history, recorded
implementation state, and open work. The call supplied `scope="rdr"` and
`bindings={"content_type": "rdr"}`. It selected plan 473,
`hybrid-factual-lookup`, whose description promises single-fact answers from
the best-matching document. The returned synthesis named the consolidation
theme but omitted every requested facet beyond that theme.

The structured result accumulated 340 chunk references: 339 from code
collections and one from an RDR collection. The final reduction said the
supplied RDR excerpt did not identify the canonical implementations. Direct T2
and T3 reads in the same session immediately returned those choices and the
gate history. Telemetry record 722 confirms plan 473, the six steps, and the
12,325-token generation input, but does not record per-stream counts or which
chunks reached the reducer. The accumulated references therefore show a
retrieval/evidence-selection problem; they do not establish that all 340
reached the reducer.

### Enumerated gaps to close

#### Gap 1: Semantic similarity admits plans with the wrong answer shape

The matcher filters scope, dimensions, unavailable bindings, identifier
conflicts, and historically failing plans. It does not test whether a plan's
declared output shape is compatible with the requested answer. A broad,
multi-facet synthesis request can therefore match a factual-lookup template.
Cost ranking then chooses among already-admitted candidates and cannot repair
that mistake.

The remedy is an **answer-shape contract**: a small declaration of the kind of
answer a plan produces. Plans incompatible with the request must be ineligible
before similarity or cost ranking. This is a routing constraint, not another
similarity weight. It does not guarantee coverage of individual facets; the
separate evidence-coverage gate handles those.

#### Gap 2: Plan corpus pins and traversal can dilute the caller's primary evidence

Two separate causes let out-of-scope evidence in.

First, the caller's scope never reaches a retrieval step whose plan already
names a corpus. The runner fills a step's corpus from caller scope only when
the step has none (`plans/runner.py:1184-1190`), so a plan-declared corpus
wins. All 12 builtin plans pin a corpus on their retrieval steps; ten of them
pin `corpus: all`, which fans out over every live collection family. Plan 473
pins it through its default binding, so on the source as read the observed
`scope="rdr"` call's vector search ran over every corpus (research 14; the
saved run's `resolved_step_args` was not read).

Second, RDR-097 deliberately expands factual lookup through citation and
relation links. The traversal primitive has no caller-scope argument. It
follows catalog links and returns up to a global cap of chunk ids from the
reachable documents. Hydration caps input count, not the share belonging to
the caller's requested corpus or named source. Consequently, linked code can
displace the RDR evidence that the caller explicitly scoped.

A bounded number of irrelevant chunks is not a sufficient fix. The system needs
to preserve primary evidence, constrain traversal to a declared evidence role,
and make any widening visible.

#### Gap 3: The reduction has no evidence-coverage gate

An answer can be well formed and still omit requested facets because the
retrieved evidence omitted them. Existing `answer_shape` classification detects
several non-answer terminal forms; it does not compare the request with the
retrieved evidence. Plan success likewise records execution outcome, not
whether the requested coverage was achieved.

The reduction needs a pre-generation evidence-coverage check and one bounded,
targeted repair opportunity. A missing facet must be reported honestly when
repair cannot fill it, rather than silently disappearing from an otherwise
plausible answer.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-078/080 | Origin | These established plan-centric retrieval and the `nx_answer` path. This RDR preserves the match-then-execute architecture and adds eligibility and evidence contracts. |
| RDR-091 | Precedent | Scope conflict is already a filter, separate from semantic score. Answer-shape compatibility belongs at the same eligibility layer, not in a larger cosine bonus. |
| RDR-097 | Origin of the observed traversal shape | Its per-stream caps bound factual-lookup context, but the caps do not reserve primary-source evidence or constrain graph expansion to the caller's scope. Its factual-lookup rationale remains valid for eligible requests. |
| RDR-189 | Shared contract, separate ownership | RDR-232 owns deterministic-first request analysis, routing eligibility, evidence containment, and bounded repair. RDR-189 owns durable answer-object families, target construction, host-bound witnesses, and post-realization preservation. RDR-232's kind is a versioned routing capability derived from the target family once that registry exists; its pre-reduction coverage ledger is input to P4's preservation trace, not proof of final obligation survival. RDR-189 P1 owns `check`/`verify` anchors. |
| RDR-196 | Precedent | Cost chooses among candidates only after correctness gates. Answer-shape eligibility precedes `choose_within_band`; it must not be traded against predicted cost. |
| RDR-200 | Precedent and boundary | It measured missing primary sources and retrieval crowding, and preserved evidence fidelity for continuation mode. This RDR addresses the same evidence problem before terminal reduction; it does not change who performs that reduction. |
| RDR-090/179 | Evaluation precedent | Existing retrieval and operator-quality harnesses supply baseline mechanics, not answer-shape or facet-coverage labels. RDR-232 adds those axes without treating synthetic NDCG as production answer quality. |

No separate RDR proposing these three coordinated fixes was found in the index.
RDR-189 is the closest conceptual overlap. It was accepted on 2026-10-24 with
a matching Shared Contract section; the boundary above is deliberate.

## Context

### Background

The observed call used the normal plan-match-first path. Plan 473 was the only
candidate offered to cost selection. Its execution was `search → traverse →
store_get_many → store_get_many → rank+generate`. All steps reported success;
there was no budget exhaustion, dropped reduction step, or reported truncation
of the reduction itself.

The evidence composition is a diagnostic clue, not a complete causal trace.
The call named an RDR and asked for several aspects of that document. Its
retrieval produced 339 code references and one RDR reference; the composition
that survived hydration and the operator cap is unrecorded. A direct lookup of
the named RDR supplied the missing material without running another model.
The implementation must record per-stream and reducer-input counts so this
causal gap is observable on future runs.

This RDR treats plan selection and evidence construction as separate failure
surfaces. Fixing only the matcher would still leave a correctly selected hybrid
plan capable of crowding out its primary evidence. Fixing only the evidence
would still make broad questions follow a factual-answer pipeline.

### Technical Environment

- **Plan matching**: `src/nexus/plans/matcher.py::plan_match` retrieves a small
  candidate pool, applies correctness filters, and ranks the survivors.
  `category_verb` supplies a separate route for builtin category plans.
- **Plan identity**: `src/nexus/plans/schema.py` canonicalizes dimensions;
  builtin YAMLs already carry `verb`, `scope`, `strategy`, description, and
  typed bindings. `answer_shape` is not a declared plan dimension today.
- **Cost choice**: `src/nexus/mcp/core.py` invokes `choose_within_band` only on
  candidates returned by the matcher.
- **Execution**: `src/nexus/plans/runner.py` resolves steps, applies caller
  scope only to retrieval steps that name no corpus, hydrates operator inputs,
  and bundles adjacent operators.
- **Traversal**: `src/nexus/mcp/core.py::traverse` accepts seeds, link types or
  purpose, depth, and direction. It has no corpus or primary-source reservation
  parameter. It returns document tumblers, chunk ids, and collections.
- **Hydration**: runner auto-hydration of an operator's `ids` preserves source
  notes and caps operator inputs at 100 after dropping empty content. The cap
  is positional; it has no per-corpus or per-document reservation. Operators
  fed explicit `inputs` from earlier `store_get_many` steps, as plan 473's
  `rank` is, have no count cap (research 17).
- **Reduction**: `src/nexus/plans/bundle.py` composes rank and generate into one
  model call and returns only the terminal schema. `answer_shape.py` classifies
  the final text afterward.
- **Run records**: RDR-203's composite record already stores the run and step
  records together. Routing and coverage summaries should extend that record,
  using its compatibility behavior for older engines.

## Research Findings

### Investigation

This draft used three sources of evidence:

1. The saved structured result of the ART RDR-084 `nx_answer` call on
   2026-10-23, including its final text, chunk collections, plan choice, step
   execution, and model usage.
2. Source reading on this RDR's base commit, `9a345da8b`, of the matcher,
   runner, traversal, hydration, bundle, plan schema, and builtin plan 473.
3. Prior RDRs on scope matching, hybrid factual lookup, target definition,
   cost-aware choice, continuation, and run records.

No new external dependency is proposed, so dependency-source verification is
not applicable. Internal interfaces still require the source verification
listed under Critical Assumptions before gate.

### Key Discoveries

- **✅ Verified** (spike) — Live `nx_answer` against ART RDR-084 on
  2026-10-23 selected plan 473 `hybrid-factual-lookup` for a multi-facet
  synthesis request with `scope=rdr` and `content_type=rdr`. The saved
  structured result contains 340 chunks: 339 code and one RDR. It omitted the
  requested canonical classes, numerical conventions, gate history,
  implementation state, and open work despite `answer_shape=answered`, no
  budget exhaustion, no dropped reduce steps, and a 12,325-token terminal
  generation input. Direct T2 `ART_rdr/084` and T3 RDR searches in the same
  session returned the missing decisions.
  *Source: saved run `mcp__plugin_conexus_nexus__nx_answer_14_563e4823.json`;
  `conexus/plans/builtin/hybrid-factual-lookup.yml`; independent direct T2/T3
  reads. T2: `nexus_rdr/232-research-1`.*

- **⚠️ Documented** (source search) — The evaluation can extend existing
  harnesses rather than create an ungrounded one. `docs/benchmarking.md`
  distinguishes `scripts/bench` retrieval NDCG@3/multi-hop scoring from the
  RDR-196 operator-output proxy. `bench/queries/spike_5q.yaml` holds five
  labeled gate-decision queries; RDR-090's full 30-query authoring and CI
  integration remain deferred. `tests/benchmarks/queries.json` is synthetic
  and is explicitly not a production quality proxy. RDR-232 needs additional
  labels for answer kind, required sources/facets, allowed corpora, and
  unacceptable plan matches, plus the ART RDR-084 regression.
  *Source: `docs/benchmarking.md`; RDR-179 §179-R20/R21;
  `tests/benchmarks/queries.json`. T2: `nexus_rdr/232-research-2`.*

- **Verified — wrong-shape admission.** Plan 473's own YAML says it is for
  single-fact answers and contrasts itself with theme/overview/summary
  questions. The matcher admitted it for a multi-facet synthesis because no
  answer-shape eligibility check exists. *Source: plan 473 YAML, run 722, and
  `plans/matcher.py`; research 1.*
- **Verified — caller scope does not reach plan-pinned retrieval steps.**
  `_apply_caller_scope_to_args` returns early when `"corpus" in args`, and it
  runs after the step's `$corpus` binding is resolved, so a plan-declared
  corpus always wins (`plans/runner.py:1184-1190`, `3093-3121`). All 12
  builtins pin a corpus on their retrieval steps: `hybrid-factual-lookup` and
  `abstract-themes` through a `corpus: all` default binding,
  `corpus-coverage-check` and `document-discovery` through a
  `knowledge,code,docs,rdr` default, and the other eight with a literal
  `corpus: all`. Plan 473's search step therefore ignored `scope="rdr"`.
  The saved run's `resolved_step_args` has not been read to confirm this for
  run 722. *Source: `plans/runner.py`; the 12 builtin YAMLs; research 14.*
- **Verified — traversal has no scope.** `traverse` does not accept a corpus
  argument, and its catalog walk collects every reachable collection.
  *Source: `mcp/core.py::traverse`; research 4.*
- **Verified — per-stream caps are not primary-source guarantees.** Plan 473
  reserves vector and graph budgets independently, then ranks their union.
  Neither its YAML nor `limit_per_source` expresses a caller corpus or named
  document. *Source: plan 473 YAML and `store_get_many`; research 4.*
- **Verified — accumulated evidence was unbalanced; reducer composition is
  unknown.** The structured result held 339 code references and one RDR
  reference despite the caller's RDR scope. This does not prove how many
  reached the reducer or that a cap displaced primary evidence.
  *Source: run 722 and the saved structured result; research 11.*
- **Verified — no reduction-budget explanation.** The observed run had no
  budget exhaustion, dropped reduce steps, or reduction truncation marker.
  Its terminal generation prompt used 12,325 input tokens, far below the
  bundle's character cap. *Source: run 722 and `plans/bundle.py`; research 1.*
- **Verified — success telemetry cannot represent this failure.** The run
  completed with `answer_shape="answered"`. Existing run/step success data does
  not assert that the named source or requested facets were covered.
  *Source: run 722 and `plans/answer_shape.py`; research 3.*
- **Assumed — a small shape vocabulary can reject clear mismatches without
  becoming a fragile question classifier.** The first vocabulary should be
  limited to existing, meaningful distinctions such as `lookup`, `synthesis`,
  `discovery/listing`, `comparison`, `audit`, and `debug`. Unknown must remain
  eligible for an appropriate generic route, not become a forced
  classification.

- **✅ Verified** (source search) — A1/A2 require specific corrections. A
  validated, versioned top-level `plan_json` answer-kind declaration can flow
  through builtin and grown saves without new plan persistence. It must not go
  into `dimensions`, which define plan identity. Existing verb inference and
  terminal `AnswerShape` do not express request/plan compatibility. Eligibility
  must cover ordinary cosine, category, FTS, and inline-planner few-shot paths.
  Manifest rows carry order and spans but no headings; T3 metadata carries
  non-uniform `section_title` and academic `section_type`. Section-diverse
  selection therefore needs manifest order plus hydrated T3 metadata and a
  positional fallback. Later research 7 falsified unvalidated use of those
  shared-chash titles for this purpose and replaced them with content-derived
  document-scoped labels. Canonical RDR scoping rules are reusable, but their
  current unbounded resolver and global first-exact-match resolution are not.
  *Source: `plans/seed_loader.py`, `schema.py`, `matcher.py`, `verb_infer.py`;
  `catalog/types.py`, `rdr_canonical.py`, `link_generator.py`;
  `mcp/core.py`; T2 `nexus_rdr/232-research-4` and the detailed
  `232-research-a1-a2-source-verification`.*

- **✅ Verified** (source search) — A3/A4 also need design corrections.
  Existing operators dispatch directly, but adjacent `check → generate` steps
  bundle and expose only terminal output. A host-side runner boundary must
  preserve typed facet/evidence anchors before reduction. Existing run records
  have fixed scalar fields; a nullable versioned JSONB summary requires an
  additive Liquibase migration, read/import threading, and a capability flag.
  It does not require a new table or second write. Older engines can silently
  discard unknown keys, so HTTP 200 is not proof that the summary persisted.
  *Source: `plans/runner.py`, `bundle.py`; `mcp/core.py`;
  `http_telemetry_store.py`; `TelemetryHandler.java`;
  `TelemetryRepository.java`; `telemetry-001-baseline.xml`; T2
  `nexus_rdr/232-research-3`.*

- **⚠️ Documented** (docs only) — The original AgenticScholar paper *is*
  indexed live, contrary to RDR-179's stale 2026-07-04 absence note: tumbler
  `1.12.116`, 54 pages, 157 chunks, indexed 2026-09-21. Its §4.2 predefined
  plan selection is two-stage — semantic candidate filtering, then an LLM
  rerank that assesses analytical-intent alignment and selects only above a
  high confidence threshold, otherwise falling through to dynamic planning.
  That is precedent for RDR-232's eligibility-before-ranking ordering and for a
  *later* optional LLM judge over already-eligible candidates; it is not a
  substitute for deterministic shape gates, because the paper's judge assesses
  intent alignment, not answer-kind compatibility. Its §4.1 Scope + Task
  decomposition parallels `RequestCoverage`'s explicit scope + facets. §6.5.2
  measures >90% token/latency reduction from plan reuse and self-correction
  converging within ≤3 attempts — precedent for bounded repair, though this RDR
  deliberately allows only one. The paper does not address corpus dilution,
  traversal containment, primary-source reservation, or facet evidence
  coverage; those remain Nexus-specific design.
  *Source: Lan et al. 2026, arXiv:2603.13774, tumbler `1.12.116`,
  `knowledge__agentic-scholar-papers__voyage-context-3__v1`; direct T3 section
  reads. T2: `nexus_rdr/232-research-5`.*

- **✅ Verified within prototype limits** (spike) — A1's explicit declarations
  and conservative eligibility policy ran against the real `plan_match` in an
  isolated harness. All 12 builtins received declarations; 14/14 labeled
  eligible cases were retained and 0/3 wrong-shape cases admitted after the
  gate. The raw matcher admitted the lookup plan before the external gate.
  Unknown requests routed to generic/inline planning as designed, and the real
  few-shot renderer excluded the rejected plan. Twelve positive cases use each
  plan's own declared kind as the request kind, so they test the gate, not
  inference or independently labeled routing quality. The three negative cases
  are one lookup-versus-synthesis mismatch on three matcher branches. This does
  not prove production integration or natural-language request inference; the
  fixture is not a live recall estimate. *Source: A1 harness and results,
  archived in DEVONthink `A7004B0A-B6D7-4F4F-840F-4C98FF8B461B`; T2
  `nexus_rdr/232-research-6`.*

- **✅ Verified** (spike) — A2 in production, Phase 1. The resolver
  (`plans/primary_evidence.py`) finds ART RDR-084 only through an explicit
  owner or project binding, never through a first global match. Section labels
  come from chunk content in manifest order, because engine 0.1.164 writes no
  label keys. On the engine substrate all 12 rows reached the reducer, and the
  over-budget case was marked partial (6 of 12). In the live canary
  (2026-10-26, develop `b72c3cd3d`) G06 resolved to owner 1.2 and 24 of 80
  primary chunks reached the reducer, disclosed as partial. Two gaps were open
  at Phase 1 exit: sibling files that share an RDR number resolved to
  candidates (decided since, not yet built: the file whose frontmatter carries
  the id wins),
  and the owner did not filter the vector stream (28 of 40 G06 vector hits
  came from another project; fixed by 1.18, see section 2a).
  *Source: golden journey and live canary. T2: `nexus_rdr/232-research-19`,
  `nexus/rdr232-golden-phase1`, `nexus/rdr232-canary-phase1`.*

- **✅ Verified** (spike) — A6 in production, Phase 1 client side. The
  traversal policy runs inside `traverse` after the walk and before manifest
  expansion (`plans/traversal_policy.py`). With 320 out-of-scope ids ahead of
  3 in-scope ones, the old 300-id cut lost all 3; the policy kept exactly the
  3. A call with no policy returns the old shape unchanged. A 500-node
  response is reported as possibly truncated, never as contained. On the same
  fixture, scope leakage at the reducer fell from 0.40 to 0.0. In the live
  canary 1 of 4 traversals hit the 500-node engine cap (G06), and 243 of
  those 500 nodes were out of scope, cut only after the engine truncated. That
  meets the gate for the engine pre-cap filter (Phase 1 step 6). The ranking
  puts link role before distance, so all 38 one-hop documents lost to two-hop
  implementation-role ones; that weighting is not yet calibrated.
  *Source: golden journey and live canary. T2: `nexus_rdr/232-research-20`,
  `nexus/rdr232-canary-phase1`.*

- **✅ Verified** (spike) — A7 in production, Phase 1. For a resolved RDR
  file, the lifecycle stream reads the owner's `<owner>_rdr` T2 project by
  exact title only (`plans/lifecycle_stream.py`), within 6 records, 24 reads,
  4,000 and 18,000 characters, and the `nx_answer` deadline. Its records lead
  the reserved share and are never evicted by chunks. On the substrate with a
  second tenant, ART RDR-084 read its metadata and latest gate record in 3
  reads; an ambiguous source, a code source and another tenant's source made 0
  reads, and no tenant saw another's records. In the live canary G06 got 2 T2
  records to the reducer and G15 got 4. Not settled: the owner-name-to-project
  rule depends on `nx rdr` and the indexer deriving the same name, which a test
  pins but no shared helper enforces.
  *Source: golden journey and live canary. T2: `nexus_rdr/232-research-21`,
  `nexus/rdr232-canary-phase1`.*

- **✅ Verified** (spike) — A1 in production, Phase 2. Eligibility runs on
  every matcher branch and on the planner few-shots, and enforces live: plan
  473 is excluded for the RDR-084 incident request and a grown plan is excluded
  as unknown-grown (proof runs 731-738). Wrong-shape admission falls only
  in-sample: golden_v1 4 of 24 to 0, and the 4 includes G06 admitted through a
  synthetic grown clone of plan 473 (3 of 24 to 0 with builtins only). On a
  blind 29-request held-out set the gate removed no wrong-shape admission (1 of
  29 before and after) and cost one non-lookup selection (recall 0.724 to
  0.690). Lookup recall holds at 0.4 only when extraction supplies the kind;
  with first-pass kinds it halves. On 143 replayed real runs the gate removes
  the selected plan from 50%, 42% and 38% of runs with first-pass, Opus and
  Haiku kinds. Plan reuse on broad requests (step 6): with first-pass kinds,
  builtin reuse on broad-kind runs falls from 33 to 15 and grown reuse stays 0
  to 0 (no grown plan was selected in the window, so it cannot move); with
  Haiku kinds, 50 of 113 broad-kind runs lose their selected plan (synthesis
  36 of 84, comparison 13 of 28, audit 1 of 1). Not settled when taken (2026-10-27), closed 2026-10-30: the directional rule
  admitted synthesis plans for lookups, so the one held-out wrong-shape admission
  could not be removed, and a live single-theorem lookup ran a synthesis plan for
  1.64 USD. The lookup row is now lookup-only (nexus-jypmj.11; entry dated
  2026-10-30 below). These figures were taken under the directional rule.
  *Source: gate matrix, replay and live proof. T2: `nexus_rdr/232-research-22`,
  `nexus/rdr232-phase2-thresholds`.*

- **✅ Verified** (spike) — A5 in production, Phase 2. The deterministic first
  pass never assigned a wrong kind but abstains often out of sample: 11 of 29
  held-out requests exact, 18 unknown (in-sample 23 of 24); on real traffic it
  leaves 97 of 143 runs unknown. The structured step, now on by default with
  Haiku, brings held-out agreement to 27 of 29: it reads H30 (labelled unknown)
  as synthesis and leaves H23 (labelled audit) unknown, and it rightly leaves
  H16 unknown, so Haiku does sometimes abstain (Opus never did: its misses are
  H16 and H30). 0 of 112 calls failed across both models (56 each); Haiku costs
  about 0.002 USD and 4 s a call, on about two thirds of real requests. The
  active-project tier works live on the unmodified RDR-084 call: candidates for
  both documents from a Nexus session, ART's RDR-084 with an inferred owner
  from an ART session, an ambiguity with its reason from a host without a
  project, and an explicit owner when the call binds the project. Not settled:
  Haiku reads compound single-paper questions as lookup where Opus says
  synthesis, and no labelled set covers that class. [Correction 2026-10-28:
  a labelled set now covers that class, `bench/queries/golden_compound_lookup_v1.yaml`
  (six requests, in-sample); the next entry records the re-measure.]
  *Source: extraction measurement and live proof. T2:
  `nexus_rdr/232-research-23`.*

- **✅ Verified** (spike) — A5, the lookup boundary after nexus-jypmj.13. The
  extraction prompt now says what section 1 says: several connected facts from
  one passage of one source are a `lookup`, and a `synthesis` spans sections,
  sources or history. Re-measured on 98 calls (Haiku 67, Opus 31; no timeout,
  error or invalid reply). With extraction, Haiku reads golden 22 of 24 as
  labelled (G23 and G24, both labelled unknown), held-out 28 of 29, and the six
  compound single-passage requests 5 of 6 as lookup (C02 reads synthesis); Opus
  reads golden 21 of 24, held-out 27 of 29 and the compound six 6 of 6 as
  lookup. The failure that opened the bead, Opus reading compound single-passage
  questions as synthesis, is closed on this set. All five Phase 2 gate
  thresholds read as they did when Sam approved them (golden wrong-shape
  admission 4 of 24 to 0; held-out 1 of 29 before and after; lookup recall 5 of
  5 on golden and 2 of 5 held-out; held-out recall 0.724 to 0.690; 0 of 98
  timeouts or errors). `_weak_lookup` was left unchanged on purpose: the first
  pass sees the question text and no passage or source, so a deterministic rule
  that calls a two-facet question `lookup` would override the model on exactly
  the case the model is there to decide, and where extraction is off or fails the
  right fallback is the inline planner, not the lookup plan. Not settled: the
  compound set is in-sample (written with the boundary in view) and has six
  requests. Its control, six one-source synthesis requests whose parts live in
  different sections (`bench/queries/golden_near_miss_synthesis_v1.yaml`), is
  authored and has not been run, so whether the new wording reads synthesis as
  lookup is unmeasured.
  *Source: extraction measurement and gate matrix. T2:
  `nexus/rdr232-jypmj13-remeasure-2026-10-28`,
  `nexus/rdr232-phase2-thresholds`.*

- **✅ Verified** (spike) — the pre-cap graph scope filter (nexus-jypmj.1.13)
  departs from the section 3 text in one place. The text says an engine that does
  not report the filter yields no secondary graph evidence. The client applies
  that only when the walk may have been cut, that is when the nodes returned plus
  the seeds the walk lost reach the engine's 500-node cap, because that cut is the
  only silent cap in the walk. A walk below the cap is complete, so filtering it
  by scope on the client is exact and omitting it would discard good evidence; the
  reason for the rule, a cut spent on out-of-scope nodes, does not apply to it.
  Consequence: against the deployed engine (0.1.167, which does not report the
  filter) and a cloud edge that has not allowlisted the flag, a capped walk (G06
  was capped at 500 in the 2026-10-26 canary) returns seed documents only, so the
  client must not ship ahead of the engine tag and the edge allowlist for that
  request class.
  *Source: engine wire change and its review. T2:
  `nexus/rdr232-engine-wire-three-changes-2026-10-28`,
  `nexus/review-rdr232-finish-critique-2026-10-28`.*

- **✅ Verified** (spike) — the live canary of the coverage check is slow, and
  latency is its cost. ART RDR-084 (G06), owner 1.2, engine 0.1.167, 2026-10-28:
  with coverage off the run answered in 29.4 s for 0.433 USD; with coverage on it
  answered in 147.3 s for 0.430 USD (assessment 0.0166 USD), 118 s longer. The
  assessment made four serial calls, one per evidence stream, 118.2 s in all.
  The primary, T2 and vector streams answered (30, 11 and 16 accepted records)
  and the graph stream, 65 anchors, timed out at the 60 s call limit, so the
  outcome was `partial_failure` with cause `raised_call`; the facets were still
  covered by the other streams (6 of 6, one conflicted). The serial design is
  recorded as provisional in `src/nexus/plans/coverage_ledger.py`. The summarize
  step's `elapsed_ms` (140.8 s) includes the assessment wait, so per-step
  latency in `nx answer-runs` over-attributes to summarize until assessment
  latency is split out of it. Follow-up nexus-jypmj.17 covers concurrent
  per-stream calls under the stage deadline, continuing after a raised call and
  the 65-anchor pack; this finding said turning the check on by default waited
  on it, and on 2026-10-28 Sam turned it on together with that work (the next
  finding). This is one request on one run.
  *Source: live canary of 2026-10-28, recorded in the bead nexus-jypmj.3.10
  comment of that date (the 2026-10-26 Phase 1 canary is T2
  `nexus/rdr232-canary-phase1`).*

- **⚠️ Defect found and fixed** (2026-10-28, live golden run) — With the
  coverage check on, an ambiguous named source was counted as a missing one.
  G01, G02 and G04 each name an RDR number that two projects carry (nexus 1.1
  and ART 1.2), so source resolution returned candidates; the coverage source
  check reported `missing`, the facet was blocked as `source_missing`, and
  generation was skipped at zero cost while the coverage-off arm answered all
  three. Per Failure Modes ("Primary source unresolved: return candidates or a
  disclosed unresolved state") and section 4 (only a missing named source
  counts as uncovered), an ambiguous source, or one whose lookup failed, timed
  out or hit its candidate cap, now has its own coverage state `unresolved`:
  disclosed in `coverage.sources` and the notice, blocking no facet. A source no
  document carries stays `missing`. The resolver's own `unresolved` status
  (nothing found) still maps to coverage `missing`, so the word means different
  things on the two sides. The golden latency and cost deltas recorded for that
  run include the three withheld pairs, so they read low; they are re-taken on
  the fixed tree.
  *Source: bead nexus-jypmj.3.17; bead nexus-jypmj.3.10 comment of 2026-10-28.*

- **❓ Assumed** (unit-test timing model; no live run; the code behaviour is
  tested, the latency and accuracy are not measured) — the coverage check is on
  by default as of 2026-10-28, by Sam's decision, taken together with the latency
  fix the canary above asked for (nexus-jypmj.17). The assessment calls now run
  at most 4 at a time, fewer when an API backend's configured
  `NX_LLM_MAX_CONCURRENCY` is lower (the `claude -p` backend has no gate and its 4
  concurrent subprocesses are unmeasured), each cut at the stage's 0.4 share of
  the deadline left, the cut killing the `claude -p` process tree and reporting an
  unknown cost; a stream over 20 anchors is split
  into balanced packs, so the canary's 65-anchor graph stream is four calls of 16
  to 17; a call that raises or replies unusably costs only its own facets, so
  `stopped_after_failure` is no longer produced; and the terminal step's
  `elapsed_ms` no longer carries the assessment. The tests model the canary at
  1/100 scale (a call takes 0.15 s plus 7 ms per anchor; 99 anchors in four
  streams of 12, 12, 65 and 10): the serial, unsplit stage takes 130
  model-seconds, with the graph call cut at the 60 s limit as in the canary
  (measured live: 118 s), and the concurrent split stage takes 50 model-seconds
  in two waves of calls, with no call cut. That 50 s is the model's figure under
  assumed per-call latencies, not a measurement: the live latency with the check
  on is re-taken on the next canary, and the 20-anchor pack size and the
  per-anchor latency are unmeasured as accuracy and latency. An unrecognised
  `NX_ANSWER_COVERAGE` value (`disable`, `off!`) reads as off, not as the default:
  the safe direction for a typo.
  *Source: bead nexus-jypmj.17; `tests/test_rdr232_coverage_concurrent.py`; bead
  nexus-jypmj.3.10 comment of 2026-10-28.*

- **✅ Verified** (spike, live) — The concurrent default-on coverage check on
  production (develop 6dd75ec3f, engine 0.1.168, 2026-10-29). G06 canary:
  assessment 54.3 s for 8 concurrent calls (graph in 4 packs), against 118 s for
  4 serial calls on 2026-10-28; 0 cut, 0 raised, 6 of 6 facets covered; wall 87.7 s
  on, 30.0 s off. Held-out set, 29 requests with the check off and on: 0 errors,
  0 answers withheld, 2 partial, 0 unassessed facets, wall latency p50 +16.8 s and
  p90 +33.9 s, assessment 0.021 USD a run (all taken under the directional lookup
  rule and before 2026-10-30's unread-anchor rule, so a lookup that now runs a
  different plan, and a facet now read as unsupported beside unread items, are
  outside these figures). The ambiguous-source fix
  (nexus-jypmj.3.17) holds live, with and without a project context; G18, still
  withheld, is a correct withhold (retrieval returned test code, and the
  coverage-off answer addresses a different question).
  *Source: bead nexus-jypmj.3.10 comments of 2026-10-29; research 24.*

- **✅ Verified** (spike, live) — Facet reliability against labels drafted blind
  from the items the assessor saw (21 held-out requests): false negative 1 of 31
  facets (3.2 percent, 95 percent interval 0.6 to 16), false skip 0 of 21 runs.
  Sam spot-checked 15 drafted pairs: the drafter was wrong on 4, all drafted
  neutral where Sam read supports, and on the 3 sampled disagreements Sam sided
  with the assessor. So the drafted supports labels, and the false-negative rate
  built on them, hold at screening grade; the drafted neutrals under-count support
  and cannot show the assessor over-covers or over-flags conflicts. Not measured:
  the five largest held-out requests (so the rate at which a correct answer
  whose support stays unread is withheld is unknown). The negative-control set
  was not measured when this was taken; since then N04 was measured (entry dated
  2026-10-30 below) and N01-N03 were replaced with markers absent from the
  tenant and have not been run.
  *Source: bead nexus-jypmj.3.10 comments of 2026-10-29; T2
  `nexus/rdr232-3.10-live-measurement-2026-10-29`; research 24.*

- **✅ Verified** (spike, live) — The run summary and the out-of-step cost column
  in production: engine 0.1.168 (telemetry-017-1, one nullable column) deployed
  after a fork rehearsal, STEP-6 green, the cloud client-path gate 13 of 13; every
  measurement row persisted and read back its summary, and 48 of 71 rows carry a
  value (1.603811 USD), matched by a read-only production recount. The old-engine
  and cached-capability rollback legs remain simulated only.
  *Source: beads nexus-0gc7m, nexus-jypmj.3.10; research 25.*

- **✅ Verified** (spike, live envelopes read; mechanism reproduced in process) — the pre-cap graph
  scope filter loses the link role and distance of in-scope nodes reached through a removed relay,
  and under a single-role policy that drops them. ART RDR-084 (G06), owner 1.2, phase-3.10
  envelopes of 2026-10-28. The same traverse step, client-side filter (canary and golden runs):
  154 nodes walked, 124 out of scope, 30 retained, 0 dropped by role, 0 out of owner. With the
  engine filter (canary2 run): 30 walked, the engine removed the same 124, 25 dropped by role,
  5 retained (seeds included), 0 out of owner. So 25 of the 30 in-scope documents (83 percent)
  were reachable only through an out-of-scope relay, arrived without an edge, took role `related`
  and no distance (`traversal_policy._distance_and_role`), and a non-`factual`
  `require_link_role` dropped them. Under `factual` they would be kept but ranked last and
  exposed to the document cap. The graph stream's evidence changed with it: the final reducer
  input held 64 graph chunks in both arms, but 16 of the 64 chunk hashes are the same (25
  percent), and the coverage check answered 6 of 6 facets in both. The envelopes carry no
  per-node tumblers for the graph stream and not the step's link types, so the node-by-node
  table (which of the 25, at what distance and role) cannot be built from them; it needs the
  seed (1.2.4686), plan 467's traverse arguments and one read-only `/v1/catalog/traverse` call
  with and without `scope_filter`. The owner limit is separate: `dropped_out_of_owner` was 0 on
  all 6 owner-bound walks (largest walk 154 nodes, none near the 500 cut), so no other owner's
  documents were measured taking cut slots. The predicate takes a collection set or a subtree,
  not an owner; owner narrowing happens client-side in `traversal_policy.select_documents`.
  Whether the lost evidence changes an answer is not measured: one request, no oracle on the
  graph stream. Remedy (decided, nexus-jypmj.18): the engine returns each node's BFS depth and best
  link role (`node_walk`, engine-service-v0.1.170) and the client scores a node the edges do not reach
  from it; against an engine without it a single-role policy sends no `scope_filter` and narrows
  client-side, which equals 7.88.0 only under the 500-node cap (a capped walk keeps seeds only,
  `secondary_omitted`, since 7.89.0). A `factual` or mixed-role policy still sends the filter to such
  an engine, so its relay-reached nodes rank last and are exposed to `max_documents` until the
  engine carries `node_walk`. Verified in process and on the real engine (Java, Postgres
  container) on the S-R-B shape; the G06 rerun on an engine carrying `node_walk` is not done.
  *Source: bead nexus-jypmj.18; T2 `nexus/rdr232-jypmj18-dropped-out-of-owner-evidence-2026-10-29`;
  `service/.../CatalogRepository.graphBFS` javadoc; `src/nexus/plans/traversal_policy.py`.*
- **Verified** (spike) — the tightened lookup row (nexus-jypmj.11, 2026-10-30). Measured three rules for a `lookup` request: the shipped directional rule (lookup or synthesis plans), lookup or listing, and lookup only; "synthesis plans declared lookup-suitable" reduces to lookup only, since no plan declares it. Offline, with the Phase 2 matrix, the shipped rule reproduces the approved figures; both tightened rules take held-out wrong-shape admission from 1 of 29 to 0 (H09 now selects the lookup plan) with no recall loss (held-out 21 of 29 against 20; lookup 3 of 5 against 2) and leave golden_v1 and the compound set unchanged. **Those two figures hold for Haiku- or Opus-extracted kinds.** With first-pass (deterministic) kinds, wrong-shape admission goes 1 to 2 (H09 and H22) under every rule, the tightened ones included, and held-out recall falls 21 to 15; no rule changes that. H18's loss is the discovery row and no lookup rule moves it. The labels list only unacceptable plans, so the two tightened rules tie on them. Live they differ. Against the shipped rule, 4 of the 5 live lookups ran a different plan under lookup or listing, and 3 of those went to the listing plan, which returned a raw list of chunk ids without reading the named source; the shipped rule ran citation-traversal for 2 of 5 lookups (H09 1.24 USD against 0.14 for the lookup plan); lookup-only answered all five with the lookup plan. The live rows are 901-916 and 918: 901-912 are the rule comparison and 913-916 and 918 the N04 runs; row 917 is the `max_rows_per_doc` get_many run and is not part of it, and rows 899 and 900 are void (the run tag matched the RDR-reference pattern and made RDR-232 a named source). A replay of the newest 300 real runs (81 with Haiku kinds, 25 lookups) shows what lookup-only costs in exclusions: it excludes 8 of the 25 lookups against 5 under the shipped rule; the three extra are synthesis plans the held-out labels list unacceptable for lookups, at 1.643 USD (run 737), 0.681 and 0.886. Not settled: the cost of the inline planner for a lookup no lookup plan matches.
  *Source: rule matrix and live runs 901-916 and 918; real-traffic replay. T2: `nexus/rdr232-jypmj11-rule-measurement-2026-10-30`, `nexus/rdr232-jypmj23-measurement-2026-10-30`.*

- **Verified** (spike, live) — an assessment that leaves anchors unread (nexus-jypmj.23, 2026-10-30; negative control N04, a lookup, scope `rdr`). Under the shipped lookup rule the request ran plan 467 (citation-traversal), which handed the coverage check 301 anchors. The assessment calls carried 127 of them: seven calls, all replied neutral (21 and 58 accepted records, none supporting), and 174 anchors were left out by the per-call size cap. The code then marked both facets `unassessed` (`assessment_incomplete`) because a call had left anchors out, so the check could not withhold: the run reported reason `assessment_failed` beside an assessment outcome `ok` with no failure, and off-topic prose (RDR-007 hosting costs) came back `answered`, on all four runs of the plan (rows 913-916). Under lookup-only the same request ran plan 473 with 15 anchors, all of which fit, and was withheld (`uncovered`, 0.0084 USD, row 918). Sam's ruling (2026-10-30): a facet no item the check READ supports or contradicts reads `unsupported`, not `unassessed`, and the notices and the envelope say how many items were not read (section 4 departure, Phase 3 As run). The count is each unread item once per stream per round, and the notices state "N of M retrieved items were not read". Repair is now entered for these facets and reads the unread anchors. What this does not show: a correct answer whose supporting item is among the unread ones is withheld, and the rate of that is not measured (the five largest held-out requests were never run). Also unmeasured: N01-N03, replaced with markers absent from the tenant.
  *Source: live runs 913-918. T2: `nexus/rdr232-jypmj23-measurement-2026-10-30`, `nexus/rdr232-jypmj23-negative-controls-2026-10-30`.*

### Critical Assumptions

Research 6–10 executed prototypes for A1–A6; A7 has none yet. They establish
feasibility within the stated fixtures, not production integration or live
answer quality. The detailed A1/A2 census is
`nexus_rdr/232-research-a1-a2-source-verification`; A3/A4 current interfaces
are in `232-research-3`. Behavioral evidence is in `232-research-6` through
`232-research-10`.

- [ ] **A1 — explicit answer-kind metadata and conservative request routing can
  improve eligibility without material recall loss.** **Status**: Partially
  verified (research 6, 22). **Method**: Source Search plus executed prototype
  fixture using the real matcher. The gate passed 14/14 eligible and 0/3
  wrong-shape cases, with correct unknown routing and few-shot exclusion.
  Phase 2 (2026-10-27) verified production integration on every matcher
  branch and the planner few-shots, and request-kind inference with Haiku
  extraction (Key Discoveries; research 22, 23). Still open when taken
  (2026-10-27): the fall in wrong-shape admission was in-sample only (held-out 1
  of 29 before and after), and the directional rule admitted synthesis plans for
  lookups. The second is closed 2026-10-30: the lookup row is lookup-only
  (nexus-jypmj.11), which takes held-out wrong-shape admission from 1 of 29 to 0
  for Haiku- or Opus-extracted kinds (first-pass kinds still give 1 to 2). Store kind in `plan_json`, never dimensions; decode legacy absence
  or invalid metadata as `unknown`; apply eligibility on every matcher branch
  and planner few-shot selection.
- [ ] **A2 — ordered manifests plus content-derived, document-scoped section
  labels can supply bounded section-diverse primary evidence.** **Status**:
  Partially verified (research 7). **Method**: Source Search plus real
  ART/Nexus RDR-084 resolution, manifest hydration, selection, and edge
  fixtures. Project-scoped resolution, bounded retrieval, aggregate budgets,
  positional fallback, and duplicate-chash attribution passed. Shared-chash
  `section_title` was falsified as authoritative document metadata: identical
  headings in different RDRs returned another document's title. The corrected
  selector derives labels from chunk content in manifest order and never
  trusts stored labels unvalidated. Production resolver/runner integration,
  candidate paging, and more than two real documents remain unverified.
- [x] **A3 — existing dispatch can support facet coverage at a new runner-owned
  pre-reduction boundary.** **Status**: Verified live 2026-10-29 (research 24); earlier, partially verified (research 8).
  **Method**: Source Search plus deterministic boundary spike using real
  `plan_run` and real bundle composition/dispatch with fake model output.
  Plain `check → generate` hid its intermediate; the proposed boundary preserved
  anchors, executed one prefix/repair/reduction, shared budget/deadline, and
  disclosed failure/scope/continuation states correctly. Production runner
  integration and live-model facet reliability remain unverified.
- [x] **A4 — a nullable versioned run summary can extend existing telemetry
  compatibly.** **Status**: Verified live 2026-10-29 except the old-engine and cached-capability rollback legs, which stay simulated (research 25); earlier, partially verified (research 9). **Method**: Source
  Search plus private-engine and isolated `TenantScope`/jOOQ repository spike.
  Old-engine silent drop, one-write current completion, nullable JSONB summary
  persistence, rollback, plan counters, imports/readback, and old-client null
  behavior passed. Production Liquibase/jOOQ/DDL-ratchet work, full handler and
  import wiring, old/new matrix, and the client capability-off sub-leg remain
  unverified; that sub-leg's private-test write-guard opt-in was denied by the
  permission classifier and was not executed.

- [x] **A5 — deterministic-first request extraction can produce kind, facets,
  and project-qualified sources without silent narrowing.** **Status**:
  Verified (research 10, corrected by review; research 23). **Method**: Source
  Search plus bounded extraction spike and real owner/catalog reads. The
  question's opening synthesis cue classified correctly and the splitter
  returned seven clauses, including topic and constraint clauses. It did not
  prove facet/constraint separation. The harness supplied literal `ART` and
  its absolute path, which resolved owner 1.2 and ART RDR-084 tumbler
  1.2.4686; the recorded question contains no `ART` token, so qualifier
  extraction was not proven. The MVV therefore requires an explicit owner or
  project binding. Phase 2 (2026-10-27) verified general inference with
  Haiku structured extraction (27 of 29 held-out; 0 of 112 calls failed;
  about 0.002 USD and 4 s a call), the ambiguity fallback, and the
  active-project tier live on the unmodified RDR-084 call (research 23).
  Still open: compound single-paper questions are lookups only because Haiku
  departs from the extraction prompt; the prompt and `_weak_lookup` are
  aligned with the approved lookup definition in nexus-jypmj.13. [Correction
  2026-10-28: only the prompt was aligned; `_weak_lookup` was left unchanged on
  purpose, and the compound set closes the Opus divergence for six requests. See
  the Key Discoveries entry on the lookup boundary.]
- [ ] **A6 — traversal can apply scope before its chunk cap without losing
  alignment.** **Status**: Partially verified (research 10). **Method**: Source
  Search plus synthetic graph fixture with >300 out-of-scope ids preceding
  in-scope evidence. Legacy truncation lost the in-scope doc; filtering nodes
  before manifest expansion retained it with per-id collection alignment.
  Production `traverse` integration and no-policy backward compatibility remain
  unverified. The engine also caps graphBFS at 500 nodes before client
  filtering. A6 does not exercise the engine path; Phase 1 discloses a capped
  response and measures how often the cap fires before deciding on an engine
  filter.
- [ ] **A7 — an RDR's T2 metadata and gate records can serve as a bounded
  lifecycle evidence stream.** **Status**: Unverified (inferred, not read: the
  read path and record shapes were not exercised for this purpose). **Method**:
  Phase 1 integration test resolving an RDR source, reading its `{repo}_rdr`
  metadata and latest gate record within budget, and refusing a source from
  another tenant.

## Proposed Solution

### Approach

Add three gates around the existing path:

```text
question + explicit caller constraints
        ↓
derive requested answer shape and requested facets
        ↓
filter plan candidates by shape eligibility and runnable bindings
        ↓
semantic rank, then cost choice among eligible plans
        ↓
retrieve named primary evidence + bounded secondary streams
        ↓
check evidence against named source and requested facets
        ↓ missing, once only
retrieve targeted missing evidence → recheck → report residual gaps
        ↓
reduce with source citations and a structured coverage record
```

The design prefers deterministic containment first. A model may help infer
facets or judge their coverage, but it must not be the only guard against a
caller-scope violation. Explicit caller constraints always outrank inferred
intent and plan defaults.

### Technical Design

#### 1. Answer-shape eligibility

Add a versioned, closed starter vocabulary for **routing capability**. It is
not an independent answer-object-family registry. RDR-189 P2 may later define
the durable target families; at that point these values become derived routing
capabilities of those families, with a versioned migration rather than a second
taxonomy.

| Shape | Meaning | Typical evidence requirement |
| --- | --- | --- |
| `lookup` | A specific fact or small set of facts, including several connected facts from one passage of one source | Best direct evidence; optional graph context |
| `synthesis` | A unified account of multiple requested aspects across sections, sources or history | Coverage of each requested facet |
| `discovery/listing` | A bounded inventory or corpus-coverage result | Complete, attributable result set within the declared bounds |
| `comparison` | Relative treatment of two or more subjects | Comparable evidence for every subject and axis |
| `audit` | Findings against a criterion or implementation | Inspectable evidence for each claimed finding |
| `debug` | Explanation and diagnosis of a failure | Failure evidence and relevant implementation path |
| `unknown` | No reliable declaration | Not a positive compatibility signal |

`AnswerShape` here means the requested answer contract, not the terminal-string
classification already in `plans/answer_shape.py`. The implementation should
name the new type distinctly, for example `PlanAnswerKind`; the terminal
classifier is extended separately for insufficient evidence.

The starter assignments are explicit, not inferred from the existing `verb`.
They are keyed by template file (several YAMLs share `name: default`):

| Builtin template file | Routing capability |
| --- | --- |
| `abstract-themes.yml`, `analyze-default.yml`, `citation-traversal.yml`, `research-default.yml` | `synthesis` |
| `corpus-coverage-check.yml`, `document-discovery.yml`, `find-by-author.yml`, `type-scoped-search.yml` | `discovery/listing` |
| `debug-default.yml` | `debug` |
| `document-default.yml` | `audit` |
| `hybrid-factual-lookup.yml` | `lookup` |
| `review-default.yml` | `comparison` |

Compatibility is defined by this table. It is deliberately small and
versioned; it is not a claim that kind implies facet coverage.

| Request kind | Eligible declared plan kinds |
| --- | --- |
| `lookup` | `lookup`, `synthesis`, `unknown` |
| `synthesis` | `synthesis`, `unknown` |
| `discovery/listing` | `discovery/listing`, `unknown` |
| `comparison` | `comparison`, `synthesis`, `unknown` |
| `audit` | `audit`, `synthesis`, `unknown` |
| `debug` | `debug`, `synthesis`, `unknown` |
| `unknown` | curated `synthesis`, curated `unknown` only; otherwise inline planning |

**Decided by Sam, 2026-10-30 (nexus-jypmj.11): the `lookup` row admits `lookup`
and `unknown` plan kinds only; it no longer admits `synthesis`.** The table above
is the text as gated; this note supersedes its first row. Measured over three
candidate rules (the Key Discoveries entry dated 2026-10-30): the directional
row let a synthesis plan outscore the lookup plan (held-out H09 ran
citation-traversal at 1.24 USD against 0.14 for the lookup plan), the
lookup-or-listing row sent three of the five live lookups to a listing plan
that read no source (four of the five ran a different plan from the shipped
rule's), and the lookup-only row took held-out wrong-shape admission from 1 of 29
to 0 with no recall loss (held-out 21 of 29 against 20; lookup 3 of 5 against 2)
and left golden_v1 and the compound set unchanged. A lookup that no lookup plan
matches now goes to the inline planner; that planner's cost on lookups is not
measured. The other rows, and the provenance limits on `unknown` plans, are
unchanged.

The `unknown` entries are limited by provenance below; they are not blanket
permission for every legacy or grown plan. In particular an unknown request
cannot select an unknown-grown plan, because that is exactly the original
uncertainty twice over.

Builtin plans declare their kind explicitly in a validated, versioned top-level
`plan_json` field. Do not add it to `dimensions`: canonical dimensions define
plan identity and database uniqueness, so changing them would fork plans and
break seed reconciliation. Missing or invalid legacy declarations decode to
`unknown`. Plan 473 declares `lookup`, so it is ineligible for the observed
synthesis request. Grown plans inherit the kind of the route that produced them
only when that route was known and recorded in provenance; otherwise they are
`unknown`. No migration may guess a shape from plan text and silently promote
it to a hard constraint.

A **builtin/curated** row stays eligible as its declared kind; a builtin seeded
before declarations existed is treated as unattested until a reseed, and the
gate is not enabled before every builtin carries its declaration. Unknown-kind
**grown** plans are not a generic bypass: with no recorded answer-kind
provenance, they are eligible only for `lookup` or `discovery/listing` requests,
and remain subject to the existing literal/scope/confidence gates. An unknown
request or a broad `synthesis`, `comparison`, `audit`, or `debug` request cannot
select one. Recorded kind provenance can relax that restriction after review.
Add grown-plan regressions for both a lookup-shaped history with a synthesis
request and an unknown request; both route to curated generic planning or the
inline planner, never to the unknown-grown plan.

The request-side derivation returns:

```text
RequestCoverage(
    answer_kind: PlanAnswerKind | unknown,
    facets: list[Facet],
    constraints: list[Constraint],
    named_sources: list[SourceRef],
    explicit_scope: ScopeConstraint,
    current_code_requested: bool,
)
```

A facet has stable text and, when available, source hints. Facets are topics
that require evidence; constraints govern how the answer must treat that
evidence and do not trigger retrieval by themselves. For example, “state gate
history” is a facet; “distinguish acceptance-date facts from later records,”
“cite sources,” and “do not infer current code” are constraints. Constraints
are preserved in the coverage ledger as separate obligations. They can prohibit
widening or require citations, but cannot silently become new retrieval facets.
Explicit user wording is the source of these fields. The caller does not need a
new MCP parameter or a new command.

A `lookup` request admits lookup plans only (Sam, 2026-10-30, nexus-jypmj.11;
the dated note under the table above and the Key Discoveries entry of that date).
A lookup-only plan still may not claim a multi-facet synthesis, and the other
rows keep their shipped directions (a synthesis plan may answer a comparison or
an audit). Unknown **plan** declarations are not positive evidence of
incompatibility and remain eligible under conservative legacy/grown gates.
Unknown **request** kind must not make a known narrow plan the default for a
broad or ambiguous request: retain only genuinely generic compatible routes or
enter inline planning with eligibility diagnostics. There is no existing
no-model generic synthesis fallback to reuse; adding one would be separate
work.

Until 2026-10-30 the lookup row was a directional rule, kept as a measured limit
for the first enabled release (Sam, 2026-10-27): any synthesis-capable plan was
admitted for a lookup, so the gate could not remove a synthesis plan the labels
call wrong for a lookup (held-out H09), and a live single-theorem lookup ran a
synthesis plan for 1.64 USD where the lookup plan costs about 0.13 (proof run
737). That rule was measured against two tightened ones after the release and
replaced by lookup-only (nexus-jypmj.11); the lookup-only rule takes no
`synthesis` plan for a lookup, and no plan has declared itself lookup-suitable,
so there is no declared exception. A grown plan of unknown kind is still
admitted for a lookup (its kind is not known to be wrong). The rule does not
move the loss of one plan the labels accept, held-out H18: that is the
`discovery/listing` row (a discovery request losing `document:default`), and
no lookup rule touches it.

Apply eligibility to ordinary cosine candidates, below-floor category-route
candidates, and FTS fallback candidates before cost choice. Wrong-shape plans
must also be excluded from the inline planner's few-shot examples, or they can
still teach the planner the rejected structure. Pass `RequestCoverage` into
that planner explicitly.

Record the inferred kind, candidate exclusion counts, and whether the decision
was explicit, heuristic, or model-assisted. Do not log private question text
when `trace=False`.

##### Request extraction and project resolution

`RequestCoverage` is built **before** `plan_match`, in a separately measured
stage with its own timeout and cost accounting. Explicit caller parameters and
bindings take precedence over question text. The implementation uses a
deterministic first pass and a single structured-extraction call only when the
first pass cannot establish the required shape. The call is on by default with
Haiku (Sam, 2026-10-27): the first pass leaves about two thirds of real
requests unknown or facet-uncertain, and the call costs about 0.002 USD and
4 s. `NX_ANSWER_STRUCTURED_EXTRACT=0` turns it off, and the eligibility gate
then runs in shadow, because the gate on first-pass kinds failed the
lookup-recall threshold (held-out lookup recall 0.4 to 0.2).

| Field | Extraction | On failure |
| --- | --- | --- |
| Answer kind | Explicit dimension/hint first; then conservative patterns for lookup, comparison, audit, debug, discovery/listing, and multi-facet synthesis. Ambiguous phrasing yields `unknown`; it does not inherit `verb_infer`. | `unknown`; use generic planning rather than a narrow plan. |
| Facets and constraints | Preserve explicit clauses and enumerated requests as stable ids. Separate evidence-requiring facets from treatment constraints such as temporal qualification, citation requirements, and prohibitions on inferring current code. If clause extraction is uncertain, use one structured `extract` call with the original question and a closed schema. | A conservative `whole_question` facet with original constraints retained; never silently drop a requested clause. |
| Named sources | Detect catalog identifiers and titles, resolve them within explicit owner/project context, then owner scope, then the active project derived from the server session/repository context. | `candidates` or `unresolved`; no first global match. |
| Scope/code intent | Use caller scope and explicit current-code/implementation wording; never infer current code merely from an RDR's implementation-status facet. | Keep corpus containment; do not widen. |

The recorded MVV question begins “Synthesize RDR-084…” and contains **no `ART`
project token**. The unmodified text therefore remains ambiguous over the ART
and Nexus RDR-084 documents. The MVV must keep that question verbatim and add
an explicit `owner="1.2"` (or `project="ART"`) binding through `bindings`; the
RDR cannot pretend the prose supplied it. Resolve that binding through catalog
owner/project identity, then apply RDR basename/admission rules. If it resolves
to multiple owners or no owner, disclose ambiguity rather than choosing Nexus
RDR-084. `scope="rdr"` alone is never a project selector. When no explicit
qualifier or reliable active project exists, return an ambiguity result.

Research 10's A5 spike supplied the literal string `ART` and absolute ART path
to its resolver. It verifies owner-scoped resolution given that input, not
extraction of a qualifier from the recorded question. The active-project tier
is a separate unverified path and is not used by the MVV.

So the call exactly as filed is fixed only when the active-project tier works.
Until then that call returns an ambiguity result naming both RDR-084
candidates, which is honest but not the requested synthesis. Phase 2 builds
and verifies the active-project tier as part of A5, and its proof includes the
unmodified call.

How the active-project tier decides (added 2026-10-26, nexus-jypmj.2.4,
approved by Sam). The session's project is reliable only in Claude Code, where
the server starts in the session's project directory and that directory maps
to a registered catalog owner; Desktop and Cowork start it elsewhere, so there
the tier never applies and the caller passes `owner` or `project`. A reliable
project qualifies an unqualified source the way an explicit owner would, but
it is a guess about intent, so the question's own words can overrule it. If
the question names a different registered project, or carries a title of two
or more distinctive words that fits another project's document and not the
session project's, the tier declines and the result is the ambiguity naming
every candidate; the title never picks a document. With no such evidence, a
reliable session resolves its own project's RDR and discloses the inference.
If the session's project holds no such RDR, resolution falls back to the
unqualified lookup and the answer text says the active project was not used.
An inferred owner never narrows the secondary streams. RDR ids read from the
question text go through the same tiers as a `source` binding, at most three
per request (a calibration parameter for Phase 2 measurement), and a range
such as "RDR-078 through RDR-095" is disclosed as not expanded.

`scope` cannot carry the qualifier for this call: it takes one token, either a
corpus prefix such as `rdr` or a catalog subtree such as `1.2`
(`_nx_answer_normalize_scope`, research 16), and this call needs both. The
owner therefore travels in the reserved `owner`/`project` binding. When a
caller passes a catalog-subtree `scope` and no `owner` binding, the subtree is
also used as the owner qualifier for source resolution. When both are given,
they agree if one tumbler is a prefix of the other (`1.2.4686` agrees with
`1.2`) and resolution uses the longer; otherwise the run reports the conflict
in the envelope instead of picking one.

Deterministic extraction has no model cost. If structured extraction is needed,
it is at most one direct operator call, never recursive `nx_answer`; it is
bounded by the same overall deadline and reported separately from plan-match
latency. Failure returns the conservative unknown/fallback states above rather
than blocking all retrieval. A5 covers behavioral verification of this stage.

#### 2. Named primary-source coverage

A named source is the document or artifact the question is about, not merely a
high-scoring search hit. Resolve identifiers such as `RDR-084` through a new
bounded, project-aware resolver before graph expansion. Reuse
`rdr_canonical`'s basename identity, current-owner/`docs/rdr/` admission, and
no-guess ambiguity rules over that bounded candidate set. Do not use its
existing caller's unbounded content-type listings, or generic
`resolve_tumbler`'s first exact match. `scope="rdr"` identifies a corpus
family, not a project. Ambiguity produces candidates or an honest unresolved
state, never an arbitrary project match.

For a resolved primary source:

- Read ordered manifest rows, group them by each row's collection, and
  batch-hydrate their chunks. Manifests supply order and spans, not headings.
- Derive document-scoped section labels from chunk **content** in manifest
  order. A chunk containing a Markdown heading starts that section; subsequent
  non-heading chunks inherit the current section. A document can have multiple
  labeled sections even when only one chunk contains headings. Keep an
  `unlabeled` state for content before the first heading and for formats with
  no recoverable headings. Do not trust T3 `section_title` unvalidated:
  identical text shared by documents has one chash, and metadata merges can
  give that chash another document's title. Academic `section_type` is also
  insufficient because many RDR headings classify as `other`. The wider
  document-scoped metadata bug and the runner's existing `section_types`
  consumer are tracked in `nexus-tko2w`.
- A metadata-only document-scoped section mapping may replace content-derived
  labels later, but shared-chash metadata alone is not a valid substitute.
- Use positional diversity when headings are absent, and record metadata as
  partial rather than treating missing headings as an error or covered facet.
- Reserve a portion of the evidence budget for the primary source before adding
  secondary streams. For a synthesis, seek the decision, evidence/research,
  implementation status, and open-question sections that actually exist; never
  assume a template heading is present.
- Preserve the original vector hits alongside manifest-selected context. If
  the document exceeds the budget, retain diversity and mark partial coverage;
  do not truncate solely to the document prefix.
- Do not treat `store_get_many`'s display-truncated body as complete evidence
  when assessing a facet. Section diversity is not facet coverage.

The budget is in tokens or characters at prompt construction, with chunk counts
as an additional bound. Cap total named sources, total manifest rows/chunks
hydrated, total fetched characters, and final selected evidence; a per-document
cap alone does not bound `named_sources` plural. Deduplicate shared chashes
without losing per-document position and source attribution. Current APIs may
require fetching a large document's chunks before metadata selection; a
metadata-only projection is an optional optimization, not a prerequisite. The
existing traversal and operator-count caps remain; they are not substitutes for
these budgets. Initial numeric limits are calibration parameters derived from
current operator and bundle limits, not unmeasured constants locked by this RDR.

The primary stream enters at the runner, not as a plan step, so it applies to
every plan that runs through `plan_run`: builtin, grown and inline-planned.
`single_query` plans have no operator step and therefore no reduction to feed;
they are not given a primary stream. Before the first operator that consumes
retrieved evidence, the runner merges the reserved primary evidence (and the
T2 records below) ahead of the plan's own inputs. Operators receive evidence
in two shapes, and the merge covers both:

- **`ids` auto-hydration** (`runner.py:1940-1978`): the existing positional
  cap of `_OPERATOR_MAX_INPUTS` (100, `runner.py:1446`) applies after the
  merge, so it cuts only secondary items.
- **Explicit `inputs`** from earlier `store_get_many` steps
  (`runner.py:2010-2040`; plan 473's `rank` step uses this shape): today this
  branch has no count cap. The merge adds the same cap there, applied the
  same way.

The reserved share is a calibration parameter and must stay below
`_OPERATOR_MAX_INPUTS`, so plan evidence always keeps some room. Every merged
item, reserved or not, also counts against the aggregate character budget
above and the bundle prompt limit. Deduplication by chash keeps the primary
copy and its attribution. The continuation path re-runs the same hydration
helper (`plans/continuation_envelope.py:302-309`) and must apply the same
merge so both reduction routes see the same evidence.

T2 is outside the corpus graph. When a named source resolves to an RDR, the
runner adds a third stream: that RDR's T2 records (its metadata record and its
latest gate record) from the `{repo}_rdr` project of the resolved source's
owner. In Phase 1 the trigger is the explicit RDR `SourceRef` alone; from
Phase 2 the stream is added only when the request also has a gate, lifecycle
or status facet. The T2 read goes through the same tenant-scoped T2 client as
every other T2 read in `nx_answer` (inferred, not read: the client's tenant
check was not traced), and a source in another tenant gets no T2 stream. The
stream contributes a small fixed number of records (a calibration parameter)
inside the reserved share, so truncation cannot evict it. It applies only to
RDR sources. It must not be replaced by code links or treated as an ordinary
T3 document. Assumption A7 covers it.

#### 2a. Caller scope over plan-declared corpora

Today a plan-declared corpus beats the caller's scope (nexus-zs1d's rule,
`plans/runner.py:1156-1190`). This RDR narrows that rule. Where the caller
gave no scope, nothing changes: the plan's corpus, then the RDR-200 default
`knowledge,code,docs,rdr`, apply as now. Where the caller gave a corpus-prefix
scope, it narrows the resolved corpus of every corpus-taking retrieval step
(`_CORPUS_QUERY_TOOLS`: `search`, `query` and the scoped search primitives;
`store_get_many` takes no corpus and is unaffected). The resolved corpus is
whatever the step has after binding resolution and `scope.taxonomy_domain`,
so a domain-derived corpus is narrowed the same way. Matching is by corpus
prefix: a plan entry matches when it is the prefix or a collection name
under it (`rdr__...` matches `rdr`).

| Resolved step corpus | Caller scope `rdr` | Result |
| --- | --- | --- |
| none | `rdr` | `rdr` (today's behavior) |
| `all` | `rdr` | `rdr`: `all` is no restriction |
| a list with entries under `rdr` | `rdr` | those entries |
| entries none of which is under `rdr` | `rdr` | scope conflict |

A scope conflict does not run the step unscoped and does not drop the
caller's scope: the step is skipped and contributes no evidence. In Phase 1
the conflict is reported in the envelope diagnostics and logs, with the plan's
corpus and the caller's scope; from Phase 3 it is also the ledger's scope row
(`conflict`). If every retrieval step conflicts, the run takes the existing
empty-retrieval path (no-match notice) and the notice names the conflict.
Steps pinned through `collection`/`collections` keep today's behavior (the
plan wins) and are reported as unscoped in the diagnostics; narrowing them is
not part of this RDR. A catalog-subtree scope (for example `1.2`) is not a
corpus and is not intersected with one (nexus-jypmj.1.16). Forwarded as
`corpus` it resolved to no collection, so a step with no corpus of its own
searched nothing. `query` and `search_metadata_scoped` take it as their own
`subtree` filter whatever the step's corpus pin (none, `all` or a list), and
the corpus is left as it was, or the RDR-200 default when the step named none.
The other retrieval tools (`search`, `search_topic_scoped`,
`search_graph_hop`, `search_aspect_scoped`) have no subtree filter: the step
runs as before and the text notice names it, as the envelope does. A
document-level address (`1.2.4686`) is never a filter, because `query` rejects
it; it is not passed or cut to its owner, and every retrieval step is reported
as not applied. A subtree never narrows by owner (see the owner paragraph at
the end of this section).

The rule lives in two places, because single-step `query` plans never reach
the runner. `nx_answer` classifies a matched plan with exactly one `query`
step as `single_query` and runs it on a fast path that reads the step's corpus
or the RDR-200 default and passes the caller's scope as `subtree`
(`mcp/core.py:9468-9505`, `11740-11800`). Both `corpus-coverage-check` and
`document-discovery` take that path. The fast path applies the same
narrowing and conflict rule to its corpus before calling `query`; a
corpus-prefix scope then goes into `corpus`, not `subtree`. A subtree scope
goes into `subtree` whatever the corpus pin, by the same function the runner
uses, so the two report the same outcome (`subtree_filtered`, or
`subtree_not_applied` for a document-level address). The continuation
path re-runs the runner's helpers (`plans/continuation_envelope.py:302-309`)
and gets the rule through `_apply_caller_scope_to_args`.

The builtin YAMLs keep their pins. Because `all` narrows like no restriction,
removing them is unnecessary for scope, and it would change unscoped
behavior: the taxonomy-domain steps would take a domain corpus, and the two
`search_metadata_scoped` plans would fall to that tool's default
`knowledge,code,docs`, losing `rdr` (research 17). Source census: research 14.

An explicit owner also narrows the secondary streams (added 2026-10-26,
nexus-jypmj.1.18). The Phase 1 canary showed why: with `owner="1.2"` given,
28 of 40 G06 vector hits still came from another project's RDR collection.
Only a resolved `owner` or `project` binding narrows. A catalog-subtree
`scope` alone does not, and neither does an owner that resolution inferred;
an unresolved or conflicting qualifier narrows nothing and says so. After the
corpus rule above, a retrieval step keeps only those `code`, `docs` and `rdr`
collections whose owner segment matches; `knowledge` collections pass, and a
collection with no tumbler-shaped owner is kept and reported as unattributed.
The traversal policy drops non-seed nodes outside the owner only when they sit
in a project-owned collection, so cited papers still arrive. A step left with
no collection takes the conflict path above: it is skipped and the notice
gives the cause. Steps pinned through `collection`/`collections` run as
written and are reported as unscoped. The decision record is T2
`nexus/rdr232-1.18-owner-narrowing-decision-2026-10-26`.

#### 3. Traversal as a bounded secondary stream

Introduce an explicit traversal evidence policy rather than pretending
`traverse` has a corpus parameter today:

```text
TraversalPolicy(
    allowed_collections: ScopeConstraint,
    preserve_seed_documents: true,
    max_documents: calibrated,
    max_chunks: calibrated,
    require_link_role: factual | implementation | citation | related,
)
```

The policy is applied **inside `traverse` before manifest expansion**, not by
filtering already-truncated results. `traverse` currently returns a sorted
union of collections and caps ids in graph order. A runner-only filter would
lose in-scope evidence before the policy saw it. The engine's `graphBFS` also
truncates at 500 nodes by `(min_depth, tumbler)` before the client sees them;
the observed run's 340 references equal its search limit of 40 plus the
client's 300-id cap, which suggests that cap was reached (inferred from the
arithmetic, not read from the run), and no evidence yet shows real
traversals reaching 500 nodes.

Direct MCP `traverse` keeps its current behavior when the optional policy is
absent. With a policy it:

1. Reports a response carrying exactly `MAX_GRAPH_NODES` (500) nodes as a
   possibly truncated traversal, and counts those responses in its own
   diagnostics. (The engine's `event=graph_node_limit` log fires whenever the
   reachable set is at least 500, `CatalogRepository.java:4564-4569`, but
   carries no caller, so it cannot isolate `nx_answer` traversals.) Only if
   those counts show capped responses does a gated step add the
   allowed-collection predicate **during the graph walk**, before truncation.
   That is a scoped filter on the existing engine traversal, not a new graph
   algorithm: `graphBFS` already reads each node's document, and
   `physical_collection` is a column of that document row, so no join is
   needed. It needs a Java query change and an engine release; `nx_answer`
   then negotiates a dedicated `catalog_graph_scope_filter` capability and
   requires a scoped-result marker or readback count proving the predicate
   ran. An older engine that ignores the parameter must not be treated as
   contained: disclose incomplete traversal and omit secondary graph evidence
   rather than claiming success.
2. Ranks and caps **documents**, not arbitrary manifest chunk order. Use link
   role and link distance as the available signals; `traverse` has no search
   relevance input, so none is invented. Use tumbler only as a deterministic
   final tie-break.
3. Expands manifests only for the retained documents, then applies the
   secondary chunk budget with per-document reservations.
4. Returns per-id collection alignment or document-grouped ids so hydration
   cannot reconstruct scope from a union of collection names. Existing
   no-policy results retain the legacy shape.

This policy contains the graph stream only. The vector stream's scope comes
from section 2a; containing traversal alone would leave plan-pinned searches
unscoped. `nx_answer` always supplies the policy derived from request and plan. A6's
fixture proves the client-side ordering against the 300-id cap; the engine's
500-node pre-cap filter is the gated Phase 1 step 6.

Default rules:

- Explicit caller scope constrains secondary evidence too.
- Cross-type expansion requires a corresponding request facet or an explicit
  plan role. For example, current-code behavior may admit `implements` links;
  historical gate status does not.
- Seed/primary documents are kept in their own stream and cannot be evicted by
  graph results.
- Graph documents are ranked and capped before hydration using link role,
  distance, and tumbler as deterministic tie-breaks. No arbitrary catalog-order
  truncation.
- Any admitted scope widening is recorded with its reason and resulting corpus
  counts.

This fixes policy at the traversal boundary without requiring a second catalog
graph walk. The engine filter and any future engine-side graph budget share the
same contract.

#### 4. Evidence coverage and one repair

Before terminal generation, derive a coverage ledger:

| Facet or required source | Evidence ids | State |
| --- | --- | --- |
| Named primary document | Manifest/chash anchors | covered / partial / missing |
| Each requested facet | Supporting chunk or T2 record anchors | covered / unsupported / contradicted |
| Explicit scope | Observed collection counts per stream | satisfied / widened / conflict / violated |

`covered` means evidence exists for the facet, not that the answer's claim is
true. `contradicted` is retained for the reduction; it is not repaired into
agreement. Coverage evaluation uses existing structured operator dispatch with
deterministic checks for source presence and scope. It must not issue another
`nx_answer` call.

The terminal result carries one of four coverage states. The state is
independent of prose fluency.

| `coverage.state` | When | `answer_shape` | Text returned | Plan counter | Plan growth |
| --- | --- | --- | --- | --- | --- |
| `complete` | every required facet has evidence | `answered` | the answer | `success_count` | allowed |
| `partial` | at least one required facet is unsupported or contradicted, and a usable answer exists | `insufficient_evidence` | a notice naming each unresolved facet, followed by the answer | `success_count` | blocked |
| `unavailable` | no usable answer can be produced | `uncovered` when retrieval returned evidence and none of the items read supported or contradicted a required facet (prose with no such facet included); items the check's per-call size cap left unread are counted and disclosed, not assessed; `no_evidence` when no retrieval result reached the reduction | the existing non-answer notice and chunk references | `failure_count` | blocked |
| `unassessed` | the request has no facets, or the coverage assessment itself failed | `answered` | the answer, as today | `success_count` | as today |

A **usable answer** exists when the terminal generation returned prose that
`classify_answer_shape` reads as `answered` and at least one required facet
is covered or contradicted: contradicting evidence is evidence the reduction
must state, so a contradiction is reported ahead of the prose rather than
withheld (Sam, 2026-10-27, nexus-jypmj.3.13). Prose with no such facet is
`unavailable`. A repair or
reassessment call that fails after the first assessment succeeded leaves the
first ledger in force, so the result is `partial` (or `complete`), not
`unassessed`. A missing named source, or a scope conflict that removed all
evidence, counts as uncovered facets under these same rules.

The counter and growth columns apply to different runs, because growth happens
only on an inline-planned run (`plan_id == 0`), and plan counters move only on
a library plan (`mcp/core.py:9826`, `12537`; research 17). For a library plan
the counter column applies; for an inline-planned run the growth column does.

The partial notice opens with a stable marker, `[insufficient-evidence:
<n> unresolved]`, so `nx answer-runs`, which classifies rows by their
recorded text (`commands/answer_runs.py:226`), can bucket them. As built, a
row is bucketed from its run summary's `coverage_partial` count when the
summary is present, for either `trace` value; without a summary only a
`trace=True` row can be bucketed by its marker, because a `trace=False` row's
recorded text is redacted whole.
`nx_answer` sets `answer_shape` from the coverage state, not by classifying
the final text.

A partial answer is still returned. Gap 3 asks for missing facets to be
reported, not for the covered ones to be withheld; the notice comes first so
no reader takes the prose as complete. `unassessed` keeps an assessor outage
from turning every answer into a notice, and the coverage object says plainly
that nothing was assessed; no facet is marked covered by default. A generation
failure keeps the existing error shape. `coverage` remains the detailed facet
ledger, not a redundant side channel that consumers can ignore.

`INSUFFICIENT_EVIDENCE` is added to `AnswerShape` but **not** to
`NON_ANSWER_SHAPES`. That set couples three effects: it records
`success=False`, never grows the plan, and replaces the prose with a notice
(`plans/answer_shape.py:98-112`, `mcp/core.py:12455-12500`, research 15). A
partial answer needs only the no-grow effect, so growth is blocked by an
explicit no-grow flag on the result instead, consulted where growth is
decided (`mcp/core.py:12537`). `nx answer-runs` classifies
`insufficient_evidence` rows in their own bucket, not as degenerate. The
vocabulary test `tests/plans/test_answer_shape.py:95` asserts that `ANSWERED`
is the only shape outside `NON_ANSWER_SHAPES`; it and the `AnswerShape`
docstring are updated to say that `ANSWERED` and `INSUFFICIENT_EVIDENCE` both
carry prose.

The counters keep their structural meaning, with one consequence spelled out.
The live consumer of `failure_count` is the matcher's always-failing skip:
`_is_always_failing` drops a plan from matching when it has zero successes and
at least `_ALWAYS_FAILING_MIN_FAILURES` (3) failures (`plans/matcher.py:139`,
`170-177`). Counting partial answers as failures would let a synthesis plan
that usually covers five of six facets disappear from matching, and those
requests would fall to the inline planner. Partial therefore counts as a
success; only `unavailable` counts as a failure. `promote.evaluate_gates` has
no production caller (research 15; RDR-189 R9), so this RDR does not change
`promote.py`. No third plan counter or plans-table migration is introduced.

Coverage outcomes live in the **run summary**, not new columns on `plans`:
`coverage_complete`, `coverage_partial`, `coverage_unavailable` and
`coverage_unassessed` are counts aggregated from the run's terminal coverage
assessment. Historical counters are not rewritten. These counts, not the
execution counters, decide whether a run can serve as a positive
answer-quality exemplar, and they supply observed answer-shape evidence for
RDR-189's family research.

Assessment cost is bounded independently of facet count. Group facets into one
structured assessment per evidence stream, with a calibrated cap on facets per
call and one repair/reassessment cycle. The motivating six-facet request must
not imply six unconditional model calls. Record assessment calls, facets per
call, and assessment latency in the run summary. As built, assessment USD is
not a summary field: it is in the run's step records (the `operator_check`
steps numbered past the plan's own steps).

*Departure, 2026-10-28 (nexus-jypmj.17).* The call count is no longer one per
evidence stream. A stream of more than 20 anchors is split into balanced packs
of at most 20, each its own call over the same facet group, and the calls run
concurrently, at most 4 at a time (fewer when an API backend's
`NX_LLM_MAX_CONCURRENCY` is lower). The number of calls is the sum over streams
of the packs times the facet groups, and no constant caps it: what bounds the
stage is its 0.4 share of the deadline left and the USD stop-line, not the
design. Under a tight allowance the later streams in plan order (graph, other)
are the ones left unassessed. The 20-anchor pack size and the 4 concurrent calls
are unmeasured as accuracy and latency.

The boundary must be runner-owned, before terminal reduction. Two adjacent
operator steps bundle and expose only their terminal result, so adding
`check → generate` to plan YAML would hide the ledger from repair logic. The
runner must either provide a pre-reduction hook or factor a resumable execution
state that retains prior step outputs, resolved bindings, `StepRecord`s, and
evidence anchors. It must not execute the plan prefix twice.

Use a typed coverage request and ledger, not `check.ok` as a proxy for coverage.
Preserve `{item_id, chash, collection, tumbler or T2 record id, content}` for
each candidate and reject returned ids outside that input. `check` can supply
per-facet evidence mappings through those ids. `verify` can assess a claim but
its citations are textual locators, not durable anchors. `extract` requires a
real facet-ledger output schema if used for this purpose. RDR-189 P1 (RDR-189
accepted 2026-10-24) owns normalization of `check`/`verify` to `(tumbler, chash)` witness
anchors; this design's additional `item_id` is only a request-local join key,
not a competing durable citation format.

If required coverage is missing:

1. Emit targeted retrieval queries for the missing facets against the primary
   source and explicit scope first.
2. Merge the results within the same evidence budgets.
3. Recompute the ledger once.
4. Generate once with all covered evidence and an explicit list of unresolved
   facets.

The repair path gets its own allowance inside the original monotonic deadline
and one shared USD accumulator spanning prefix, assessment, repair, and
reduction. It is not a second full plan budget. Existing USD enforcement checks
before segments and is a stop-line rather than a hard ceiling; unknown-cost
steps remain a disclosed accounting limitation. As built, the continuation
fallback re-run, which re-executes the plan without the cut, repays the prefix,
assessment and repair against the same remaining USD; this is logged, not
prevented. Record coverage/repair usage in
the existing step accounting vocabulary rather than inventing an unversioned
step source. If repair is unaffordable, skip it and generate once from the
evidence already held; the result then follows the coverage-state table above
(`partial` with the unresolved facets listed ahead of the prose when a usable
answer exists, `unavailable` when none does). Do not claim `answered` solely
because generation produced prose.

The structured envelope gains a small `coverage` object: requested and covered
facet ids, primary-source state, observed corpus counts, repair attempts, and
residual gaps. Full evidence text is not duplicated into telemetry; when
`trace=False`, facet text and source hints are redacted as well. `answer_shape`
and the detailed `coverage.state` follow the coverage-state table above: a
partial result is never labeled `answered`, and its text is the
unresolved-facet notice followed by the answer. Continuation
responses carry the same coverage state rather than revert to unassessed.

Persistence uses one nullable, versioned run-summary JSONB column on
`nx_answer_runs`, added through a new Liquibase changeset with jOOQ and the
migration DDL ratchet updated. Thread the same summary through client payloads,
both `/record` and `/complete`, repository insert/query, and single/batch
import. Otherwise it is write-only or lost during migration. This is additive
schema evolution on the existing run table, not a new table, second POST, or
follow-up update. On `/complete`, it remains in RDR-203's transaction with step
children and plan counters; `/record` paths such as planner failure,
continuation handoff, and `nx_answer_report` need the same field.

Advertise an independent `/version` capability in the existing cached response
and cloud-edge allowlist. Refresh the capability on `/version` changes and at a
bounded interval; a cached flag alone is not durable proof across an engine
rollback. When the caller requests persisted coverage, read back the run
summary after the same composite operation and set `persistence=persisted`
only if it matches the version and content digest. The digest uses a canonical
JSON form: recursively sorted object keys and normalized numeric values, with a
documented treatment of integer-versus-float representations. A missing/mismatched summary
becomes `persistence=dropped` with a warning, never silent success. New client
+ old engine omits the summary, logs the persistence drop, preserves the
existing record, and exposes `persistence=unsupported` in the live envelope.
An old engine may silently discard unknown keys, so HTTP 200 is not evidence
of persistence. Old client + new engine is unchanged because the column and
wire field are nullable. As built, two further states: `unverified` when a
summary was sent and no readback was requested (a text-mode call never reads
back), and `not_recorded` when no run record was written. Returned coverage and persisted coverage are distinct
states. Readback is a read, not a second telemetry write or update.

#### 5. Evaluation before tuning

Create a versioned golden set of real requests with labeled answer kind,
required sources, required facets, allowed corpora, and unacceptable plan
matches. Extend `scripts/bench` and its labeled query format rather than
creating a parallel runner. Reuse the RDR-090 five-query spike and RDR-196
operator proxy where they measure their existing axes, but add the new
coverage labels those harnesses do not carry. Include the observed ART
RDR-084 request verbatim as a regression fixture, with its source corpus
available to the test.

Measure separately:

- **eligible-plan recall**: whether at least one acceptable plan survives;
- **wrong-shape admission rate**: incompatible plans selected;
- **primary-source coverage**: required sources represented before reduction;
- **facet evidence coverage**: requested facets with evidence before reduction;
- **scope leakage**: evidence admitted outside explicit constraints without a
  recorded, permitted reason;
- **repair yield and cost**: missing facets repaired within one attempt;
- **terminal completeness**: required facets addressed or disclosed as missing.

Do not optimize match confidence as a proxy for these measures. Historical plan
success may rank only candidates that pass the same eligibility gates. The new
coverage outcome counts, not the older execution-success counters, decide
whether a run can serve as a positive answer-quality exemplar.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Answer-kind compatibility | `plans/matcher.py`, `plans/schema.py`, `seed_loader.py`, builtin YAML, grown saves | Extend: validated top-level `plan_json` declaration; eligibility on cosine/category/FTS and planner few-shots before cost ranking. Never put kind in identity dimensions. |
| Request facet extraction | `mcp/core.py`, existing structured operator dispatch | Extend narrowly: deterministic-first request kind/facet/source extraction and explicit unknown-request route; optional one direct structured call; not a general RDR-189 target registry. |
| Named-source resolution | `rdr_canonical` admission/winner policy, owner-scope lookup, bounded catalog lookup, manifest APIs | Extend with bounded project-aware resolution; resolve explicit project qualifiers and no-guess ambiguity; do not reuse unbounded resolution or global first-exact selection. |
| Caller scope on retrieval steps | `plans/runner.py::_apply_caller_scope_to_args` (also reached by `continuation_envelope.py`), the `single_query` fast path in `mcp/core.py` | Narrow the resolved corpus by caller scope at both sites; skip and disclose a step whose intersection is empty. Builtin YAML pins stay. |
| Traversal containment | `mcp/core.py::traverse`, catalog nodes/manifests | Apply policy inside traverse before manifest expansion, with per-id alignment; preserve legacy no-policy behavior. Runner-only post-filtering is insufficient. |
| Evidence budgets | `store_get_many`, runner tumbler hydration, `_OPERATOR_MAX_INPUTS`, bundle prompt construction | Extend with a runner-level merge of the reserved primary stream ahead of plan inputs, so positional truncation cuts only secondary items; source-aware reservations and content-derived document-scoped section labels; manifest order + positional fallback; shared-chash T3 titles are not authoritative. |
| Facet coverage | `check`, `verify`, `extract`, source annotations, runner segmentation | Reuse direct operator dispatch; add runner-owned pre-reduction state/hook and typed anchors. Do not rely on bundled intermediate output or recursive `nx_answer`. |
| Coverage telemetry | RDR-203 composite run record, engine telemetry, structured envelope | Add nullable versioned run-summary JSONB via Liquibase; thread record/complete/read/import; capability-negotiated. No new table or extra write. |
| Generic fallback | Inline planner | Extend with `RequestCoverage`; no existing no-model generic synthesis fallback was found. |
| Terminal answer classifier | `plans/answer_shape.py`, `mcp/core.py`, `commands/answer_runs.py` | Extend: add `INSUFFICIENT_EVIDENCE` to `AnswerShape` but not to `NON_ANSWER_SHAPES`; prose kept behind an unresolved-facet notice; an explicit no-grow flag; its own read-side bucket. Partial counts as success, `unavailable` as failure; run coverage outcomes stay separate. |
| Routing/coverage evaluation | `scripts/bench`, `bench/queries`, RDR-196 operator proxy | Extend: retain retrieval and operator metrics; add answer-kind, primary-source, facet, scope, and unacceptable-plan labels. |

### Decision Rationale

The cheapest reliable improvement is to make wrong evidence mixes hard to
construct. That is why primary-source reservation and traversal policy precede
any attempt to tune semantic plan matching. They also make a wrong plan match
less damaging.

Answer-shape eligibility addresses the part retrieval cannot. A lookup plan
with perfect evidence is still the wrong plan for a multi-facet synthesis.
Filtering before ranking is preferable to increasing the match threshold:
thresholds measure similarity, not compatibility, and previous work has already
shown that absolute cosine is weak for category-plan selection.

The coverage ledger closes the remaining gap without introducing an unbounded
agent loop. One repair supplies a clear cost bound and an observable outcome.
Residual gaps become explicit output instead of another silent quality failure.

## Alternatives Considered

### Alternative 1: Tune embeddings, thresholds, and scope weights

**Description**: Keep the current plan contract and adjust similarity scoring
until good plans win more often.

**Pros**: Small change to the matcher; uses existing calibration practice.

**Cons**: Similarity cannot express answer-shape incompatibility. It also does
nothing about graph expansion or missing evidence facets. The observed call
already had explicit RDR scope and still selected a lookup plan.

**Reason for rejection**: Useful only for ranking candidates that are already
eligible. It is not a substitute for the proposed gates.

### Alternative 2: Add a model-based plan-selection judge

**Description**: Ask a model to rank the top candidates by question fit before
execution.

**Pros**: Can interpret nuanced requests and plan descriptions; little schema
change.

**Cons**: Adds cost and latency to every match or ambiguous match; inherits the
same stochastic selection problem; cannot enforce corpus containment; historical
success can reinforce prior mistakes.

**Reason for rejection as the primary remedy**: AgenticScholar §4.2 documents
exactly this two-stage pattern (semantic filtering, then LLM intent-alignment
rerank with a high confidence threshold), so the option is credible — but its
judge assesses analytical-intent alignment, not answer-kind compatibility, and
it cannot enforce corpus containment. A limited judge may be evaluated later
for ambiguous eligible candidates. Deterministic contracts and evidence checks
come first.

### Alternative 3: Reduce over a larger undifferentiated context

**Description**: Hydrate more chunks and rely on a long-context model to find
what matters.

**Pros**: Simple; can improve recall when relevant chunks were merely capped.

**Cons**: The observed failure already had hundreds of chunks but almost no
primary evidence. More context increases cost and crowding and does not ensure
section or facet coverage.

**Reason for rejection**: Budgeted, source-aware evidence is required before
more context is useful.

### Briefly Rejected

- **Route everything through inline planning**: removes useful plan reuse and
  still needs evidence containment and coverage checks.
- **Disable traversal**: removes legitimate citation and implementation recall
  to avoid a policy defect; bounded secondary traversal is the narrower fix.
- **Adopt all of RDR-189 first**: couples a measured routing defect to an
  unresolved general architecture. This RDR leaves compatible primitives for
  that work without requiring it.
- **Add another final-answer judge only**: detects some omissions after paying
  for generation but cannot repair missing retrieval evidence or prevent a
  wrong-shape plan from running.

## Trade-offs

### Consequences

- More requests may fall through to inline planning when the only similar saved
  plan has the wrong answer shape. That is an intended precision/latency trade.
  Phase 2 measured its size: replayed over 143 real runs, the gate removes the
  selected plan from 50% of runs with first-pass kinds, 42% with Opus and 38%
  with Haiku extraction (the shipped configuration). Plan 473 is the only
  `lookup` builtin, and every existing grown plan decodes to `unknown`.
- Source reservation gives less space to secondary evidence. It should improve
  source-grounded answers but may reduce broad contextual discovery unless the
  caller asks for it.
- Coverage extraction and repair add model and retrieval work. Request
  extraction runs on about two thirds of real requests (one Haiku call each).
  The one-repair bound and existing call budgets prevent an open-ended loop.
- Explicit answer-kind declarations add a small authoring obligation to builtin
  plans. Unknown grown plans remain supported rather than forcing a migration
  guess.
- Coverage state gives callers a useful distinction between “the plan ran” and
  “the request was covered,” but also exposes gaps that current prose answers
  can hide.

### Risks and Mitigations

- **Risk**: Request-shape inference misclassifies a nuanced question.
  **Mitigation**: conservative `unknown`, explicit constraints take precedence,
  generic fallback remains available, and golden fixtures include ambiguous
  phrasing. Never use a hard classification to return an empty answer.
- **Risk**: A hard scope rule blocks evidence needed for current-code questions.
  **Mitigation**: infer expansion only from an explicit code/implementation
  facet or plan role, record the widening, and test both historical-RDR and
  current-code requests.
- **Risk**: Source identifiers collide across projects.
  **Mitigation**: scope-aware catalog resolution and ambiguity reporting; never
  pick the first global RDR match.
- **Risk**: Facet coverage becomes a model grading its own retrieval.
  **Mitigation**: deterministic source/scope checks, evidence anchors for every
  covered facet, golden labels independent of the implementation, and a
  disclosed residual-gap state.
- **Risk**: Repair doubles cost on difficult questions.
  **Mitigation**: one attempt, dedicated sub-budget, no recursive `nx_answer`,
  and skip repair when the existing deadline or USD budget cannot accommodate
  it.
- **Risk**: New declarations fragment plan identity or invalidate caches.
  **Mitigation**: keep answer kind out of `dimensions`; store a versioned
  declaration in `plan_json`, decode absent/invalid legacy values as `unknown`,
  and test seed reconciliation, grown saves, and cache refresh explicitly.

### Failure Modes

- **Primary source unresolved**: return candidates or a disclosed unresolved
  state; do not silently substitute a same-numbered source from another
  project.
- **No eligible plan**: inline-plan with the coverage object; if that fails,
  return the existing planner error with eligibility diagnostics.
- **Traversal policy failure**: keep the primary/vector stream and omit the
  secondary stream with a warning. Do not fall back to unrestricted traversal.
- **Coverage assessment fails**: use deterministic source/scope checks,
  return the answer with `coverage.state="unassessed"`, and say that facet
  coverage was not assessed; do not mark facets covered by default.
- **Repair exhausts budget**: skip repair and generate once from available
  evidence: `partial` with the unresolved facets listed ahead of the prose when
  a usable answer exists, `unavailable` when none does.
- **Older engine lacks new run fields**: omit the summary after capability
  negotiation, preserve the current run record, and expose
  `persistence=unsupported`; HTTP 200 alone does not prove persistence. Do not
  add a second write or turn a telemetry failure into a retrieval failure.
- **Coverage inserted as an ordinary adjacent operator**: bundling hides its
  intermediate output. The runner-owned pre-reduction boundary is mandatory;
  never substitute a second full `plan_run` that repeats model spend.

## Implementation Plan

### Prerequisites

- [x] Run bounded A1–A6 research spikes and record findings through
  `/conexus:rdr-research` (research 6–10). These are partial prototype
  verifications, not proof of production integration or live answer quality.
- [ ] Build the labeled golden set before choosing numeric thresholds or
  declaring a match-quality improvement.
- [ ] Reproduce the ART RDR-084 evidence mix in a self-contained fixture; do
  not depend on a mutable live corpus for the unit regression.
- [x] Exercise real manifest fixtures with and without headings, duplicate
  chashes, aggregate budgets, same-number RDRs across projects, and sibling
  artifacts. The shared-chash metadata anomaly is recorded in research 7;
  production candidate paging and oversized/missing-manifest cases remain for
  implementation tests.
- [x] Run the permitted A3 repair-path spike: runner-owned boundary,
  anchor-preserving ledger, at most one repair and one terminal reduction,
  shared accounting, and continuation behavior (research 8).
- [x] Review RDR-189 against this shared seam. Done: RDR-189 was accepted on
  2026-10-24 with a Shared Contract with RDR-232 section that supersedes its
  answer-shape-dimension option and records the split. RDR-232 owns request
  analysis/eligibility/containment; RDR-189 owns durable target families,
  host-bound witness normalization (P1), and post-realization
  preservation-trace semantics. RDR-232's routing capability derives from the
  target family once P2 exists; its live pre-reduction coverage ledger is
  input to P4's final-answer preservation assessment, not its substitute.
  Phase 3 witness-anchor work consumes P1's contract once P1 lands; if Phase 3
  lands first, it uses the explicitly versioned local subset without claiming
  RDR-189 compatibility.

The production run-summary migration and its old/new client-engine matrix are
Phase 3 step 3, not a prerequisite.

### Minimum Viable Validation

The end-to-end MVV belongs to Phase 3, when all required mechanisms exist.
Phases 1 and 2 have separate proofs and do not claim full facet coverage.

**Phase 1 proof:** using explicit `SourceRef` and `ScopeConstraint` inputs (the
full `RequestCoverage` extractor is Phase 2), resolve the correct ART RDR-084,
preserve section-diverse primary evidence, and keep a >300-out-of-scope-id
traversal from discarding in-scope evidence. With `scope="rdr"`, the vector
stream's corpus counts show only `rdr__` collections, on plan 473, on a
taxonomy-domain synthesis builtin such as `research-default`, and on the
`single_query` fast path. Record per-stream corpus counts and
reducer-input counts. No facet-coverage guarantee is claimed.

The traversal proof for Phase 1 is client-side:

- **Client:** >300 out-of-scope manifest ids precede in-scope evidence; policy
  runs before manifest expansion and preserves per-id collection alignment.
- **Engine cap disclosure:** an engine response with exactly 500 nodes (the
  `MAX_GRAPH_NODES` cap, `CatalogRepository.java:4442`) is reported as a
  possibly truncated traversal, never as contained.

The engine pre-cap filter is a separate, gated step (Phase 1 step 6). Its
proof, run only if that step is taken: >500 out-of-scope graph nodes precede
in-scope evidence; the negotiated predicate runs before `MAX_GRAPH_NODES`
truncation and returns a scoped-result marker. An old engine without the
capability yields disclosed incomplete traversal, never a containment claim.
That step needs an engine release; the engine-tag freeze from the RDR-007 A2
cutover lifts no earlier than 2026-10-26 13:46Z, so check its status before
scheduling one.

**Phase 2 proof:** the same request's answer kind and facets/constraints are
extracted, and an explicit `project="ART"` or `owner="1.2"` binding resolves
without guessing. The recorded question has no `ART` token, so this binding is
required rather than implied. The unmodified call, with no binding, resolves
through the active-project tier when the server session's project is ART, and
returns an ambiguity result otherwise: from an unreliable host, and from a
reliable session of another project, where the question's ART title vetoes the
inference (see "How the active-project tier decides" in section 1). A
title-less question from a reliable session of another project resolves that
project's RDR, disclosed as inferred. Lookup-only plan 473 is ineligible, and
unknown-grown plans cannot bypass the gate. A generic route or inline planner
receives the coverage request. No facet-coverage guarantee is claimed.

**Phase 3 end-to-end proof:** run the recorded ART RDR-084 request verbatim
with the same `scope="rdr"` and `content_type="rdr"` plus explicit
`project="ART"` or `owner="1.2"`. It must not select a lookup-only plan;
must include section-diverse ART RDR-084 and lifecycle evidence; must not fill
the evidence set with unrelated code; and must address or explicitly mark
unresolved every requested facet. The envelope shows `coverage.state`, the
ledger, any repair, and returned-versus-persisted summary status. This is the
first phase that claims the complete motivating behavior.

### Phase 1: Evidence containment and sufficiency

1. Accept explicit `SourceRef` and `ScopeConstraint` inputs for containment.
   In production they arrive as reserved `bindings` keys on the existing
   `nx_answer` call: `source` (an RDR id or catalog tumbler) and `owner` or
   `project` (the qualifier), next to the existing `scope`. No builtin plan
   declares a binding with those names today; grown and library plans were
   not enumerated, so validation rejects a plan that declares one. `nx_answer`
   removes them before building the run bindings and the availability set
   (`_nx_answer_caller_bindings`, `mcp/core.py:9740-9758`), so they never reach
   plan binding resolution or matching. Add named-source resolution
   and a source-aware evidence-stream model using catalog manifests. Full
   `RequestCoverage` extraction lands in Phase 2.
2. Apply caller scope over plan-declared corpora (section 2a) in the runner
   and on the `single_query` fast path, with conflict disclosure in the
   envelope.
3. Add traversal evidence policy inside `traverse` before manifest expansion,
   with per-id collection alignment and legacy no-policy results preserved.
   Disclose an engine response at the 500-node cap as possibly truncated.
4. Reserve primary-source and secondary-stream budgets, and merge the primary
   stream at the runner as section 2 specifies. Add the bounded T2 lifecycle
   stream for a resolved RDR source.
5. Add deterministic source/scope checks and per-stream/reducer-input
   diagnostics, plus the observed RDR-084 regression fixture. Phase 1 and 2
   diagnostics live in the returned envelope and logs only; they persist once
   Phase 3 adds the run summary.
6. Engine pre-cap graph filter, gated. The signal is step 3's count of
   `nx_answer` traversals that came back at the 500-node cap; the engine's
   `graph_node_limit` log has no caller field and can only corroborate. If any
   golden-set or live-canary traversal returns a capped response, or capped
   responses appear in real `nx_answer` traffic, add the predicate inside the catalog
   walk before `MAX_GRAPH_NODES` truncation: a Java query change, an engine
   release, dedicated capability negotiation, and the scoped-result proof
   above. The rest of Phase 1 does not wait for it.
7. Ship behind the existing `nx_answer` path, not a new user verb. Compare the
   golden set with the pre-change baseline before enabling facet repair.

### Phase 2: Answer-shape eligibility

1. Add the starter answer-kind declaration to builtin plans and conservative
   metadata for grown plans.
2. Derive request kind and facets; filter incompatible candidates before
   similarity/cost selection.
3. Pass the coverage object to the inline planner on miss.
4. Record candidate exclusion and fallback reasons in the run envelope.
5. Measure eligible-plan recall and wrong-shape admission on the golden set;
   do not ship a classifier whose only evidence is the motivating example.
6. Exit criteria, measured on the golden set with request kinds labeled
   independently of plan declarations (not each plan's own kind, as A1's
   fixture did): the request-kind classifier's `unknown` rate, its
   eligible-plan recall on lookup requests, and, replayed over recent
   `nx_answer_runs`, the share of real requests whose selected plan would now
   be excluded. The thresholds are set from the baseline measurement before
   the gate is enabled. All grown plans decode to `unknown` today, so report
   the drop in plan reuse on broad requests as its own number.
   As run (2026-10-27): the thresholds were set after the gate-on measurement,
   not before it. The golden baseline is in-sample, since the classifier was
   written with those requests in view, so a threshold drawn from it bounds
   nothing out of sample. A blind held-out set was written first, then all
   three kind sources were measured gate-off and gate-on, then the thresholds
   were set; three of them sit at the measured values (T2
   `nexus/rdr232-phase2-thresholds`). The only measured fall in wrong-shape
   admission is in-sample (golden 4 of 24 to 0, one of them a synthetic clone
   of plan 473); on the held-out set it is 1 of 29 before and after.
7. Build and verify the active-project tier of source resolution (A5).

### Phase 3: Bounded repair and quality reporting

1. Add facet-evidence assessment at a runner-owned pre-reduction boundary using
   existing direct operator dispatch and preserved typed anchors. Do not use a
   bundled intermediate as the host-side ledger.
2. Add one targeted retrieval repair and one final reduction within the shared
   deadline and cost accounting; do not execute the plan prefix twice.
3. Add nullable versioned run-summary JSONB via Liquibase; update jOOQ and the
   DDL ratchet, thread `/record`, `/complete`, readback and import, and add
   capability negotiation. Extend the structured envelope with returned versus
   persisted coverage states. Add `INSUFFICIENT_EVIDENCE` outside
   `NON_ANSWER_SHAPES`, the explicit no-grow flag, the unresolved-facet notice
   ahead of the prose with its stable marker, its `nx answer-runs` bucket,
   and the updated vocabulary test. Partial runs count as
   successes and `unavailable` runs as failures, so `_is_always_failing` skips
   only plans that never produce a usable answer. No plans-table counter
   migration and no `promote.py` change: coverage outcomes are derived from
   run summaries.
4. Measure repair yield, residual-gap disclosure, latency, and cost against
   Phase 1. Keep the deterministic containment and eligibility gates even if
   model-assisted repair does not justify its cost.

As run (2026-10-27): steps 1 to 3 are built (nexus-jypmj.3.1 to .3.6); step 4
is nexus-jypmj.3.10. The coverage check shipped off and `NX_ANSWER_COVERAGE=1`
opted in: the Phase 3 review found that turning it on before step 4 measured
it contradicted Phase 1 step 7 and the Phase 2 precedent, so the default was to
flip only after nexus-jypmj.3.10 and Sam's approval. **Amended 2026-10-28:** Sam
decided on 2026-10-28 (nexus-jypmj.17) that the check is on by default, ahead
of the step 4 measurement and together with the concurrent assessment calls; it
shipped off in 7.88.0 and on in the release after it, and `NX_ANSWER_COVERAGE=0`
turns it off. The step 4 measurements of the shipped call shape (the Key
Discoveries and Performance Expectations list what is still outstanding) were
not taken before the flip. Departures from section 4, each
stated in the code and the published docs: an assessor reply with no usable
evidence record leaves its facets `unassessed`, not `unsupported`; a facet
with both supporting and contradicting evidence is covered and marked
`conflicted`; prose is kept as `insufficient_evidence` while any facet is
unassessed, so `uncovered` needs every facet known; the reassessment after the
repair reads the evidence the repair added and the anchors the first round left
unread (amended 2026-10-30, below); a partial ledger hands
generation a coverage note naming the unresolved facets, which is how the
"explicit list" of step 4 reaches it; ids that come back but hydrate to
nothing give `no_evidence` and a failure; the envelope's `coverage` object
carries the state after the post-generation decision. **Added 2026-10-30
(Sam, nexus-jypmj.23):** a facet that no item the check read supports or
contradicts is `unsupported` even when the per-call size cap left anchors
unread; it is not `unassessed`. A plan that hands the check more anchors than its
calls carry (N04: 301 anchors, 127 read, 174 omitted) can therefore be
withheld as `uncovered`; before, both facets read `unassessed` and off-topic
prose came back `answered`. Only a facet whose assessment failed, raised or was
skipped stays `unassessed`. The count of unread items is disclosed in both
notices ("N of M retrieved items were not read") and in the envelope
(`skipped_items`, `anchors_total`), and a repair reads the unread anchors. The
cost: a correct answer whose support stays unread is withheld, at a rate not
measured. The run summary
persists only on engine-service-v0.1.166 and later; the deployed engine
reports `unsupported`.

### Day 2 Operations

No new collection, database table, or credential is proposed. One additive
nullable run-summary JSONB column on the existing `nx_answer_runs` table is
managed through the normal Liquibase/jOOQ/DDL-ratchet lifecycle, backup, and
migration path. The golden-set version and answer-kind vocabulary are
source-controlled configuration; changes require regression results and a
migration note for plan metadata. Routing/coverage summaries use existing
run-record inspection, import/export, and structured logs. No additional
deletion operation is required beyond the existing run-record lifecycle.

### New Dependencies

None proposed. Use the existing catalog, structured operator dispatch, plan
library, and composite telemetry surfaces.

## Test Plan

- **Scenario**: The observed multi-facet RDR-084 synthesis — **Verify**:
  lookup-only plan 473 is ineligible; primary RDR and lifecycle evidence are
  represented; every facet is covered or disclosed.
- **Scenario**: “Which paper reported value X?” — **Verify**: an eligible
  factual-lookup plan still matches and bounded graph expansion remains useful.
- **Scenario**: Named RDR with `scope="rdr"` and an `rdr` content binding —
  **Verify**: the vector stream searches only `rdr__` collections even when
  the plan pins `corpus: all`; traversal does not admit code merely through
  `implements` or `relates`; per-stream corpus counts prove containment.
- **Scenario**: Caller scope `rdr` on a multi-step plan with one step pinned
  to `code` (or a `taxonomy_domain: code` step) — **Verify**: that step is
  skipped and contributes no evidence, the envelope discloses a scope conflict,
  and the other steps run narrowed to `rdr`.
- **Scenario**: Caller scope `rdr` on a `single_query` plan whose step pins
  `code` — **Verify**: the fast path discloses a scope conflict instead of
  querying `code`; on a plan pinned to `knowledge,code,docs,rdr` it queries
  `rdr` only.
- **Scenario**: Every retrieval step conflicts — **Verify**: the run returns
  the empty-retrieval notice, and the notice names the scope conflict.
- **Scenario**: No caller scope on any builtin — **Verify**: retrieval corpora
  are unchanged from today.
- **Scenario**: “What does current code implement differently from RDR-084?” —
  **Verify**: implementation/code expansion is explicitly allowed, while the
  RDR remains a reserved primary stream.
- **Scenario**: Two projects contain the same RDR number — **Verify**:
  resolution respects project/scope or reports ambiguity; no arbitrary source
  substitution.
- **Scenario**: Primary document exceeds the evidence budget — **Verify**:
  section-diverse selection survives, partial coverage is marked, and secondary
  chunks cannot consume the reservation.
- **Scenario**: Explicit RDR `SourceRef` in Phase 1 (gate-history facet from
  Phase 2) — **Verify**: the T2 lifecycle stream supplies that RDR's metadata
  and latest gate record within its record count and the character budget; a
  non-RDR source gets no T2 stream; a source in another tenant gets none.
- **Scenario**: Plan inputs exceed `_OPERATOR_MAX_INPUTS` with a reserved
  primary stream, once through `ids` hydration and once through explicit
  `inputs` (plan 473's shape) — **Verify**: in both, truncation removes only
  secondary items and every reserved primary and T2 item reaches the operator.
- **Scenario**: Engine traversal response with exactly 500 nodes —
  **Verify**: the run reports a possibly truncated traversal, not containment.
- **Scenario**: More than 300 out-of-scope traversal ids precede in-scope ids —
  **Verify**: nodes are filtered and ranked before manifest expansion, in-scope
  evidence survives, and per-id collection alignment is preserved.
- **Scenario**: One requested facet is absent from initial retrieval —
  **Verify**: exactly one targeted repair is attempted; the facet receives new
  evidence or remains an explicit residual gap.
- **Scenario**: Retrieval evidence contradicts a requested premise —
  **Verify**: coverage preserves contradiction for the answer; repair does not
  search until it finds agreement.
- **Scenario**: No plan has a known compatible shape — **Verify**: conservative
  fallback or inline planning, never forced selection of a known mismatch.
- **Scenario**: Unknown-kind grown plan with lookup-shaped history and a
  synthesis request — **Verify**: it cannot bypass eligibility; route to a
  generic synthesis plan or inline planner.
- **Scenario**: Multi-facet request with an ambiguous project qualifier —
  **Verify**: disclose ambiguity instead of choosing another project's RDR.
- **Scenario**: Six-facet request with several evidence streams — **Verify**:
  assessment is grouped within the call cap, not six unconditional model calls.
- **Scenario**: Execution succeeds but required evidence is missing —
  **Verify**: `answer_shape="insufficient_evidence"`, coverage is `partial`,
  the text opens with the `[insufficient-evidence: ...]` marker followed by
  the prose, the run summary's `coverage_partial` count is 1, and the run is not
  a quality exemplar. On a library plan `success_count` increments; on an
  inline-planned run the no-grow flag blocks growth. `nx answer-runs` puts the
  row in its own bucket.
- **Scenario**: A plan whose runs are all `partial` — **Verify**: it stays
  matchable; `_is_always_failing` does not skip it. A plan with three
  `unavailable` runs and no success is skipped.
- **Scenario**: Request with no facets — **Verify**: `coverage.state` is
  `unassessed` and the answer is returned as today.
- **Scenario**: Unknown request with unknown-grown lookup history — **Verify**:
  the grown plan is ineligible; curated generic planning or inline planning is
  used instead.
- **Scenario**: Engine rolls back after capability was cached — **Verify**:
  capability refresh/readback detects dropped summary and reports
  `persistence=dropped`, not success.
- **Scenario**: `trace=False` and private question text — **Verify**: new logs
  and telemetry contain structured ids/counts, not the redacted question.
- **Scenario**: The first coverage assessment call fails — **Verify**:
  deterministic checks still run, no facet is defaulted to covered, and the
  terminal state is `unassessed` with the answer returned, not a notice in its
  place.
- **Scenario**: The repair call fails after a successful first assessment —
  **Verify**: the first ledger stands and the state is `partial` with the
  unresolved facets listed.
- **Scenario**: Older engine run-record capability — **Verify**: no extra write,
  no retrieval failure, and current record fields remain intact.
- **Scenario**: Existing bundle/continuation path — **Verify**: the same evidence
  fidelity reaches either reduction route; the coverage gate does not fork
  semantics.
- **Scenario**: `check` immediately before `generate` — **Verify**: coverage is
  evaluated at the runner-owned boundary, not hidden in bundled intermediate
  output; prefix and terminal reduction each run at most once.
- **Scenario**: Unknown request kind with high-confidence lookup match —
  **Verify**: use a genuinely generic eligible route or inline planner; exclude
  incompatible plans from planner few-shot examples.
- **Scenario**: FTS and below-floor category routes — **Verify**: the same
  answer-kind eligibility applies, not only the ordinary cosine path.
- **Scenario**: Naive/legacy chunks without section headings — **Verify**:
  positional diversity, disclosed partial metadata, and no facet marked covered
  from heading presence alone.
- **Scenario**: Multiple named sources and duplicate chashes — **Verify**:
  aggregate source/chunk/character budgets hold; deduplication preserves each
  document's positions and attribution.
- **Scenario**: New client with old engine — **Verify**: summary is omitted
  after capability negotiation, run record remains valid, and live envelope
  reports `persistence=unsupported` rather than trusting HTTP 200.
- **Scenario**: New engine readback and single/batch import — **Verify**:
  versioned run summary survives both `/record` and `/complete`, queries, and
  migration without a second write.

## Validation

### Testing Strategy

Use deterministic unit fixtures for eligibility, source resolution, stream
budgets, traversal policy, and coverage transitions. Use real catalog and T2
substrates for integration tests, consistent with repository rules. Seed all
ranking fixtures and use a fixed clock for run records. Tests that require a
model-backed coverage assessment must have a non-vacuity assertion and a
recorded deterministic fixture path; a skipped model dependency cannot count as
quality validation.

The evaluation has three layers:

1. **Contract tests**: wrong shapes cannot pass eligibility; explicit scope
   cannot be widened silently; primary evidence cannot be displaced.
2. **Golden-set comparison**: report the baseline and new eligible-plan recall,
   wrong-shape admission, source/facet coverage, and scope leakage. Ship only
   if wrong-shape admission and scope leakage fall without a material loss of
   eligible-plan recall on lookup fixtures. For Phase 2 this was measured
   in-sample only; see Phase 2 step 6, "As run".
3. **Live canary**: rerun a small, labeled set including ART RDR-084 against the
   actual service, compare structured evidence and final answers, and record
   model/latency/cost. This complements rather than replaces deterministic
   tests.

Numeric quality targets must be set from the measured baseline during research.
The design does not claim an arbitrary “80–90%” improvement before that
measurement exists.

### Performance Expectations

Containment should reduce hydrated context and secondary evidence on crowded
runs. Request extraction is deterministic-first; the single structured call
(on by default, Haiku) has separately measured cost and timeout: about
0.002 USD, p50 4 s and p95 7 s, 0 of 112 calls failed in measurement. Facet assessment is grouped per
evidence stream, with the repair and reassessment bounded to one cycle. Measure
invocation rates, facets per call, incremental latency, and cost against the
current run records. No throughput target is asserted without a baseline.

Coverage check on (default from 2026-10-28), what is measured and what is not.
Measured, one request on one run (ART RDR-084 G06, engine 0.1.167, 2026-10-28,
the serial design, one `operator_check` call per stream): check off 29.4 s and
0.433 USD; check on 147.3 s and 0.430 USD, of which the assessment took 118 s
(four serial calls, one cut at the 60 s limit) and 0.0166 USD. The concurrent
design with 20-anchor packs has no live figure. Its timing is a unit-test model
of the canary at 1/100 scale (a call takes 0.15 s plus 7 ms per anchor), under
which the stage takes 50 model-seconds against 130 for the serial one: a model of
assumed per-call latencies, not a measurement, and not a prediction to cite.
Outstanding, in order: the canary on and off on the shipped tree (wall clock, not
summed step time; calls, peak in flight, cut calls, backend and
`NX_LLM_MAX_CONCURRENCY`); reply classes per pack against the one-call-per-stream
baseline (0 unusable of 40 calls); G18 (withheld after assessment while the off
arm answered), G01, G02 and G04 on the fixed tree; the full golden set on and off
for latency and cost; a small `budget_usd` and `budget_seconds` for first-wave
overshoot and cut calls; a persisted run through `nx answer-runs --steps`; and, since 2026-10-30, the rate at which a
correct answer whose support stays unread is withheld (N04 was measured; N01-N03 were replaced and
not run). The
`claude -p` backend runs 4 concurrent subprocesses with rate-limit and host-load
behaviour unmeasured.

## Finalization Gate

### Contradiction Check

Gate round 1 failed this criterion on three findings (C2, S1, S2), and C1
refuted an assumption the diagnosis rested on. All four are resolved. Gap 2
now names both causes of dilution and section 2a fixes the plan-pinned one.
Partial coverage was specified three ways; section 4's coverage-state table is
now the single definition, and the counters follow it, with the matcher as
their live consumer. RDR-189 is shown as accepted. Whether facet
assessment can reliably reuse existing operators remains an open assumption
(A3), not a verified claim.

### Assumption Verification

The prototypes (research 6–10) establish feasibility within their fixtures.
Production integration and live-model quality cannot be shown before the code
exists, so acceptance does not wait on them. Each open assumption is instead an
exit criterion of the phase that builds it:

| Phase | Assumptions it must close before the phase is done |
| --- | --- |
| Phase 1 | A2 (production section selection), A6 (production `traverse` policy and no-policy compatibility), A7 (T2 lifecycle stream) |
| Phase 2 | A1 (production eligibility on every matcher branch), A5 (request extraction and the active-project tier) |
| Phase 3 | A3 (live facet assessment at the runner boundary), A4 (production migration and the client capability-off leg) |

Shared-chash section titles are not authoritative; labels must be
content-derived or explicitly document-scoped. The telemetry design requires
an additive migration and capability negotiation, not just client wire fields.
The RDR-189 boundary is settled by RDR-189's Shared Contract section.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `plan_match(...)` candidate filtering | Nexus `plans.matcher` | Source Search + external eligibility spike passed (research 6); production integration unverified |
| `traverse(...)` policy before manifest expansion | Nexus MCP/catalog | Ordering fixture passed (research 10); production integration unverified |
| `graphBFS(...)` node cap and `graph_node_limit` log | Nexus engine catalog | Source Search verified the 500-node pre-client cap and its log event (research 16); a scope predicate, release, capability marker, and old-engine fallback are needed only if Phase 1 step 6 is taken |
| Catalog resolve/manifest + content-derived section selection | Nexus catalog/T3 | Real bounded resolution/manifest/selection spike passed (research 7); production resolver/runner and paging unverified |
| Request extraction and owner-qualified source resolution | `mcp/core.py`, catalog owner scope | Prototype + real owner resolution passed (research 10); general extraction and ambiguity handling unverified |
| `store_get_many(..., limit_per_source=...)` | Nexus MCP/runner | Source Search completed; not a primary-source reservation |
| Structured coverage at runner boundary | Nexus operators/runner | Deterministic spike passed (research 8); production integration and live-model reliability unverified |
| Composite `nx_answer` run record | Nexus telemetry/engine | Private repository/DDL spike passed (research 9); production migration, wiring, and client degradation unverified |

### Scope Verification

The Minimum Viable Validation is phased as specified above. Phase 3 uses the
original ART RDR-084 request to test selection, evidence, and final coverage
together; earlier phases have narrower proofs and make no full-coverage claim.
The general RDR-189 target-definition registry, new graph algorithms, a new
retrieval language, and an LLM-only plan judge are out of scope.

### Cross-Cutting Concerns

- **Versioning**: Version answer-kind declarations and the coverage envelope;
  retain old-plan and old-engine compatibility paths.
- **Build tool compatibility**: Existing Python and Java/Maven build paths;
  jOOQ regeneration and the DDL ratchet are required for the telemetry column.
- **Licensing**: No new dependency or copied external implementation.
- **Deployment model**: Client runner/MCP changes plus an additive engine
  telemetry migration (Phase 3, an engine release), and possibly a Java
  graph-scope filter with dedicated capability negotiation. The graph filter ships
  only if Phase 1's count of capped `nx_answer` traversals shows the cap is
  reached; the rest of Phase 1 is client-only.
- **IDE compatibility**: N/A.
- **Incremental adoption**: Land deterministic evidence containment first,
  then eligibility, then optional bounded repair; each phase has its own
  baseline comparison.
- **Secret/credential lifecycle**: Reuse existing operator dispatch and service
  credentials. No new secret, storage, or override.
- **Memory management**: Bound source streams by prompt budget and count;
  deduplicate by chunk identity; retain manifests and ledgers, not duplicate
  document copies, in telemetry.

### Proportionality

The design addresses one observed pipeline failure at its three independently
testable boundaries. It deliberately excludes the general RDR-189 answer-object
architecture, a new planner, a new telemetry table, or a second telemetry write.
Phase 3 can be
dropped if its measured repair yield does not justify its cost; Phases 1 and 2
remain coherent without it.

## References

- Observed ART RDR-084 `nx_answer` structured run, 2026-10-23, plan 473;
  durable telemetry `nx_answer_runs.id=722`. Saved session result
  `mcp__plugin_conexus_nexus__nx_answer_14_563e4823.json` supplied the chunk
  references; per-stream/reducer composition is unavailable.
- Verification scripts, logs, and result JSON: DEVONthink Inbox
  `/Research/nexus-rdr-232-verification/rdr232-verification-artifacts.tar.gz`,
  record `A7004B0A-B6D7-4F4F-840F-4C98FF8B461B`. Evidence index:
  `2922E07B-005D-43D4-8091-4FEBE7EE3D1D`. Numbered findings:
  `nexus_rdr/232-research-1` through `-16`.
- ART `docs/rdr/RDR-084-list-parse-canonical-consolidation.md` and T2 record
  `ART_rdr/084`, used as the independent known-good source check.
- `src/nexus/plans/matcher.py`, `schema.py`, `runner.py`, `bundle.py`, and
  `answer_shape.py` on base commit `9a345da8b`.
- `src/nexus/mcp/core.py`: traversal, `nx_answer`, candidate cost choice,
  hydration, and run recording.
- `conexus/plans/builtin/hybrid-factual-lookup.yml` (plan 473's template).
- RDR-091: scope conflict filtering and ranking.
- RDR-097: hybrid factual lookup and the context-precision trade-off.
- RDR-189: target-definition layer and answer-object contracts (accepted
  2026-10-24).
- RDR-196: cost ranking after correctness gates.
- RDR-200: primary-source reach, crowding, and continuation evidence fidelity.
- RDR-203: composite `nx_answer` run record.
- Lan, Wang, Bao, Li, Ji (2026). *AgenticScholar: Agentic Data Management with
  Pipeline Orchestration for Scholarly Corpora*. arXiv:2603.13774,
  DOI 10.48550/arxiv.2603.13774. Indexed tumbler `1.12.116`,
  `knowledge__agentic-scholar-papers__voyage-context-3__v1`. §4.1 (Scope + Task
  decomposition), §4.2 (two-stage predefined plan selection), §6.5.2 (hybrid
  planning efficiency and bounded self-correction). T2 research:
  `nexus_rdr/232-research-5`.

## Revision History

- 2026-10-23: Draft created from the observed ART RDR-084 synthesis failure.
- 2026-10-23: Research rounds 1–4 recorded. Corrected answer-kind storage and
  unknown-request routing; distinguished manifest order from T3 section
  metadata; added bounded source resolution and aggregate budgets; required a
  runner-owned coverage boundary; specified additive run-summary JSONB,
  capability negotiation, and read/import compatibility. A1–A4 behavioral
  verification remains open.
- 2026-10-23: Research 5 recorded. Original AgenticScholar paper confirmed
  indexed (tumbler `1.12.116`, RDR-179 absence note stale) and read directly:
  §4.2 two-stage selection cited as precedent for eligibility-before-ranking
  and a later optional LLM judge; §6.5.2 plan-reuse efficiency and bounded
  self-correction noted. Alternative 2 updated accordingly.
- 2026-10-24: Research 6–9 recorded bounded A1–A4 prototype verification.
  A2 falsified shared-chash `section_title` as document-scoped metadata and
  switched selection to content-derived labels. A3 proved the runner-boundary
  orchestration with deterministic dispatch. A4 passed the isolated repository
  and rollback/import checks; production migration and the denied client
  degradation leg remain unverified. All assumptions are partial, not claims of
  production integration or live-model quality.
- 2026-10-24: `nexus_35f` critique found four blockers (request extraction,
  pre-cap traversal truncation, phase/MVV mismatch, A1 overstatement) and seven
  follow-ups. Research 10–11 and the corresponding revisions add A5/A6,
  deterministic-first request extraction and project resolution, traversal
  filtering before manifest expansion, per-phase MVV proofs, an explicit
  compatibility matrix, unknown-grown restrictions, coverage outcome semantics,
  telemetry readback, RDR-189 ownership boundaries, and durable evidence.
  `nexus-tko2w` tracks the wider shared-chash metadata bug. Re-review pending;
  this is not gate approval.
- 2026-10-24: Confirmation review corrected the recorded-request project claim:
  the MVV now uses the verbatim question plus explicit owner/project binding.
  Added facet/constraint separation, engine graphBFS pre-cap filtering,
  explicit `INSUFFICIENT_EVIDENCE` no-grow behavior, and run-summary coverage
  outcomes without a plans-table migration. RDR-189 boundary is now a shared
  contract: 232 owns analysis/routing/containment, 189 owns durable target
  families, witnesses, and preservation semantics. Research 12 records these
  corrections.
- 2026-10-24: Final peer confirmation reported no blockers. Remaining
  implementation follow-ups are now explicit: `INSUFFICIENT_EVIDENCE` increments
  `failure_count`, suppresses growth, and returns a notice rather than partial
  prose; `promote.py` must consume coverage evidence. The pre-cap graph filter
  requires a Java engine release, capability negotiation, and a separate
  500-node proof. RDR-189 now carries the matching shared-contract section.
- 2026-10-25: Gate round 1 — BLOCKED (2 Critical, 8 Significant, 2 ship-blocker(s)); commit `6b2f94e66`; critique `nexus_rdr/232-gate-critique-2026-10-25-r1`.
- 2026-10-25: Gate round 2 — PASSED (0 Critical, 8 Significant, 0 ship-blocker(s)); commit `44a11f3fa`; critique `nexus_rdr/232-gate-critique-2026-10-25-r2`.
- 2026-10-26: Phase 1 closed. Key Discoveries record A2, A6 and A7 as
  verified in production (research 19-21). Section 2a adds the owner-narrowing
  rule for secondary streams (nexus-jypmj.1.18), approved by Sam.
- 2026-10-26: Section 2a replaces the subtree forwarding sentence with the shipped subtree-filter rule (nexus-jypmj.1.16), approved by Sam.
- 2026-10-26: Section 1 states how the active-project tier decides (reliability, the project-name and title veto, the fallback notice, question-text sources, the range disclosure) and the Phase 2 proof states what "otherwise" means for the unmodified call (nexus-jypmj.2.4), approved by Sam.
- 2026-10-27: Phase 2 measured and enabled (nexus-jypmj.2.8, nexus-jypmj.12), approved by Sam. Section 1: a lookup includes several connected facts from one passage of one source; the directional rule is recorded as a measured limit; request extraction is on by default with Haiku and turning it off puts the gate in shadow. Trade-offs and Performance Expectations carry the measured fall-through and extraction cost. Phase 2 step 6 records that the thresholds were set after measuring, and why.
- 2026-10-27: Section 4's coverage-state table row for `unavailable` names the new `uncovered` shape for evidence that covered no facet and keeps `no_evidence` for zero retrieval results, per Sam's ruling (nexus-jypmj.3.12), added with the shape in nexus-jypmj.3.3.
- 2026-10-27: Section 4 counts a contradicted facet toward a usable answer: the definition and the `unavailable` row say covered or contradicted, so a contradiction-only result is `partial` with the contradiction named ahead of the prose, per Sam's ruling (nexus-jypmj.3.13).
- 2026-10-27: Phase 3 As run recorded (the coverage check ships off until nexus-jypmj.3.10 and Sam's approval; the section 4 departures as built), and section 4 and the Test Plan corrected where they misdescribed the build (marker bucketing under trace=False, assessment USD in step records, the continuation fallback repay, the unverified and not_recorded persistence states, the coverage_partial count), approved by Sam.
- 2026-10-28: Key Discoveries record the lookup-boundary re-measure (nexus-jypmj.13), the pre-cap filter's old-engine departure (nexus-jypmj.1.13) and the live canary latency (nexus-jypmj.3.10); two A5 statements are corrected as findings. No decision text changed.
- 2026-10-28: The coverage check is on by default (Sam's decision, nexus-jypmj.17), with concurrent assessment calls. Decision text changed: Phase 3 As run (the check no longer ships off; the amendment names Sam and the date), section 4 (a dated departure: packs of at most 20 anchors, at most 4 concurrent calls, the call count bounded by the stage share and the USD stop-line, not by design) and Performance Expectations (the measured canary figures, the unit-test timing model labelled as a model, the outstanding measurements). The Key Discovery for the timing model is tagged Assumed.
- 2026-10-30: Two decisions by Sam, recorded. (1) The `lookup` row admits lookup plans only (nexus-jypmj.11). (2) A facet no read item bears on is `unsupported` when the per-call size cap left anchors unread, with the unread count disclosed (nexus-jypmj.23). Decision text changed: section 1 (the compatibility paragraph and the measured-limit paragraph now state the lookup-only rule and point at the dated note), section 4 (the `uncovered` definition reads "among the items read"; a dated departure in Phase 3 As run for the unread-anchor rule, N04's 301 anchors, 127 read, 174 omitted; the post-repair reassessment also reads the unread anchors), Key Discoveries (the lookup entry corrected for first-pass kinds and the rows it covers, a separate entry for the omitted-anchor mechanism, the two open items of 2026-10-27 closed, the Phase 3 live figures qualified as taken under the old rules), and Critical Assumptions A1.
