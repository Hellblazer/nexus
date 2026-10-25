---
title: "nx_answer Answer-Shape Eligibility and Evidence Sufficiency"
id: RDR-232
type: Architecture
status: draft
priority: high
author: Sam
reviewed-by: self
created: 2026-10-23
accepted_date:
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
chunks survived the 100-input cap. The accumulated references therefore show a
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

#### Gap 2: Traversal can dilute the caller's primary evidence

RDR-097 deliberately expands factual lookup through citation and relation
links. The traversal primitive has no caller-scope argument. It follows catalog
links and returns up to a global cap of chunk ids from the reachable documents.
Hydration caps input count, not the share belonging to the caller's requested
corpus or named source. Consequently, linked code can displace the RDR evidence
that the caller explicitly scoped.

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
RDR-189 is the closest conceptual overlap and remains a draft; the boundary
above is deliberate.

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
- **Execution**: `src/nexus/plans/runner.py` applies caller scope to corpus
  arguments, resolves steps, hydrates operator inputs, and bundles adjacent
  operators.
- **Traversal**: `src/nexus/mcp/core.py::traverse` accepts seeds, link types or
  purpose, depth, and direction. It has no corpus or primary-source reservation
  parameter. It returns document tumblers, chunk ids, and collections.
- **Hydration**: runner auto-hydration preserves source notes and caps operator
  inputs at 100 after dropping empty content. The cap is positional; it has no
  per-corpus or per-document reservation.
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
- **Verified — traversal scope stops at corpus arguments.** Caller scope is
  applied through `_apply_caller_scope_to_args`. `traverse` does not accept a
  corpus argument, and its catalog walk collects every reachable collection.
  *Source: `plans/runner.py` and `mcp/core.py::traverse`; research 4.*
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

### Critical Assumptions

Research 6–9 executed prototypes for all four assumptions. They establish
feasibility within the stated fixtures, not production integration or live
answer quality. The detailed A1/A2 census is
`nexus_rdr/232-research-a1-a2-source-verification`; A3/A4 current interfaces
are in `232-research-3`. Behavioral evidence is in `232-research-6` through
`232-research-9`.

- [ ] **A1 — explicit answer-kind metadata and conservative request routing can
  improve eligibility without material recall loss.** **Status**: Partially
  verified (research 6). **Method**: Source Search plus executed prototype
  fixture using the real matcher. The gate passed 14/14 eligible and 0/3
  wrong-shape cases, with correct unknown routing and few-shot exclusion.
  Production matcher/core integration and natural-language request-kind
  inference remain unverified; the small labeled fixture is not a live recall
  estimate. Store kind in `plan_json`, never dimensions; decode legacy absence
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
- [ ] **A3 — existing dispatch can support facet coverage at a new runner-owned
  pre-reduction boundary.** **Status**: Partially verified (research 8).
  **Method**: Source Search plus deterministic boundary spike using real
  `plan_run` and real bundle composition/dispatch with fake model output.
  Plain `check → generate` hid its intermediate; the proposed boundary preserved
  anchors, executed one prefix/repair/reduction, shared budget/deadline, and
  disclosed failure/scope/continuation states correctly. Production runner
  integration and live-model facet reliability remain unverified.
- [ ] **A4 — a nullable versioned run summary can extend existing telemetry
  compatibly.** **Status**: Partially verified (research 9). **Method**: Source
  Search plus private-engine and isolated `TenantScope`/jOOQ repository spike.
  Old-engine silent drop, one-write current completion, nullable JSONB summary
  persistence, rollback, plan counters, imports/readback, and old-client null
  behavior passed. Production Liquibase/jOOQ/DDL-ratchet work, full handler and
  import wiring, old/new matrix, and the client capability-off sub-leg remain
  unverified; that sub-leg's private-test write-guard opt-in was denied by the
  permission classifier and was not executed.

- [ ] **A5 — deterministic-first request extraction can produce kind, facets,
  and project-qualified sources without silent narrowing.** **Status**:
  Partially verified (research 10, corrected by review). **Method**: Source
  Search plus bounded extraction spike and real owner/catalog reads. The
  question's opening synthesis cue classified correctly and the splitter
  returned seven clauses, including topic and constraint clauses. It did not
  prove facet/constraint separation. The harness supplied literal `ART` and
  its absolute path, which resolved owner 1.2 and ART RDR-084 tumbler
  1.2.4686; the recorded question contains no `ART` token, so qualifier
  extraction was not proven. The MVV therefore requires an explicit owner or
  project binding. Prototype heuristics needed fixture corrections, and
  general inference, ambiguity fallback, and optional structured extraction
  cost remain unverified.
- [ ] **A6 — traversal can apply scope before its chunk cap without losing
  alignment.** **Status**: Partially verified (research 10). **Method**: Source
  Search plus synthetic graph fixture with >300 out-of-scope ids preceding
  in-scope evidence. Legacy truncation lost the in-scope doc; filtering nodes
  before manifest expansion retained it with per-id collection alignment.
  Production `traverse` integration and no-policy backward compatibility remain
  unverified. The engine also caps graphBFS at 500 nodes before client
  filtering, so production must apply the scope predicate before that cap as
  well. A6 does not exercise the engine path.

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
| `lookup` | A specific fact or small set of facts | Best direct evidence; optional graph context |
| `synthesis` | A unified account of multiple requested aspects | Coverage of each requested facet |
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

Unknown-kind **builtin/curated** plans remain eligible as in the compatibility
matrix because their scope and operator shape are reviewed. Unknown-kind
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

Compatibility is directional. A synthesis-capable plan may answer a lookup when
its operators are suitable; a lookup-only plan may not claim a multi-facet
synthesis. Unknown **plan** declarations are not positive evidence of
incompatibility and remain eligible under conservative legacy/grown gates.
Unknown **request** kind must not make a known narrow plan the default for a
broad or ambiguous request: retain only genuinely generic compatible routes or
enter inline planning with eligibility diagnostics. There is no existing
no-model generic synthesis fallback to reuse; adding one would be separate
work.

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
deterministic first pass and an optional single structured-extraction call only
when the first pass cannot establish the required shape.

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

T2 is outside the corpus graph. An RDR question whose facets include gate or
lifecycle state may use the existing RDR metadata/memory lookup as a distinct,
bounded evidence stream when the caller can access it. It must not be replaced
by code links or treated as an ordinary T3 document.

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

The policy is applied **inside the catalog walk before its node cap and inside
`traverse` before manifest expansion**, not by filtering already-truncated
results. `traverse` currently returns a sorted union of collections and caps
ids in graph order; the engine's `graphBFS` also truncates at 500 nodes by
`(min_depth, tumbler)` before the client sees them. A runner-only or post-BFS
filter would lose in-scope evidence before the policy saw it.

Direct MCP `traverse` keeps its current behavior when the optional policy is
absent. With a policy it:

1. Applies the allowed-collection predicate to catalog nodes **during the graph
   walk**, before `MAX_GRAPH_NODES` truncation. This is a scoped filter on the
   existing engine traversal, not a new graph algorithm. It requires a Java
   engine signature/query change and a tenant-scoped join to the collection
   metadata, followed by an engine release. `nx_answer` negotiates a dedicated
   `catalog_graph_scope_filter` capability and requires a scoped-result marker
   or readback count proving the predicate ran. An older engine that ignores
   the parameter must not be treated as contained: disclose incomplete
   traversal and omit secondary graph evidence rather than claiming success.
2. Ranks and caps **documents**, not arbitrary manifest chunk order. Use link
   role and link distance as the available signals; `traverse` has no search
   relevance input, so none is invented. Use tumbler only as a deterministic
   final tie-break.
3. Expands manifests only for the retained documents, then applies the
   secondary chunk budget with per-document reservations.
4. Returns per-id collection alignment or document-grouped ids so hydration
   cannot reconstruct scope from a union of collection names. Existing
   no-policy results retain the legacy shape.

`nx_answer` always supplies the policy derived from request and plan. A6's
fixture proves the client-side ordering against the 300-id cap; the engine's
500-node pre-cap filtering remains an implementation verification item.

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
| Explicit scope | Observed collection counts | satisfied / widened / violated |

`covered` means evidence exists for the facet, not that the answer's claim is
true. `contradicted` is retained for the reduction; it is not repaired into
agreement. Coverage evaluation uses existing structured operator dispatch with
deterministic checks for source presence and scope. It must not issue another
`nx_answer` call.

The terminal result uses an explicit coverage state vocabulary: `complete`
when every required facet has evidence, `partial` when at least one remains
unsupported/contradicted or assessment is incomplete, and `unavailable` when
no usable answer can be produced. This state is independent of prose fluency.
For backward compatibility, terminal `answer_shape="answered"` is allowed only
when `coverage.state="complete"`; otherwise the structured result uses
`answer_shape="insufficient_evidence"` and human-readable output gets the
standard insufficiency notice. A generation failure keeps the existing error
shape. `coverage` remains the detailed facet ledger, not a redundant side
channel that consumers can ignore.

Plan execution counters remain execution counters in the existing structural
sense: they record whether the result is an answer according to `AnswerShape`,
not whether it covered the question. Add `INSUFFICIENT_EVIDENCE` to
`AnswerShape` and `NON_ANSWER_SHAPES`, with a stable notice and corresponding
`nx answer-runs` classification. It is excluded from plan growth, so a partial
answer cannot compound as a good plan. Under the current non-answer semantics,
it also increments `failure_count`, prevents plan growth, and replaces
`final_text` with the standard notice plus chunk references. The structured
`coverage` object retains the detailed evidence assessment; the partial prose
is not returned by the normal text path. Phase 3 must either preserve that
established behavior or explicitly design a prose-preserving envelope without
changing the no-grow rule. Existing `success_count` and `failure_count` keep
their structural semantics; no third plan counter or plans-table migration is
introduced here.

Coverage outcomes live in the **run summary**, not new columns on `plans`:
`coverage_complete`, `coverage_partial`, and `coverage_unavailable` are counts
aggregated from the run's terminal coverage assessment. Because partial runs
increment `failure_count`, they reduce the current promotion ratio. Phase 3
includes an explicit `promote.py` policy change to consume coverage evidence
rather than pretending the legacy ratio measures answer quality. Historical
counters are not rewritten. This also supplies observed answer-shape evidence
for RDR-189's family research.

Assessment cost is bounded independently of facet count. Group facets into one
structured assessment per evidence stream, with a calibrated cap on facets per
call and one repair/reassessment cycle. The motivating six-facet request must
not imply six unconditional model calls. Record assessment calls, facets per
call, and incremental cost/latency in the run summary.

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
real facet-ledger output schema if used for this purpose. RDR-189 P1, if
accepted, owns normalization of `check`/`verify` to `(tumbler, chash)` witness
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
steps remain a disclosed accounting limitation. Record coverage/repair usage in
the existing step accounting vocabulary rather than inventing an unversioned
step source. If repair is unaffordable, return the available structured
coverage ledger and the standard insufficiency notice with a
non-`answered` coverage state: `answer_shape="insufficient_evidence"` with
`coverage.state="partial"` when some usable answer exists, or
`coverage.state="unavailable"` when none exists. Both use the same non-answer
shape; the coverage state distinguishes them. The text path returns the
standard insufficiency notice, not the partial prose; the detailed coverage
ledger remains structured. Do not claim `answered` solely because generation
produced prose.

The structured envelope gains a small `coverage` object: requested and covered
facet ids, primary-source state, observed corpus counts, repair attempts, and
residual gaps. Full evidence text is not duplicated into telemetry; when
`trace=False`, facet text and source hints are redacted as well. `answer_shape`
and the detailed `coverage.state` follow the compatibility rule above: an
insufficient result is never labeled `answered`. Human-readable output adds
the standard insufficiency notice for unresolved required facets. Continuation
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
wire field are nullable. Returned coverage and persisted coverage are distinct
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
| Traversal containment | `mcp/core.py::traverse`, catalog nodes/manifests | Apply policy inside traverse before manifest expansion, with per-id alignment; preserve legacy no-policy behavior. Runner-only post-filtering is insufficient. |
| Evidence budgets | `store_get_many`, runner tumbler hydration, bundle prompt construction | Extend with source-aware reservations and content-derived document-scoped section labels; manifest order + positional fallback; shared-chash T3 titles are not authoritative. |
| Facet coverage | `check`, `verify`, `extract`, source annotations, runner segmentation | Reuse direct operator dispatch; add runner-owned pre-reduction state/hook and typed anchors. Do not rely on bundled intermediate output or recursive `nx_answer`. |
| Coverage telemetry | RDR-203 composite run record, engine telemetry, structured envelope | Add nullable versioned run-summary JSONB via Liquibase; thread record/complete/read/import; capability-negotiated. No new table or extra write. |
| Generic fallback | Inline planner | Extend with `RequestCoverage`; no existing no-model generic synthesis fallback was found. |
| Terminal answer classifier | `plans/answer_shape.py`, `mcp/core.py`, `commands/answer_runs.py` | Extend: add `INSUFFICIENT_EVIDENCE` to `AnswerShape` and `NON_ANSWER_SHAPES`, a stable notice, read-side classification, and no-grow behavior. Keep structural counters and run coverage outcomes separate. |
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
- Source reservation gives less space to secondary evidence. It should improve
  source-grounded answers but may reduce broad contextual discovery unless the
  caller asks for it.
- Coverage extraction and repair add model and retrieval work on some calls.
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
- **Coverage check unavailable**: use deterministic source/scope checks and
  disclose that facet coverage was not assessed; do not mark facets covered by
  default.
- **Repair exhausts budget**: generate once from available evidence when useful
  and disclose unresolved facets, or return a structured partial/no-evidence
  result.
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
- [ ] Implement and verify the production additive run-summary migration and
  all old/new client-engine combinations, including readback and import. The
  isolated repository spike passed; the denied client write-guard sub-leg was
  not executed.
- [ ] Review RDR-189 against this shared seam before either design is accepted:
  RDR-232 owns request analysis/eligibility/containment; RDR-189 owns durable
  target families, host-bound witness normalization (P1), and post-realization
  preservation-trace semantics. RDR-232's routing capability derives from the
  target family once P2 exists; its live pre-reduction coverage ledger is
  input to P4's final-answer preservation assessment, not its substitute. Amend RDR-189's answer-shape-dimension suggestion to
  non-identity metadata and its P3 to deterministic-first extraction where
  possible. Phase 3 witness-anchor work consumes P1's contract if accepted;
  otherwise it uses the explicitly versioned local subset without claiming
  RDR-189 compatibility.

### Minimum Viable Validation

The end-to-end MVV belongs to Phase 3, when all required mechanisms exist.
Phases 1 and 2 have separate proofs and do not claim full facet coverage.

**Phase 1 proof:** using explicit `SourceRef` and `ScopeConstraint` inputs (the
full `RequestCoverage` extractor is Phase 2), resolve the correct ART RDR-084,
preserve section-diverse primary evidence, and keep a >300-out-of-scope-id
traversal from discarding in-scope evidence. Record per-stream and reducer-input
counts. No facet-coverage guarantee is claimed.

The traversal proof has two required halves:

- **Client:** >300 out-of-scope manifest ids precede in-scope evidence; policy
  runs before manifest expansion and preserves per-id collection alignment.
- **Engine:** >500 out-of-scope graph nodes precede in-scope evidence; the
  negotiated predicate runs before `MAX_GRAPH_NODES` truncation and returns a
  scoped-result marker. An old engine without the capability yields disclosed
  incomplete traversal, never a containment claim.

The engine half depends on an engine release. The peer-reported A2 engine-tag
freeze is scheduled to lift no earlier than 2026-10-26 13:46Z; verify its
current status before scheduling that release. The client half does not prove
the engine half.

**Phase 2 proof:** the same request's answer kind and facets/constraints are
extracted, and an explicit `project="ART"` or `owner="1.2"` binding resolves
without guessing. The recorded question has no `ART` token, so this binding is
required rather than implied. Lookup-only plan 473 is ineligible, and
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

1. Accept explicit `SourceRef` and `ScopeConstraint` inputs for containment;
   add named-source resolution and a source-aware evidence-stream model using
   catalog manifests. Full `RequestCoverage` extraction lands in Phase 2.
2. Add traversal evidence policy inside the catalog walk before its 500-node
   cap and inside `traverse` before manifest expansion. This includes the Java
   engine signature/query change, engine-release sequencing, dedicated
   capability negotiation, and scoped-result proof. Preserve legacy no-policy
   results and aligned scoped results for `nx_answer`.
3. Reserve primary-source and secondary-stream budgets before hydration.
4. Add deterministic source/scope checks and per-stream/reducer-input
   diagnostics, plus the observed RDR-084 regression fixture.
5. Ship behind the existing `nx_answer` path, not a new user verb. Compare the
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

### Phase 3: Bounded repair and quality reporting

1. Add facet-evidence assessment at a runner-owned pre-reduction boundary using
   existing direct operator dispatch and preserved typed anchors. Do not use a
   bundled intermediate as the host-side ledger.
2. Add one targeted retrieval repair and one final reduction within the shared
   deadline and cost accounting; do not execute the plan prefix twice.
3. Add nullable versioned run-summary JSONB via Liquibase; update jOOQ and the
   DDL ratchet, thread `/record`, `/complete`, readback and import, and add
   capability negotiation. Extend the structured envelope with returned versus
   persisted coverage states. Add `INSUFFICIENT_EVIDENCE` and its no-grow/read
   behavior. No plans-table counter migration is needed: coverage outcomes are
   derived from run summaries. Update `promote.py` to consume that evidence
   explicitly; partial runs increment `failure_count` under the established
   non-answer semantics and must not be explained away as execution successes.
4. Measure repair yield, residual-gap disclosure, latency, and cost against
   Phase 1. Keep the deterministic containment and eligibility gates even if
   model-assisted repair does not justify its cost.

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
  **Verify**: traversal does not admit code merely through `implements` or
  `relates`; corpus counts prove containment.
- **Scenario**: “What does current code implement differently from RDR-084?” —
  **Verify**: implementation/code expansion is explicitly allowed, while the
  RDR remains a reserved primary stream.
- **Scenario**: Two projects contain the same RDR number — **Verify**:
  resolution respects project/scope or reports ambiguity; no arbitrary source
  substitution.
- **Scenario**: Primary document exceeds the evidence budget — **Verify**:
  section-diverse selection survives, partial coverage is marked, and secondary
  chunks cannot consume the reservation.
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
  the plan is excluded from growth, structural counters remain separate from
  run-summary coverage outcomes, and the run is not a quality exemplar.
- **Scenario**: Unknown request with unknown-grown lookup history — **Verify**:
  the grown plan is ineligible; curated generic planning or inline planning is
  used instead.
- **Scenario**: Engine rolls back after capability was cached — **Verify**:
  capability refresh/readback detects dropped summary and reports
  `persistence=dropped`, not success.
- **Scenario**: `trace=False` and private question text — **Verify**: new logs
  and telemetry contain structured ids/counts, not the redacted question.
- **Scenario**: Coverage or repair model call fails — **Verify**: deterministic
  checks still run, no facet is defaulted to covered, and the terminal state
  discloses the limitation.
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
   eligible-plan recall on lookup fixtures.
3. **Live canary**: rerun a small, labeled set including ART RDR-084 against the
   actual service, compare structured evidence and final answers, and record
   model/latency/cost. This complements rather than replaces deterministic
   tests.

Numeric quality targets must be set from the measured baseline during research.
The design does not claim an arbitrary “80–90%” improvement before that
measurement exists.

### Performance Expectations

Containment should reduce hydrated context and secondary evidence on crowded
runs. Request extraction is deterministic-first; the optional single structured
call has separately measured cost and timeout. Facet assessment is grouped per
evidence stream, with the repair and reassessment bounded to one cycle. Measure
invocation rates, facets per call, incremental latency, and cost against the
current run records. No throughput target is asserted without a baseline.

## Finalization Gate

### Contradiction Check

Draft: no known contradiction between the observed evidence and the proposed
containment/eligibility design. Whether facet assessment can reliably reuse
existing operators remains an open research assumption, not a verified claim.

### Assumption Verification

A1–A6 have source-search and partial prototype verification (research 6–10).
Production integration and live-model quality remain open and must be resolved
before acceptance. Shared-chash section titles are not authoritative; labels
must be content-derived or explicitly document-scoped. The telemetry design
requires an additive migration and capability negotiation, not just client
wire fields. A5's optional structured extraction cost and A6's production
integration are still open; the RDR-189 boundary must be resolved rather than
presumed compatible.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `plan_match(...)` candidate filtering | Nexus `plans.matcher` | Source Search + external eligibility spike passed (research 6); production integration unverified |
| `traverse(...)` policy before manifest expansion | Nexus MCP/catalog | Ordering fixture passed (research 10); production integration unverified |
| `graphBFS(...)` scope predicate before node cap | Nexus engine catalog | Source Search verified 500-node pre-client cap; Java filter, release, capability marker, and old-engine fallback unverified |
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
  telemetry migration and a Java graph-scope filter. The latter requires an
  engine release and dedicated capability negotiation; sequence it after the
  reported engine-tag freeze lifts, subject to verification at implementation.
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
  `nexus_rdr/232-research-1` through `-11`.
- ART `docs/rdr/RDR-084-list-parse-canonical-consolidation.md` and T2 record
  `ART_rdr/084`, used as the independent known-good source check.
- `src/nexus/plans/matcher.py`, `schema.py`, `runner.py`, `bundle.py`, and
  `answer_shape.py` on base commit `9a345da8b`.
- `src/nexus/mcp/core.py`: traversal, `nx_answer`, candidate cost choice,
  hydration, and run recording.
- `conexus/plans/builtin/hybrid-factual-lookup.yml` (plan 473's template).
- RDR-091: scope conflict filtering and ranking.
- RDR-097: hybrid factual lookup and the context-precision trade-off.
- RDR-189: target-definition layer and answer-object contracts (draft).
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
