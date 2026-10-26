---
title: "Target-Definition Layer for NL-to-X Querying: Answer-Object Families, Target Contracts, and Witness Obligations over the Existing Plan/Operator Realization Layer"
id: RDR-189
type: Architecture
status: accepted
accepted_date: 2026-10-24
priority: medium
author: Hal Hildebrand
reviewed-by: Sam
created: 2026-07-25
related_issues: ["nexus-sbl4m", "nexus-yg49g", "nexus-l2vam"]
related: [RDR-078, RDR-079, RDR-080, RDR-084, RDR-088, RDR-089, RDR-090, RDR-093, RDR-134, RDR-147, RDR-179, RDR-232]
---

# RDR-189: Target-Definition Layer for NL-to-X Querying

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Status note**: D1=(a), the full target-definition layer, was adopted by Sam
on 2026-10-24 after the formal gate passed. D2(b) remains the governance:
observed candidates, human-authored obligations, and human promotion. The
shared contract with RDR-232 is accepted: RDR-232 was accepted on 2026-10-25.
RDR-232's research is partial prototype evidence, not production or live-model
proof. P1 is unblocked; P5 first requires recovering or re-registering the
ghost roadmap source described below.

## Problem Statement

The source paper — *"Natural Language to What? A Vision for Intermediate
Representations in NL-to-X Querying"* (catalog tumbler `1.14.23`,
`knowledge__semantic-operators`) — splits NL querying into two layers:

- a **target-definition layer**: what answer object should exist,
  formalized as a **target contract** τ = ⟨K,B,R,W,P,U,C⟩ (kind,
  bindings, relations, witness / provenance / uncertainty obligations,
  constraints), tested for adequacy against a declared **environment
  profile** Γ(E);
- a **realization layer**: operators, RAG, and tool calls that attempt to
  construct that object, emitting a **preservation trace** ρ recording
  which obligations survived.

Its §7.6 addresses nexus almost directly: *"Semantic operators and
tool-mediated plans are realization mechanisms; NL-to-X asks which target
contract they should realize."*

Nexus has a mature realization layer but **no target-definition layer** that
binds answer-object families, obligations, and environment into a versioned
target contract. Existing `AnswerShape` classification distinguishes terminal
prose from raw retrieval/operator payloads, but it does not supply that target
contract or its preservation semantics.

#### Gap 1: No target identification anywhere in the answer path

`_nx_answer_plan_miss` (`src/nexus/mcp/core.py:5090`) decomposes a
question straight into a step DAG over a fixed tool vocabulary. No step,
prompt fragment, or gate asks what *kind* of answer object the question
requires. The target is not supplied by the caller either — it is simply
never modeled. Nexus answers "how do I get this?" well and has no
first-class answer to "what, structurally, am I trying to produce?"

#### Gap 2: No answer-object family, so plans have no declared obligations

`PlanResult` (`src/nexus/plans/runner.py:457-521`) contains step/final
outputs plus budget and continuation bookkeeping, but no target contract or
preservation trace. There is no
registry of named, versioned answer-object families (the paper's
`claim-conflict`, `evidence bundle`, `comparison object`,
`issue-evolution track` examples) with declared slots and constraints.
`AnswerShape` distinguishes raw retrieval/operator payloads from prose, but
it declares no required slots or witnesses. Plans can therefore produce
structurally valid prose without satisfying a target contract.

#### Gap 3: The witness primitives exist, are unreachable, and discard the surviving evidence

`operator_check` (`core.py:4177`) returns
`{ok, evidence: [{item_id, quote, role}]}` and `operator_verify`
(`core.py:4234`) returns `{verified, reason, citations: [str]}`. These are
the closest existing analogue to the paper's witness obligations (W).
Three defects, each verified:

1. **Unreachable from the plan-miss path.** `_ALLOWED_TOOLS`
   (`core.py:5192`) is `{search, query, traverse, store_get_many, extract,
   rank, compare, summarize, generate}` — `check` and `verify` are not in
   it. The inline planner cannot emit a witness step even if a plan wanted
   one; a step naming one is dropped with a `planner_step_dropped` warning
   (`core.py:5235-5238`).
2. **Inconsistent evidence typing between siblings.** `check` keys
   structured evidence to input ids; `verify` returns a bare
   `list[str]` of free-text locators. `bundle.py:610` already shares
   check's evidence-item schema in the bundled path, so the pull toward
   unification exists but is unfinished.
3. **The enrichment validator keeps the witness only on failure.**
   `nx enrich aspects --validate-sample N` runs `operator_verify` as a
   hallucination check. On `verified=True` it increments a counter and
   continues (`src/nexus/commands/enrich.py:2002-2004`), discarding
   reason/citations. Only nonverified rows are appended
   (`enrich.py:2015-2025`); errored cases are logged but not persisted. The
   paper's preservation trace is precisely the record of obligations that
   *survived*; this validator discards that half.

#### Gap 4: Plan dimensions route retrieval, not answer shape

`_REQUIRED_DIMENSIONS = ("verb", "scope")`
(`src/nexus/plans/schema.py:94`) — `verb` selects a retrieval strategy
family, `scope` selects a corpus. Neither encodes what kind of object the
answer should be. The paper's Γ(E) is about which answer objects an
environment can support: a different axis entirely.

## Context

### Background

Nexus selects and executes retrieval plans, then reduces their evidence into
an answer. The July investigation found that a plan could execute successfully
without declaring what kind of answer object it should produce or which
witnesses and constraints that object requires. The motivating evidence was a
small grown-plan population whose generation prompts carried those obligations
implicitly. The October RDR-232 investigation supplied a second, independent
failure: a multi-facet RDR synthesis selected a single-fact lookup plan and
retrieved mostly out-of-scope code evidence.

This RDR asks whether Nexus should make those obligations explicit and durable.
It does not assume that every execution failure needs a target registry. P1's
witness-schema work is independent hygiene; P2-P4 require a human adoption
decision and evidence from RDR-232's realization-layer work.

### Technical Environment

The Python client owns plan matching, plan execution, structured operator
calls, enrichment validation, and `nx_answer` result handling. The Java engine
owns catalog retrieval and run/plan persistence; it does not execute target
contracts. The relevant interfaces are `PlanResult`, runner hydration and
bundling, `operator_check`/`operator_verify`, `PlanHandler`, the composite
`nx_answer` run record, and the continuation envelope. RDR-232's proposed
request, coverage, traversal, and run-summary changes are separate draft work,
not deployed capabilities.

- **The historical Nexus roadmap is weaker than this paper and partly stale.**
  `1.11.197` ("Semantic Operator Pipelines — Nexus Roadmap & Basecamp",
  2026-06-04) targets the *sibling* paper NL2Pipe (`1.14.10`). It is now a
  title-only ghost record; the surviving July synthesis, not a current source
  document, preserves its row R6 as backend-portability work marked
  "low urgency while single-backend." That reasoning does not apply here:
  knowing what you are trying to produce matters with exactly one backend.
  Adopting R6 as scoped would still miss this paper.
- **R6's prior-art citation is wrong and should not be read as a
  precedent.** R6 cites "RDR-079 abandoned" as evidence a nexus abstract
  IR was tried and failed. RDR-079 was abandoned for a concurrency
  defect — a synchronous `subprocess.run` blocking the asyncio event loop
  — not on the merits of an IR design
  (`docs/rdr/rdr-079-operator-dispatch-and-execution.md`).
- **Nexus has a stronger witness substrate than the paper assumes.** The
  paper treats witness anchors as quoted spans, verifiable only by string
  matching. Nexus has content-addressed chunk identity (`chunk_text_hash`,
  full 64-hex / 32 raw bytes per RDR-180) joined through the
  `document_chunks` manifest (`documents.tumbler → document_chunks.doc_id
  → chash`). A witness can anchor on a `(tumbler, chash)` pair that is
  stable, deduplicated, and checkable without re-reading the source. This
  is the part of the design most likely to hold up.
- **No-knob-reflex directive applies.** If a target-definition layer is
  adopted it folds into the existing `nx_answer` / plan journey. No new
  client verb, no new user-facing configuration.
- RDR-088/089/093 built the operator algebra this layer would sit above.
  RDR-084 shipped the plan library's growth policy — a three-tier scope
  ladder that reconciles auto-growth with curation. Its former promotion
  command is retired; P2 defines the family-review workflow rather than
  pretending the old executable mechanism still exists.

## Shared Contract with RDR-232

RDR-189 and RDR-232 operate on the same request and evidence artifacts, but
own different parts of the design. This section supersedes the earlier option
of putting answer shape into identity-bearing plan dimensions.

| Concern | Owner | Shared boundary |
| --- | --- | --- |
| Durable answer-object families and target contracts | **RDR-189 P2/P3** | Candidate families are derived from observed answer/coverage shapes and human-promoted. They declare required slots, witnesses, and constraints. |
| Request analysis and target identification | **RDR-232**, consumed by **RDR-189 P3** | `RequestCoverage` carries routing kind, evidence facets, treatment constraints, named sources, and scope before plan matching. It is input to target construction, not the target contract itself. |
| Plan routing capability | **RDR-232**, derived from **RDR-189** families once available | A versioned top-level `plan_json` field, never identity-bearing `dimensions`. It is not a second permanent family registry. |
| Witness normalization | **RDR-189 P1** | `check`/`verify` return local ids that the host binds to typed `(tumbler, chash, collection)` evidence. Models never invent durable anchors. |
| Evidence-sufficiency assessment | **RDR-232 Phase 3** | Its live pre-reduction coverage ledger is input to **RDR-189 P4**, not proof that final answer obligations survived. |
| Preservation-trace semantics and family adequacy | **RDR-189 P4/P6** | Evaluates final answers against all declared obligations after generation/repair, including RDR-232's source/scope/facet coverage. |
| Evidence containment and bounded repair | **RDR-232** | Applies scope before manifest caps on the client, reserves primary evidence, and performs at most one targeted retrieval repair. Scope before the engine's 500-node graph cap is a gated RDR-232 Phase 1 step (step 6), taken only if capped traversals are observed. |

A **facet** is a topic requiring evidence. A **constraint** governs how that
evidence must be treated: temporal qualification, citation requirements, or a
prohibition on inferring current code. Constraints are obligations in the
preservation trace, not automatic retrieval facets. This distinction is part
of the target contract and must survive extraction.

The phase dependency is explicit. RDR-232 Phase 1 accepts explicit
`SourceRef`/`ScopeConstraint` inputs, so containment does not wait for the
family registry. Phase 2 unifies `RequestCoverage` and eligibility. Phase 3
emits coverage outcomes in run summaries for RDR-189's family research and
promotion review. P4 consumes the **live** RDR-232 coverage ledger directly;
persisted telemetry is optional evidence storage, never the runtime input to
post-realization assessment. RDR-232's witness work consumes RDR-189 P1's
contract when accepted; otherwise it uses its explicitly versioned local
subset and does not claim RDR-189 compatibility.

RDR-189's registry does **not** adopt RDR-232's six starter routing kinds as a
permanent taxonomy. P2 derives a small family set from observed request and
coverage evidence. Once a family exists, its routing capability is a derived
attribute with a versioned migration. Coverage outcomes in run summaries are
evidence for family research and human promotion, not an automatic adequacy
signal. Existing structural plan counters remain distinct; no plans-table
coverage-counter migration is introduced.

## Target Construction

`RequestCoverage` is request/evidence input. P3 constructs a separate,
versioned target contract before plan execution:

```text
TargetContract(
    family_id: string | unresolved,
    family_version: integer | null,
    contract_version: integer,
    request: RequestCoverage,
    required_slots: list[SlotObligation],
    witness_obligations: list[WitnessObligation],
    treatment_constraints: list[Constraint],
    environment: EnvironmentProfile,
)
```

**Family binding.** Match the request kind, facets, constraints, and source
shape against reviewed family declarations and exemplars. A family match must
instantiate its slots and witness obligations, not merely attach a label.
Exactly one compatible current family binds. Zero matches yields
`family_id=unresolved` and uses RDR-232's generic route without family-specific
preservation claims. Multiple matches returns an ambiguity diagnostic with
candidate ids; it never silently picks the first. A caller may pin
`family_id/version` explicitly through the existing `bindings` map, using
`target_family_id` and `target_family_version`. Both are required together;
the id must be a nonempty string and the version a positive integer (not a
boolean). Unknown or invalid values produce a diagnostic, never a guessed
family. These are routing inputs, not plan identity and not new configuration.
For `family_id=unresolved`, `family_version=null`, family-specific obligation
lists are empty, and the environment remains bound. A complete family-bound
target requires a positive family version and instantiated obligations; an
unresolved target is complete only as the explicit generic fallback and makes
no family-specific preservation claim.

**Versioning.** Routing capability stores `family_id`, `family_version`, and
`contract_version` separately from plan identity. Existing RDR-232 starter
kinds migrate to reviewed family capabilities by an explicit mapping, never by
assuming their names or number define the registry. Unknown-family or old-
version requests retain the local fallback and disclose the lack of family-
specific checks. New execution uses only a compatible current family. An old
family pin therefore uses generic realization and `requiresReview`; it does
not evaluate under retired obligations. Historical run summaries instead use
the immutable source-controlled declaration for their original
`family_id/family_version/contract_version`, which P2 retains after promotion;
interpreting an old run never reroutes its request. Changing a family's
obligations increments its version, and `contract_version` travels from target
construction into the run summary.

**Environment profile Γ(E).** The reduced executable profile records the
capabilities needed to assess this target: available collections/source
families, catalog/T2 access, normalized witness support, engine scope-filter
and run-summary capabilities, and whether continuation is available. It is
capability evidence, not a model of the whole world. Missing required
capabilities produces `requiresReview` or an unresolved target, not an assumed
`preserved`. P6 compares expected and actual profile support where relevant;
it does not claim a general environment-solver.

The target contract is complete only when family, obligations, and environment
are bound. The request's routing kind alone cannot produce that contract.

## Desired End State

Deliberately stated as a target, not a commitment — D1 may
resolve to "no."

1. A question entering `nx_answer` is analyzed deterministically-first into a
   request contract before plan matching. Once the family registry exists, P3
   binds a versioned family and environment to construct the target contract;
   unresolved families use the explicit local fallback.
2. The chosen family declares its obligations — required slots, facets,
   treatment constraints, and which claims require a witness.
3. The plan runner is checked against that declaration, not merely against
   structural well-formedness. RDR-232's eligibility and evidence containment
   are realization-layer checks, not a replacement target registry.
4. `PlanResult` carries a **post-realization preservation trace**: which
   declared obligations survived in the final answer, which were weakened or
   require review, each host-bound to `(tumbler, chash, collection)` where evidence came
   from the corpus. RDR-232's pre-reduction coverage ledger is an input to
   this assessment, not its substitute. Under RDR-200 continuation the caller
   performs reduction, then the host assesses the exact reported output;
   `ok=true` alone never completes this trace.
5. `operator_check` / `operator_verify` speak one evidence shape, are
   reachable from every planning path, and their surviving evidence is
   retained rather than discarded.

## Research Findings

### Investigation

The July research read the NL-to-X and NL2Pipe papers, the Nexus plan and
operator implementation, the Java engine boundary, the live plan library, and
the run telemetry schema. It included one live plan-reuse probe. The October
research revised the shared RDR-232 boundary, verified the witness and
preservation seams against current source, and repeated the adoption census
against the deployed service.

The current census uses `nx plan list --include-disabled -n 200 --json`,
`nx plan hygiene`, and `nx answer-runs --since 2026-07-25T00:00:00Z --json`.
The plan list returned 15 rows, below its 200-row limit; the run aggregate
returned 351 rows. Two plans tagged `ad-hoc,grown` remain in the visible
library. `nx plan list` labels their origin `user` because its origin heuristic
requires empty tags; tags, not that display label, identify the grown pair.
The run totals include degenerate rows and have the command's documented
continuation blind spot. They establish usage and routing volume, not answer
quality or target-family adequacy. Costs before August 22 are structural
artifacts, so the mixed-era average is not a clean cost baseline.

No new third-party dependency is proposed. Internal API verification is listed
under Critical Assumptions and the Finalization Gate.

### Key Discoveries

Findings marked **[verified]** were checked against the cited source or a
bounded live read. Historical snapshots say so explicitly. Unimplemented
behavior stays in Critical Assumptions with a phase verification method.

### R1 — The July absence scan is historical; target semantics remain absent **[verified at the time]**

`grep -rniE "target.contract|answer.object|answer.family|preservation.trace|witness.obligation" src/ docs/rdr/`
returned zero hits at the July research snapshot. That is not a current claim
now that RDR-189/232 introduce the vocabulary. Current `AnswerShape`
classification supplies terminal non-answer categories, not family-bound
obligations or post-realization preservation, so the underlying gap remains.

### R2 — The realization layer is genuinely mature **[verified]**

`plan_run` (`runner.py`) executes a JSON step DAG with `$stepN.field`
inter-step references, scope injection, and operator bundling
(`bundle.py:97`). `_OPERATOR_TOOL_MAP` (`runner.py:690-691`) routes
`check`/`verify` when a *saved* plan names them. This RDR proposes a layer
above this machinery, not a replacement for it.

### R3 — Witness primitives are structurally sound but mis-wired **[verified]**

Both operators dispatch through `claude_dispatch(prompt, schema)` with a
JSON schema, so the structured-output machinery needed for declared
obligations already exists. The defects are wiring, not capability: see
Gap 3 (1)(2)(3), each verified at the cited line.

### R4 — Curated and auto-grown already coexist; RDR-084 is the precedent, not the obstacle **[verified]**

The paper argues answer-object families must be curated, versioned system
artifacts: *"If every query produces a new handcrafted template, target
contracts become prompt artifacts rather than system artifacts."*
`docs/rdr/rdr-084-plan-library-growth.md:33` reads as the shipped
opposite — the plan library compounds "without any human curation step."

**That sentence is a summary of one tier, not the whole policy.** The
implementation (`src/nexus/mcp/core.py:5761-5797`) auto-saves a
successful ad-hoc plan with `scope=personal` and a config-driven TTL
(`plans.ad_hoc_ttl`, 30-day default, `config.py:677`). Its own comment is
explicit: *"scope=personal keeps growth isolated to the caller (the
project/global scopes are reached only via `/conexus:plan-promote`)."*
The library therefore ran three tiers at the July snapshot. The first two
remain current; the third now describes a governance concept, not an executable
`/conexus:plan-promote` command:

| Tier | Growth | Lifetime | Gate |
|---|---|---|---|
| Seeded templates (`conexus/plans/*.yml`; 14 in the July snapshot) | hand-authored | permanent | authoring |
| `scope=personal` grown plans | automatic | 30-day TTL | none |
| `scope=project` / `global` | promoted | permanent | human review (July: `/conexus:plan-promote`, since retired) |

Auto-growth is real but **bounded**: ephemeral, caller-isolated, and
unable to bind anyone else's queries. Curation is what converts a plan
into a shared, permanent artifact. This is exactly the paper's
governance position applied to the realization layer — nexus arrived at
it independently. The framing in this RDR's first draft ("not
reconcilable") was wrong.

**What does not transfer, and is the real question.** RDR-084's
promotion signal is cheap because a plan is *empirically falsifiable per
run*: it executed without error and produced an answer, so "it worked" is
observable. An answer-object family is a **type declaration whose defects
are silent** — a family missing a required slot still executes fine and
merely produces under-specified answers forever. "It ran" cannot be the
promotion criterion. See Decision D2.

### R5 — Target logic is client-side; integration has engine dependencies **[verified 2026-07-25; qualified 2026-10-24]**

*(Closed. Was "unchecked, not established as absent" in the first draft.)*

- **Vocabulary absent.** The July `service/src` search returned zero hits
  across 83 Java files; the October source check found no target/preservation
  execution layer either. This is a bounded absence check, not a claim that
  engine integration is unnecessary.
- **`PlanHandler.java` is CRUD, not execution.** Its route table is
  `/save /get /delete /disable /enable /set_scope_tags /list_active
  /search /list /exists /import /import_batch` plus three metrics routes,
  all delegating to `PlanRepository`. It stores `plan_json` as an opaque
  blob and never interprets it.
- **No execution engine-side.** The July `grep -rniE
  "plan_?run|execute.*plan|operator_?(extract|filter|rank|check|verify|generate)|claude_?dispatch"
  service/src/main/java` returned zero hits. A later re-run matched three
  unrelated identifiers; they are false positives for target execution, so
  the conclusion rests on the bounded handler/source review, not that old
  zero-hit count.

**Conclusion**: the engine is a persistence + retrieval substrate for the
plan library. Target-family interpretation, operator dispatch, and
preservation assessment are client-side. P2-P4 have no engine-side execution
planner, but their integration depends on RDR-232's engine run-summary
migration and graph-scope capability. Those are separate release/capability
dependencies, not a claim that the whole target layer is client-only.

**But P0 must weigh one consequence.** The engine *does* own the
`nx_answer_runs` telemetry surface — `/metrics/match`,
`/metrics/run_start`, `/metrics/run_outcome`. If D2 resolves to option
(c) (automatic promotion on a measurable adequacy signal), that signal
has to be persisted and queried through those engine routes: a
**second-lifecycle change** (engine-service tag + `REQUIRED_ENGINE_VERSION`
bump), not a client-only edit. Option (c) is therefore materially more
expensive than (a) or (b), and any P0 costing that assumes client-only
scope for D2(c) is wrong.

### R6 — The paper's §6 benchmark supplies no automatic adequacy signal **[verified 2026-07-25]**

*(Closes Open Question 2.)*

§6.5 "Direction 5: Target-Fidelity Benchmarks" is a research agenda, not a
metric suite. A benchmark instance is query + Γ(E) + available families +
acceptable contract set + expected witnesses/provenance/trace-status,
scored on three subtasks: regime-identification correctness, trace
fidelity (does ρ record preserved/approximated/weakened/requiresReview or
silently upgrade a degradation), and answer-object correctness. Proposed
scale is 10-20 human-annotated queries; the paper calls scaling past that
"a hard open problem."

**None of it is computable from current telemetry.** The run parent stores
`question, plan_id, matched_confidence, step_count, final_text, cost_usd,
duration_ms, created_at`; step children record operator/model/cost/elapsed outcomes. No
coverage, target, or preservation field exists. Plan success/failure remains
a structural counter, not family adequacy. No field backs any
of the three subtasks, and they cannot be computed before P2 (families) or P4
(traces) supplies the objects being measured. RDR-232's proposed run-summary
field is a storage dependency, not an existing coverage metric.

**Load-bearing negative for D2.** §6 supplies **no** human-label-free
adequacy signal. Every benchmark input is asserted as human-curated, and
the paper's own validity criterion is correlation with *expert* judgment.
The one self-computable candidate — `fit()` obligation status — is
circular: the system grading its own proposed contract against its own
environment profile, with no independent check. Combined with R5's
finding that D2(c) would also be a second-lifecycle change, **(c) is now
the expensive option with no known signal to justify it.**

### R7 — NL2Pipe's Phase A Linker is grounding, not target identification **[verified 2026-07-25]**

*(Closes Open Question 3.)*

The Linker (paper `1.14.10`) classifies question phrases as
matched/topical/absent against the data, then discovers "bridge entities"
needed to join across sources, LLM-verifying they exist. Its output feeds
the Planner as grounded referents that become filter predicates and join
keys. The paper's own ablation — "Linker and Planner are coupled, not
additive" — confirms grounding is its whole job.

By this RDR's own Gap 4 distinction it sits on the **referent** axis
(which concrete things does this query mention), never the
**answer-shape** axis (what kind of object should exist). NL2Pipe has no
analogue of τ at all; its target is implicitly whatever the σ→⋈→γ
pipeline emits. P3 would get nothing from it.

**Already covered, and on record.** The July roadmap snapshot's §4 row R1
stated: *"Query Linker: pre-grounding before inline planning...
Covered. RDR-147's ingest-time entity-linker + type-mismatch trigger
(Gaps 1,3) is NL2Pipe's Linker."* RDR-147 (`accepted`, world-blocked,
resume bead `nexus-3lu23`) subsumes it. OQ3 resolves to **no new work —
cross-reference RDR-147**; P3 needs a genuinely different mechanism
(answer-object-family classification) that nothing in NL2Pipe attempts.

### R8 — D1 evidence: prompt artifacts persist, but low usage does not **[verified 2026-07-25; refreshed 2026-10-24]**

*(Bears directly on D1. July measurements and October refresh.)*

**The July snapshot is historical.** `nx plan list` routes through the
service-backed plan library. At that time it returned 17 plans: 15 builtin
seeds and two auto-grown plans (353 and 354). The library hygiene scan was
clean. Those statements are not a description of the October deployment.

**The October census supersedes the usage premise.** The current list returned
15 rows below its 200-row limit: 12 displayed as builtin, two personal plans
tagged `ad-hoc,grown` (495 and 517), and one disabled legacy non-DAG row (352).
The origin column calls the grown pair `user` because its heuristic does not
recognize their tags. Both grown plans remain unused and unmatched. Hygiene
flags plans 474, 466, and 469 as always-failing; this is report-only evidence,
not a request to disable them during research.

The run report counted 351 `nx_answer` rows since July 25: 254 matched a plan
and 97 used inline fallback. The reuse loop therefore has substantial caller
volume; the July statement that calling volume was low must not drive D1.
These totals include degenerate rows and are blind to the continuation rows
described by the command's own caveat. They measure volume and route choice,
not answer quality.

**July reuse was sparse, not broken.** Only three plans then showed one use,
with the most recent use on May 27. RDR-179 had already warned that plan
counters and run records could disagree, so these were directional figures.
The October run census above replaces them as the usage premise.

**The prompt-artifact failure remains observable.** In July, both grown plans
hand-rolled answer structure into generation templates. In October, both
surviving grown plans still do so: 495 requests five extraction fields and a
reason-by-reason cited explanation; 517 requests three extraction fields and
a causal explanation plus recommended action. Neither declares a versioned
family or a post-realization obligation trace. This is two of two visible
grown plans, not a claim about all historical or user-authored plans.

RDR-232's run 722 is stronger evidence that the missing contract has user
impact: a multi-facet synthesis selected the single-fact lookup plan 473 and
accumulated 339 code references and one RDR reference. It demonstrates a
wrong-shape plan and an unbalanced evidence set, not a measured prevalence
across the 351 runs.

**Refinement to Gap 2.** `operator_generate`/`operator_summarize` still
have no shape parameter (Gap 2 stands). However `operator_extract(fields=…)`
*is* an existing ad-hoc slot declaration, used in both grown and seeded
plans — e.g. `fields: decision,rationale,rdr`
(`conexus/plans/builtin/debug-default.yml` family),
`fields: verb,scope,strategy,object,domain`, and plan 353's
`client_package_repo_owner,edge_auth_mechanism,token_type,tenant_data_path_flow`.
P2 should build on `extract`'s field list as the seed of a declared slot
set rather than starting from zero.

**Live probe — the reuse machinery is NOT broken.** A single
`nx_answer(dimensions={"verb":"research"}, scope="knowledge__semantic-operators")`
call on 2026-07-25 matched builtin plan 14, passed the scope filter and
the 0.40 confidence gate, was selected, executed, and incremented its
counters: `match_count` 136→137, `use_count` 1→2, `success` 1→2,
`last_used` → `2026-07-25T04:34:51Z`. **First try, post-cutover, end to
end.** The July timestamps therefore marked absent callers, not failed
machinery. The October census now supplies positive volume as well. Neither
observation measures target fidelity.

**The July probe exposed a defect, since repaired.** That run returned
*"The plan's retrieval steps returned zero results"* and was still recorded
`success=True`. In the July source, the post-execution fall-through recorded
success and the single-step reroute did so even for `"No results."` (the old
`core.py:5659`, `:5563`, and `:5579`). Success/failure was therefore an
exception counter, not an outcome counter; `nexus-yg49g` tracked the defect.
Current source instead records empty retrieval with `success=False` before
plan growth (`core.py:12366-12434`) and the single-step reroute with
`success=not _nx_answer_text_is_empty(...)` (`core.py:11794-11831`). The
remaining limitation is that structural counters do not measure family
adequacy.

**D1 reading.** Recommend (a), on the current prompt artifacts and run 722,
not on the retired low-usage premise. This supports investigating a target
layer; it does not prove that P2-P4 will improve answers enough to justify
their cost. P1/P5 remain independently justified hygiene, and P6 supplies the
stop measurement for the larger architecture.

D2(b) now has a larger run history and two current prompt-artifact exemplars,
but coverage data at the granularity needed for family promotion does not yet
exist. P2 therefore starts with human-reviewed exemplars and counterexamples,
then uses RDR-232's run summaries when they exist. `nexus-yg49g` and
`nexus-sbl4m` are closed, and the deployed engine 0.1.164 contains their fix
(first engine tag 0.1.57). This verifies release ancestry and deployment
version, not a fresh behavioral TTL experiment. Historical counters retain
their old semantics and cannot certify family or plan adequacy.

P1 and P5 remain unaffected by D1's outcome.

### R9 — Current governance has no automatic family-adequacy signal **[verified 2026-10-24]**

The old `/conexus:plan-promote` skill and its proposal template were deleted
by `af1b292bc` on August 22. `nx plan` has no promote command, and
`evaluate_gates` has no production caller. The plan-level success gate is not
a family-adequacy metric, and RDR-232's proposed pre-reduction coverage cannot
prove that a family declared the right obligations or that terminal prose
preserved them. P2 therefore uses the source-controlled review workflow below,
not the retired command or automatic promotion. *Source: current `nx plan
--help`, `plans/promote.py`, retirement commit, and research 13.*

### R10 — Current interfaces expose the required work, not the proposed behavior **[verified 2026-10-24]**

The inline planner omits `check`/`verify`; saved-plan dispatch includes them,
and adjacent bundling can hide their intermediate outputs. Hydration removes
`ids`/`collections`. Its `verify` path falls through to `inputs`, which direct
dispatch rejects while `evidence` remains missing (`runner.py:1456-1476,
1979-1991`). `PlanResult` and the continuation envelope have no target,
coverage, environment, or preservation fields. `nx_answer_report` accepts only
id/ok/optional short excerpt. Current continuation-envelope failure can rerun
the whole plan and repeat paid prefix work (`core.py:12237-12270`). P1/P4
must correct these paths and test them; source inspection is not behavioral
proof. *Source: current runner/core/bundle/operator/enrichment/continuation
source and research 14.*

Only `aspect_validation_failures.jsonl` exists. Successful validation drops
its reason/citations, and errored cases are not persisted. The run parent,
step children, and result envelope have no coverage or preservation member.
The engine persists plans and telemetry but does not execute target contracts.
RDR-232's coverage ledger, run summary, and graph-scope filtering are accepted
design with no code yet; the graph-scope filter is a gated Phase 1 step. Current `AnswerShape`/`NON_ANSWER_SHAPES` rejects raw retrieval,
hydration, extraction, rank, other operator payloads, listings, empty output,
and errors; the write side records these as failure and excludes them from
growth. This is terminal structural classification, not requested-family or
final-obligation assessment, and its extra-top-level-key limitation remains.
RDR-232's `INSUFFICIENT_EVIDENCE` shape does not replace it: it is added to
`AnswerShape` but not to `NON_ANSWER_SHAPES`, carries prose behind a
disclosed-gaps notice, records success, and blocks plan growth with its own
flag.
*Source: `enrich.py:1072-1075,1950-2025`, telemetry Liquibase and handlers,
PlanHandler/PlanRepository, `plans/answer_shape.py:1-112`,
`core.py:12436-12454`, and research 14 and 19.*

### R11 — The historical roadmap is a ghost record **[verified 2026-10-24]**

The basecamp T2 record identifies roadmap `1.11.197` by its exact canonical
title and historical short doc id. Reads by that id and exact title returned
not found. The August ghost-note census explicitly lists this tumbler and
title among 228 records whose content is absent from reachable stores. The
July synthesis preserves the R6 claims as secondary evidence, not a current
owning document. P5 therefore requires source recovery or explicit replacement
and re-registration before correction. *Source: T2
`nexus/semantic-operator-pipelines-basecamp-2026-06-04` and
`nexus/ghost-notes-knowledge__knowledge-2026-08-28-titles`; research 15.*

### Critical Assumptions

The source research is complete. These are design assumptions whose behavioral
proof belongs to the named implementation phase; none is claimed as already
implemented.

- [ ] **A1 — direct and bundled witness paths can share typed evidence and
  visible dispatch.** **Status**: source interfaces verified; behavior
  unverified. **Method**: Source Search, then P1 integration tests. Current
  `check`/`verify` schemas differ, hydration drops ids/collections, and
  `verify` id-hydration falls through to an unsupported `inputs` argument
  instead of `evidence`. P1 must define claim/evidence hydration, host-bound
  identity, returned-id validation, quote/span fidelity checks, and an explicit
  non-bundling boundary.
  Ship P1 only when direct/bundled success and failure cases, shared-chash
  ambiguity, and all enrichment outcomes pass.
- [ ] **A2 — a small human-reviewed family set can bind requests without silent
  ambiguity.** **Status**: governance/source evidence verified; family adequacy
  unverified. **Method**: Source Search, then P2/P3 labeled fixtures. Two
  visible grown prompt artifacts and run 722 motivate the work but do not
  define the families or their prevalence. P2 activates only a versioned,
  human-reviewed registry with counterexamples; P3 must pass exact, unresolved,
  ambiguous, pinned, and old-version cases before target routing ships. The
  caller supplies the two pin values through the existing `bindings` map as
  `target_family_id` and `target_family_version`, not through `dimensions`.
- [ ] **A3 — post-realization preservation can be assessed without repeating
  execution or upgrading unsupported states.** **Status**: current boundaries
  verified; assessment and live-model reliability unverified. **Method**:
  Source Search, then P4 deterministic integration and labeled live evaluation.
  `PlanResult` and continuation currently lack target/trace fields; current
  continuation-envelope failure can rerun the whole plan. P4 must resume from
  retained state or disclose a non-reexecution fallback, distinguish coverage
  from final preservation, and handle pending/failed/redacted/old-envelope
  paths. No unsupported obligation may become `preserved`. The host assesses
  the exact continuation output against retained target/ledger state and
  validates its digest and evidence ids; `ok=true` is execution status only.
- [ ] **A4 — the target layer's measured value justifies its cost.**
  **Status**: deliberately unevaluated until P4/P6; not a precondition for P1
  hygiene. **Method**: paired human-labeled P6 evaluation with a pre-registered
  fidelity margin and added-cost/latency ceiling. D4 stops or narrows the
  architecture if that rule is not met. Current execution counters and the
  351-row mixed-era aggregate cannot answer it.

### API Verification Boundary

Current source proves the seams and defects above, not the proposed behavior.
RDR-232's `RequestCoverage`, pre-reduction coverage ledger, run-summary JSONB,
and engine pre-cap graph filtering are draft dependencies, not existing APIs.
P5's historical roadmap tumbler `1.11.197` does not resolve in the current
catalog. T2's basecamp record and the ghost-note census identify its exact
canonical title and confirm that its content is absent from reachable stores.
The July synthesis preserves the old claims, but the owning document and
current wording remain unavailable. P5 must recover or explicitly reconstruct
and re-register the source before editing; it must not substitute another
roadmap.

## Decision (draft — options to resolve in research)

**D1. Does nexus adopt a target-definition layer at all?** **DECIDED: (a),
adopt the full layer (Sam, 2026-10-24).** R8's recommendation was based on
current prompt artifacts and run 722, not the retired low-usage premise. The
degradation is
real (2/2 grown plans embed answer contracts in prompt strings) and a
live probe confirmed the reuse machinery works end to end. The shared
contract with RDR-232 defines how the layer is introduced without a second
registry or preservation trace. P2 needs reviewed exemplars and counterexamples
from observed shapes, supplemented by RDR-232's run coverage summaries. The
promotion-signal and TTL defects (`nexus-yg49g`, `nexus-sbl4m`) are closed;
their historical evidence is not a current blocker. The deployed engine is
0.1.164; release ancestry shows the fix first shipped in 0.1.57. No fresh TTL
behavioral probe was run for this research, so disuse-based expiry still gets
an implementation acceptance test before P2 relies on it. P1/P5 are
independent of D1; P5 still requires source recovery.

**D2. What is the promotion criterion for a family?** Not
curated-vs-auto-grown — R4 establishes that those artifacts coexist. Reuse
the `personal → project → global` governance concept, not the retired
`/conexus:plan-promote` command. P2 defines the family-specific workflow
below. The open part is the *signal*, since execution success cannot detect a
silently under-specified family. Options: (a) human-only proposal and
promotion; (b) automatic candidate observation from answer/coverage shapes,
human-authored obligations, and human promotion; (c) automatic promotion on a
measurable adequacy signal. **R6 settles against (c)**: the paper supplies no
human-label-free signal, and current counters measure structural outcomes,
not family adequacy. RDR-232's proposed coverage outcomes improve the evidence
available to (b); they do not establish that a family's required slots are
correct. Recommend (b).

**D3. Where does target identification live?** Resolved in the shared
contract: deterministic-first request analysis inside `nx_answer` before plan
matching, using RDR-232's `RequestCoverage`. It is not limited to the
plan-miss path, not a new user-facing MCP surface, and not an identity-bearing
plan dimension. `dimensions` remains plan identity; routing capability is a
versioned `plan_json` attribute derived from the target family when available.
An explicit family pin uses the two reserved `bindings` keys named above,
never identity-bearing `dimensions`.
An optional single structured extraction call is allowed when deterministic
analysis cannot establish the shape; its cost and timeout are measured
separately. Unknown or unresolved inputs use conservative fallback rather than
silently narrowing to a lookup plan.

**D4. Is adequacy-checking (regime diagnosis) worth its cost**, given
`plan_match`'s cosine-similarity reuse gate already does something
directionally similar for free? **Deferred deliberately.** P0 cannot answer
this before P2/P4/P6 produce targets, traces, and measured coverage. Pre-
register the evaluation and a stop threshold in P6; decide D4 from those
measurements, not from the absence of a registry.

## Approach (phased, draft) — Implementation Plan

**Note on phase derivation**: the synthesis recommendation "extend
`operator_check`/`verify` into a declared witness mechanism" is split
across **P1 and P4**, because its two halves have different gating. The
evidence-shape work stands alone and pays off whether or not this RDR
proceeds; the declared-obligation work has no declaration source until P2
supplies a reviewed family registry. Splitting avoids touching the same
RDR-088 contract twice.

### Minimum Viable Validation

Bind one multi-facet request to one human-reviewed family and execute typed
direct evidence through terminal reduction. A deterministic fixture produces
one final answer preserving a required obligation and another dropping a
different obligation despite covered pre-reduction evidence. The result must
show host-bound witnesses, distinct final states, and separate coverage and
preservation fields. A continuation case stays `pending` until its versioned
finalization report and finalizes without repeating the plan prefix; an
envelope-assembly failure must not invoke the current whole-plan rerun. The
caller returns exact output that drops a required obligation with `ok=true`;
the host must still assess that obligation `weakened` or `requiresReview`,
not `preserved`. These proofs belong to P1/P3/P4, not the later P6 evaluation.

### P0 — Decision gate (no code)

**D1 and D2 are decided.** Sam adopted D1=(a), the full target-definition
layer, on 2026-10-24. D2(b) is the governance: observed candidates,
human-authored obligations, and human promotion. P2–P4 proceed under their
stated prerequisites and P6's later keep/narrow/stop measurement. P1/P5 remain
independent. The October census refreshed the July evidence; RDR-232's
finer-grained coverage summaries are future inputs, not current
family-adequacy measurements. D4 is not decided here; it requires P4/P6
evidence and a pre-registered stop threshold.

### P1 — Evidence-shape unification (independent hygiene)

P1 defines one versioned witness schema for direct and bundled operators:
`item_id`, quote/span/role, and `unresolved`/`ambiguous` states. The host binds
that id to `{tumbler, chash, collection}` from the evidence stream; the model
never emits or infers hashes. Hydration preserves typed evidence items and
source identity, direct and bundle schemas stay aligned, and host-side joins
reject returned ids outside the supplied evidence. Host-side quote/span
validation also requires containment in the bound evidence after Unicode NFKC
and whitespace normalization; a fabricated quote or wrong-document span yields
`ambiguous` or `requiresReview`, never a valid witness. Shared chashes across
documents require the parent document/collection or an `ambiguous` state.

- Align `operator_verify` with the structured evidence schema used by
  `operator_check`; add `check`/`verify` to `_ALLOWED_TOOLS`. Define `verify`'s
  claim/evidence hydration explicitly: current ids-hydration falls through to
  `inputs`, which direct dispatch rejects while `evidence` remains missing.
  The P1 acceptance test must execute this path, not only compare schemas.
- Declared witness obligations use host-visible direct dispatch or an explicit
  non-bundling boundary. Adjacent `check`/`verify → generate` steps cannot
  hide intermediate witness output inside a bundle.
- Preserve successful `nx enrich --validate-sample` witnesses in a new
  `aspect_validation.jsonl` all-outcomes record beside the existing
  `aspect_validation_failures.jsonl`. Keep `--validation-out` exclusively for
  the legacy failures file. Add `--validation-all-out` for the additive file,
  with a distinct default under the Nexus config directory, and reject equal
  paths rather than mixing schemas.
  Each row carries `status` (`verified`, `disagreement`, `unverifiable`,
  `errored`), source identity, extracted claims, host-bound witness items,
  verify reason, timestamp, and evidence-window metadata. The existing
  failures file keeps its schema and consumers; the new file is additive.
  It stores evidence anchors and claim identifiers, not full document text or
  private source hints by default. A new `--validation-trace` /
  `--no-validation-trace` CLI option, default `--validation-trace`, passes
  `trace` into `_run_validation_sample`; this is distinct from `nx_answer`'s
  `trace` argument and needs no new configuration file. `trace=False` redacts
  free text and retains only permitted ids/states/counts.
  Retention and deletion follow the existing validation-output lifecycle;
  there is no new service or database table.
- **Requires an RDR-088 amendment** and synchronized updates to
  `operator_requests.py`, `core.py`, `bundle.py`, hydration, and the documented
  return contracts. Compatibility tests cover direct/bundled operation,
  unresolved/ambiguous anchors, verify failure, and successful-enrichment
  retention.

### P2 — Answer-object family registry (requires D1=a, D2, reviewed evidence)

Build a small registry from reviewed request/response exemplars and
counterexamples, with actual `nx_answer_runs` and RDR-232 coverage outcomes as
candidate evidence. Evidence shows what requests and plans do, not which
normative slots a family ought to require. Humans author the required slots,
facets, treatment constraints, and witness obligations and promote named,
versioned families after reviewing counterexamples. The evidence determines
family names and count; 3-4 is not a quota.

The registry owns durable families; RDR-232's starter routing kinds are inputs
to research, not a second registry. Once a family exists, its routing
capability is a derived, versioned `plan_json` attribute.

Family promotion is an explicit reviewed change to the source-controlled
registry. A candidate carries its observed request/run references, proposed
family id/version, human-authored slots and witness obligations, positive
exemplars, counterexamples, unresolved disagreements, and the environment
capabilities used. A reviewer compares the candidate against those labels and
records approval or rejection on the change; coverage outcomes are evidence,
never an automatic approval. The registry read path accepts only a validated,
versioned declaration with an immutable revision reference. Promotion tests
prove that an unreviewed candidate cannot become active, that counterexamples
and disagreements remain attached, and that activating a new version does not
rewrite old run interpretation. No new MCP verb is required, and the retired
`/conexus:plan-promote` skill is not a dependency.

### P3 — Target construction in the answer path (requires P2 + RDR-232 Phase 2)

Consume RDR-232's `RequestCoverage` and bind exactly one reviewed family and
version, its obligations, and the environment profile using the Target
Construction contract above. `RequestCoverage` alone is not the target. The
result drives both eligibility and inline planning. Unknown family, ambiguous
family, missing capability, and old family versions use the explicit fallback
matrix rather than silently narrowing to a lookup or inventing obligations.

Optional structured extraction uses at most one direct operator call with
separately measured cost and deadline. Acceptance fixtures cover family
binding, ambiguity, unknown family, explicit family/version pins, migration
from RDR-232 starter capabilities, `contract_version` propagation, unresolved
`family_version=null`, and unavailable environment capabilities, and
both valid and invalid pins. Fixtures require both pin keys together, reject
partial pins, empty ids, non-positive or non-integer versions, booleans, and
unknown ids/versions, and prove that a valid pin resolves an ambiguity without
changing plan identity.

### Cross-Path Fallback Contract

| Condition | Runtime result | Persistence / caller signal |
| --- | --- | --- |
| No compatible family | RDR-232 generic realization; `family_id=unresolved`; no family-specific preservation claim | Return the target's unresolved state; do not invent slots or obligations. |
| Multiple compatible families | Stop family-specific realization and return candidate ids with an ambiguity diagnostic | No first-match choice; a valid `bindings.target_family_id` plus `target_family_version` pin resolves the ambiguity. |
| Old family or contract version | For new execution, use generic realization and `requiresReview`; do not evaluate under retired obligations | For historical runs, interpret evidence under the immutable source-controlled original family/contract declaration; preserve all version fields in the summary and never reroute the old request. |
| Missing required environment capability | Keep primary/direct evidence; omit dependent secondary behavior; unresolved target or `requiresReview` as appropriate | Mark the capability missing; never infer support from an ignored parameter or HTTP 200. |
| RDR-232 required coverage `partial` after its one repair | RDR-232 returns `answer_shape=insufficient_evidence`: the prose behind a notice opening `[insufficient-evidence: <n> unresolved]` that names each unresolved facet, plus the coverage ledger | Count as success (`success_count`; outside `NON_ANSWER_SHAPES`), block plan growth with RDR-232's own no-grow flag, and mark affected P4 obligations `unavailable` or `requiresReview`; never assess the partial prose as `preserved`. |
| RDR-232 required coverage `unavailable` (no usable answer) | RDR-232 returns an existing non-answer shape with its notice and chunk references, plus the coverage ledger | Increment `failure_count`, suppress plan growth, and mark affected P4 obligations `unavailable`. |
| Old client/envelope without target fields | Preserve legacy answer behavior and structural classification | Do not fabricate a target or trace; mark `target=unavailable` in new responses that can represent it. |
| Run-summary persistence unsupported or readback dropped | Return the live target/coverage/preservation result without claiming storage | `persistence=unsupported` or `dropped`; no second write, no silent success. |
| `trace=False` | Authorized handoff/report protocol retains the question, facets, source hints, evidence text, exact final output, and typed ids needed for reduction and host assessment | Logs, telemetry, validation files, and other persisted artifacts keep only permitted ids, states, counts, and capability outcomes; redact question/facet text, source hints, quotes, and other free text. Exact-output persistence requires an explicit bounded retention policy, not blanket persistence. |
| Continuation handed off, report not yet received | Preservation is `pending`, not preserved | Hand off target, live ledger, environment, and finalization obligations. |
| Continuation report has exact final output | Host validates continuation identity, schema version, digest, and typed `returned_evidence_ids`, then assesses the output against the retained target and ledger | `ok` reports execution only. Caller-supplied preservation states are never authoritative. |
| Continuation report missing, failed, or lacks/mismatches required output | `unavailable` or `failed` per obligation; `requiresReview` for an integrity mismatch; no final preservation claim | Record the reason and preserve the partial ledger. `ok=true` alone never completes P4. |

A fallback never upgrades an unresolved or unsupported state to `preserved`.

### P4 — Post-realization preservation (requires P1 + P2 + P3 + RDR-232 Phase 3)

Evaluate the **realized final answer** against the target contract after
terminal generation and any RDR-232 repair. The live RDR-232 pre-reduction
coverage ledger is direct input; persisted run summaries are optional evidence
storage, not runtime input. Coverage says evidence existed for a facet. P4
asks whether the answer preserved each required slot, treatment constraint,
and witness obligation.

Use per-obligation states `pending`, `preserved`, `approximated`, `weakened`,
`requiresReview`, `unavailable`, and `failed`, with host-bound witnesses.
Declared witness obligations use direct host-visible dispatch or an explicit
non-bundling boundary; no hidden bundled intermediate. Reuse RDR-232's
runner-owned pre-reduction hook for evidence assembly, then perform P4's
post-realization assessment without repeating the plan prefix.

Thread the live trace through `PlanResult` and the `nx_answer` envelope. Under
RDR-200 continuation, the caller performs reduction but the **host performs
P4 assessment**. Hand off the target, live coverage ledger, environment, and
finalization obligations, retained by continuation id. Extend
`nx_answer_report` with a versioned finalization payload containing
`continuation_id`, `finalization_schema_version`, the exact final output (or
an explicit unavailable state), its digest, and `returned_evidence_ids`.
Each returned id is a typed local id with host-bound
`{tumbler, chash, collection}`; unavailable output carries an empty list and
the reason. `ok` means reduction executed; it is not a preservation state.

On report, the host validates the continuation identity, schema version,
output digest, and returned evidence ids against retained state, then assesses
the exact output against the target obligations. Missing or mismatched output
yields `unavailable` or `requiresReview`, never `preserved`; caller-supplied
per-obligation states are not authoritative. The current id/ok/500-character
excerpt cannot carry this proof. Authorized handoff/report protocol payloads
retain the content needed for reduction and host assessment under
`trace=False`; persisted logs, telemetry, validation files, and other
artifacts redact free text as the fallback matrix specifies. Exact-output
persistence requires an explicit bounded retention policy.

Retain resumable execution state across envelope-assembly and finalization
failures. Today's continuation fallback can call `plan_run` again without the
cut, repeat paid prefix operators, and lose the original budget accounting.
P4 may not inherit that path: either resume the retained state within one
shared budget or return a disclosed preservation-unavailable result without
repeating the plan prefix. Define failed-operator, unavailable-evidence,
old-envelope, and `trace=False` behavior. Persistence uses RDR-232's run-summary
capability; lack of persistence does not erase the live trace or falsely claim
it was stored.

### P5 — Roadmap source recovery and correction (independent)

The July roadmap, tumbler `1.11.197`, is now one of the 228 title-only ghost
records in `nexus/ghost-notes-knowledge__knowledge-2026-08-28-titles`. Its
canonical title is recorded in `nexus/semantic-operator-pipelines-basecamp-2026-06-04`;
its content is absent from reachable T3 collections, and the July synthesis is
the surviving secondary evidence for its R6 wording.

Recover a legitimate original source or explicitly create a replacement
roadmap from the surviving evidence. Do not edit an unrelated document or
claim to know current wording. In the recovered/replacement source, replace
the "RDR-079 abandoned" prior-art citation with the verified concurrency
rationale and separate target-definition urgency from backend portability.
Register a durable `source_uri`, manifest, and catalog identity, then verify
the text and those identities. Source recovery precedes the correction; this
phase is independent of D1 but is not an unblocked trivial edit.

### P6 — Target-fidelity benchmark and D4 decision (requires P4)

Per R6: the paper's §6 benchmark scores regime identification, trace
fidelity, and answer-object correctness — none of which have anything to
score until families (P2) and preservation traces (P4) exist. Scoped here
rather than in RDR-090 (different paper, different axis, parked). Reuse
RDR-232's labeled request/source/facet/constraint fixtures where applicable,
but do not substitute retrieval NDCG or execution success for target fidelity.
Expect 10-20 human-annotated queries and no automatic scaling path; if that
cost is unacceptable, this phase is the first to drop. Pre-register the D4
stop threshold before measuring: if target-aware routing and preservation do
not improve human-labeled target fidelity enough to justify their measured
cost, stop at P1/P5 and the RDR-232 realization-layer safeguards rather than
keeping a registry without demonstrated value.

## Alternatives Considered

### Alternative 1: Fold the work into realization-layer RDRs

**Description**: Fold into RDR-147 / RDR-090 / RDR-134, as `1.11.197` plans to fold
  NL2Pipe's ideas. Rejected as the default: those RDRs address entity
  grounding, benchmarking, and taxonomy-aware recall — all realization-layer
  concerns. Filing a layer-above concern inside them repeats the structural
  misfiling R6 already shows.

**Pros**: reuses active retrieval, entity-resolution, and benchmark work;
avoids another architecture document for those components.

**Cons**: those RDRs do not own target contracts or final obligation
preservation. Folding this work into them hides the decision and makes their
acceptance scopes misleading.

**Reason for rejection**: use their realization-layer outputs, but keep the
target-definition decision here.

### Alternative 2: Adopt only the roadmap's abstract plan IR

**Description**: adopt R6 as scoped, an abstract plan IR for backend
portability.

**Pros**: smaller conceptual change and direct alignment with the historical
roadmap.

**Cons**: backend portability does not declare what answer object is required
or which witnesses must survive.

**Reason for rejection**: orthogonal to this paper's argument, which holds at
one backend.

### Alternative 3: Stop at evidence hygiene

**Description**: ship P1's witness/output corrections, recover the roadmap in
P5, and do not build P2-P4.

**Pros**: fixes observed witness defects without a registry or a new
assessment stage.

**Cons**: leaves wrong-shape routing and final obligation preservation to
RDR-232's realization safeguards; no durable target families or trace.

**Reason for rejection as the default**: reasonable if Sam chooses D1=b or
P6 fails its stop rule, but the current prompt artifacts and run 722 justify
evaluating the larger layer rather than presuming it has no value.

## Trade-offs

### Consequences

P1 changes an existing operator contract and adds a local validation-output
file. P2 introduces a small, human-governed registry rather than a second
plan-dimension taxonomy. P3/P4 add request/target metadata and live traces to
existing result envelopes; they do not add a second planner or a new telemetry
table. Older clients retain legacy answer behavior when they cannot represent
those fields. Run-summary persistence follows RDR-232's additive migration and
capability rules; no unsupported persistence is reported as successful.

### Risks and Mitigations

- **Risk**: a family declaration omits obligations and gives false confidence.
  **Mitigation**: human-authored exemplars and counterexamples, explicit
  versions, ambiguity diagnostics, and P6 measurement before D4 adoption.
- **Risk**: the model invents or misattributes a durable witness.
  **Mitigation**: host-bound ids, input-id validation, document/collection
  context for shared chashes, and unresolved/ambiguous states.
- **Risk**: pre-reduction evidence coverage is mistaken for final preservation.
  **Mitigation**: separate ledgers, a post-generation assessment, and explicit
  continuation/failure states. The host assesses exact continuation output;
  caller-reported success is not preservation evidence.
- **Risk**: assessment cost exceeds the benefit on long-running requests.
  **Mitigation**: deterministic checks first, bounded grouped assessment, no
  repeated plan prefix, and a pre-registered P6 stop rule.
- **Risk**: partial engine/client support silently drops evidence or traces.
  **Mitigation**: versioned envelopes, capability negotiation/readback, and the
  fallback matrix; missing support never upgrades to `preserved`.

### Failure Modes

Unknown or ambiguous family binding returns the explicit fallback rather than
choosing the first candidate. Missing capabilities omit dependent behavior and
retain direct evidence. Partial required evidence after RDR-232's repair
returns the prose behind RDR-232's disclosed-gaps notice, and P4 never marks
that prose `preserved`; no usable answer returns RDR-232's non-answer notice.
A model or
hydration failure marks affected obligations `unavailable` or `failed`; it does
not mark them preserved. An old envelope keeps legacy behavior and carries no
fabricated trace. A continuation without a final report stays `pending`; a
failed or missing report is disclosed. Authorized handoff/report protocol
retains required content; persisted validation output and telemetry redact it
under the fallback matrix.

- **Cost**: deterministic-first target identification avoids a model call on
  clear requests. An optional single structured extraction call adds latency
  only when needed; facet assessment is grouped and RDR-232 permits one repair
  cycle. Measure those costs against `nx_answer`'s existing distribution
  rather than assuming an LLM round-trip on every call. D4 must weigh the
  residual cost against measured coverage failures.
- **Governance**: the `personal → project → global` scope ladder supplies a
  useful curation model, but its former promotion command no longer exists.
  P2 must land the explicit family-review workflow above rather than calling
  a retired skill. The residual cost is the promotion signal: a family's
  defects are silent, so a human stays in the loop.
- **Blast radius**: P1 touches a shipped RDR-088 contract with a live
  consumer and three documented surfaces.

## Test Plan

- **Scenario**: direct and bundled `check`/`verify` consume the same evidence —
  **Verify**: one versioned schema, host-bound source identity, returned ids
  restricted to supplied evidence, quote/span containment in the bound evidence,
  and no hidden intermediate witness output. Fabricated quotes and
  wrong-document spans fail rather than becoming valid witnesses.
  A saved-plan `verify` id-hydration case supplies both `claim` and `evidence`
  and no rejected `inputs` argument.
- **Scenario**: identical chunk text occurs in two documents —
  **Verify**: parent document and collection disambiguate the witness, or the
  result is `ambiguous`; another document's section metadata is not inherited.
- **Scenario**: enrichment validation succeeds, disagrees, is unverifiable, or
  errors — **Verify**: the additive all-outcomes JSONL retains the correct
  status and host witness, the legacy failures file is unchanged, the two
  output paths cannot collide, and `--validation-trace` default and
  `--no-validation-trace` redaction both propagate into the validator.
- **Scenario**: zero, one, or several reviewed families match a request —
  **Verify**: unresolved, exactly bound, or ambiguity diagnostic respectively;
  no first-match selection or invented obligations.
- **Scenario**: a family or contract version is pinned or is no longer current —
  **Verify**: new execution uses the generic route plus `requiresReview`, never
  retired-obligation evaluation; historical summaries use the retained
  immutable original family/contract declarations without rerouting. An
  unresolved target carries `family_version=null` and no family obligations.
- **Scenario**: a required environment capability is absent or an engine ignores
  a new field — **Verify**: missing capability is disclosed; no HTTP 200 or
  cached capability is treated as proof; unsupported persistence is not success.
- **Scenario**: RDR-232 coverage is `partial` after one repair —
  **Verify**: the prose is returned behind the `[insufficient-evidence: <n>
  unresolved]` notice, the run counts as success, growth is blocked by
  RDR-232's no-grow flag, the shape stays outside `NON_ANSWER_SHAPES`, and P4
  does not claim preservation for the partial prose. When coverage is
  `unavailable`, the non-answer notice and chunk references replace prose and
  `failure_count` increments.
- **Scenario**: initial evidence covers a facet but terminal prose drops its
  constraint or witness — **Verify**: pre-reduction coverage remains an input;
  the post-realization trace marks the obligation `weakened` or
  `requiresReview` rather than inheriting `covered` as `preserved`.
- **Scenario**: continuation has not reported, its report fails, or envelope
  assembly fails — **Verify**: `pending`, `unavailable`, or `failed` as
  applicable; no early or fabricated final preservation claim, no repeated
  plan-prefix execution, and one shared cost/deadline accounting path.
- **Scenario**: continuation returns `ok=true` but its exact output drops a
  required obligation — **Verify**: the host assesses `weakened` or
  `requiresReview`, not `preserved`; missing/mismatched output cannot complete
  P4 and caller-supplied preservation states are not trusted. An unknown or
  mismatched `returned_evidence_ids` entry is rejected, and unavailable output
  carries an empty list plus its reason.
- **Scenario**: `trace=False` with private sources and facets — **Verify**:
  authorized handoff/report protocol retains the content needed for reduction
  and host assessment; logs, telemetry, validation files, and other persisted
  artifacts retain only permitted ids/states/counts and redact free text.
  Protocol functionality and persisted-artifact redaction have separate tests.
- **Scenario**: old clients and engines exchange new and old envelopes —
  **Verify**: legacy behavior remains, new responses disclose unavailable
  target/trace state, and persistence readback/drop handling matches RDR-232.
- **Scenario**: P6 labeled requests and counterexamples —
  **Verify**: independent human labels score target selection, final obligation
  preservation, and trace fidelity; execution success and retrieval NDCG are
  not substituted for those measures.

## Validation

### Testing Strategy

P1 uses real direct and bundled operator dispatch with deterministic model
outputs and real evidence hydration fixtures. Cross-document shared-chash cases
must include the parent document and collection, and every successful outcome
must be checked in the actual output file, not merely counted. P3/P4 use typed
request/family/environment fixtures and assert the complete state transition,
including the negative and compatibility paths above. Tests use fixed clocks,
seeded selection, dynamic ports, and real substrates for persistence.

P2's registry is admitted only with human-reviewed exemplars and
counterexamples. P6 uses 10–20 human-annotated requests spanning fixed-target,
partially specified, and target-construction cases, with acceptable contract
sets and expected witness/trace states. These are proposed benchmark cases,
not a completed labeled corpus. A skipped model dependency cannot count
as validation; model-backed tests need a deterministic fixture path and a
non-vacuity assertion. No quality target is inferred from the two grown plans,
one run-722 failure, or the aggregate execution-success counters.

### Performance Expectations

The current run aggregate supplies a latency baseline and its continuation
caveat, not a target-fidelity score. Measure request/target construction and
post-realization assessment separately from retrieval and generation. Record
model-call count, latency, cost, memory used by typed evidence, and the number
of obligations assessed. The one-repair bound belongs to RDR-232; P4 must not
repeat the plan prefix or add an unbounded assessment loop.

Before P6 measurement, record the baseline distribution and the smallest
improvement in human-labeled target fidelity that Sam considers worth the
added cost. P2–P4 proceed under their stated prerequisites so P6 can measure
the result. After P6, keep the target layer only if the lower confidence bound
for the paired target-fidelity delta meets that pre-registered minimum and
measured added cost stays within the agreed budget. Stop or narrow for
futility if the upper bound is
below that minimum or the cost/latency ceiling is exceeded without the gain;
treat any other inconclusive interval as insufficient evidence, not adoption.
Cap evaluation spend and important-stratum label floors before comparing arms.
Do not invent a numeric improvement, confidence level, or cost ceiling from
the current mixed-era census. D4 uses that result; it is not decided by
execution volume.

### Day 2 Operations

Family declarations and contract versions are source-controlled and promoted
through the P2 review workflow. The scope ladder supplies its governance
concept; no current executable plan-promotion command is assumed. Versioned
run summaries use RDR-232's
migration, import/export, readback, and deletion lifecycle. `aspect_validation.jsonl`
uses its separate `--validation-all-out` path and the existing local
validation-output retention lifecycle, with
redaction and deletion covered by tests. No new database table, collection,
service, credential, or independent telemetry write is proposed.

### New Dependencies

None. Reuse the existing runner, operator dispatch, catalog, plan library,
validation-output path, and RDR-232's proposed run-summary capability.

## Finalization Gate

### Contradiction Check

The October census removes the July low-usage rationale but not the observed
prompt-artifact and wrong-shape problems. The current design therefore bases
D1 on those observations and reserves cost/value for P6. Pre-reduction coverage
is an input to, not proof of, post-realization preservation. RDR-232's routing
kinds are not a second durable family registry. No contradiction is known
between the refreshed evidence and the phased design; the independent peer
critique is not a formal gate result.

### Assumption Verification

The research closes the source, census, governance, and deployment-version
questions recorded in T2. It does not claim that unimplemented target binding,
witness normalization, preservation assessment, or live-model assessment
already works. Each phase's Critical Assumptions names the implementation proof
required before that phase ships. RDR-232 is an accepted dependency for
P3/P4; its production integration and live-model verification remain its own
work, not evidence borrowed by this RDR.

#### API Verification

| API or boundary | Owner | Verification |
| --- | --- | --- |
| `operator_check` / `operator_verify` | Python MCP operators | Source search: current request/response shapes verified; unified witness schema is proposed |
| Inline and saved-plan dispatch | `mcp/core.py`, `plans/runner.py` | Source search: dispatch and bundling boundaries verified; non-hidden witness dispatch requires implementation tests |
| Evidence hydration and `PlanResult` | `plans/runner.py` | Source search: current identity/result boundaries verified; typed witness and trace threading are proposed |
| Enrichment validation output | `commands/enrich.py` | Source search: current failure-only retention verified; all-outcomes record is proposed |
| Continuation and redaction envelopes | `mcp/core.py`, RDR-200 | Source search: existing handoff/report paths checked; report payload, resumable fallback, and preservation finalization require implementation tests |
| Plan/run persistence | Java engine handlers/repositories | Source search plus deployed `/version`: current engine 0.1.164; RDR-232 summary and graph capabilities are not assumed deployed |

### Scope Verification

The minimum viable validation is the multi-facet family binding and
post-realization proof under Implementation Plan. It demonstrates host-bound
witnesses, distinct final states for preserved and weakened obligations, and
an envelope that distinguishes pre-reduction coverage from preservation. The
continuation and envelope-failure cases finalize without repeating the plan
prefix. The continuation MVV requires exact output with a deliberately dropped
obligation to become `weakened` or `requiresReview` under host assessment even
when `ok=true`. These proofs are in P1/P3/P4 implementation scope, not deferred
to P6 or replaced by static source inspection.

P0 and P5 need no production target runtime. P0 requires the explicit D1/D2
human decision and the completed October census. P5 requires recovering or
re-registering the ghost roadmap before correction, then verifies content,
source URI, manifest, and catalog identity afterward. P6 is the later
cost/value decision, not a substitute for the phase MVV.

### Cross-Cutting Concerns

- **Versioning**: family, contract, witness schema, envelope, and run-summary
  versions are explicit. New execution of an old family uses generic
  realization plus review; historical evidence is interpreted under its
  immutable original declaration or marked for review.
- **Build tool compatibility**: existing Python and Java/Maven paths. Engine
  migration and graph dependencies belong to RDR-232 and its release process.
- **Licensing**: no new dependency or copied external implementation.
- **Deployment model**: client-side target/preservation logic with existing
  engine persistence; no target execution is attributed to the Java engine.
- **IDE compatibility**: N/A.
- **Incremental adoption**: P1 is independent; P5 first recovers its source;
  P0 controls P2–P4; P3 requires P2 and RDR-232 Phase 2; P4 requires P1/P2/P3
  and RDR-232 Phase 3; P6 follows P4 and decides D4.
- **Secret/credential lifecycle**: existing operator and service credentials;
  no new secret, source credential, or override. Redaction applies to persisted
  logs, telemetry, validation files, and other artifacts; authorized protocol
  payloads retain the content required for reduction and host assessment.
- **Memory management**: typed witnesses carry ids and bounded spans rather
  than duplicate documents; evidence and trace sizes are bounded by the runner
  budgets, with aggregate family/obligation counts measured in P4.

### Proportionality

The design reuses plan execution, structured operators, run records, and the
scope-ladder governance concept. It does not add a second planner, graph
algorithm, telemetry table, or user-facing verb. P1/P5 are independently
useful. P2–P4
are justified only if the P6 target-fidelity measurement meets the stop rule;
otherwise the registry and preservation assessment are abandoned and the
RDR-232 safeguards remain.

## Open Questions

1. ~~**D1 adoption:**~~ **DECIDED (Sam, 2026-10-24): (a), full layer.** P6
   will test whether P2–P4 earn their cost.
2. **D4 cost/value:** deferred until P4/P6 produce target-fidelity and cost
   measurements against a pre-registered stop threshold.
3. ~~Does the paper's §6 benchmark supply automatic promotion?~~ **CLOSED** —
   no human-label-free adequacy signal. Candidate generation plus human
   promotion is the governance model.
4. ~~Could NL2Pipe's Phase A Linker double as target identification?~~
   **CLOSED** — it is referent grounding, subsumed by RDR-147.

## References

- Paper: "Natural Language to What? A Vision for Intermediate
  Representations in NL-to-X Querying" — catalog `1.14.23`,
  `knowledge__semantic-operators__voyage-context-3__v1`, DEVONthink
  `x-devonthink-item://18F066AE-6141-47FE-B310-33848562203B`.
- Sibling paper: "Bridge the Last-Mile Gap to Semantic Analytics" —
  catalog `1.14.10`.
- Historical roadmap: tumbler `1.11.197`, now a title-only ghost record;
  recovery evidence in `nexus/semantic-operator-pipelines-basecamp-2026-06-04`
  and `nexus/ghost-notes-knowledge__knowledge-2026-08-28-titles`.
- Synthesis: T3 `research-nl-to-x-ir-vs-nexus-2026-07-25`
  (`a337b930…`), same collection; secondary evidence for the ghost roadmap.
- Shared realization/evidence contract: RDR-232,
  `nexus_rdr/189-research-6`, and `nexus_rdr/232-research-12`.
- Research completion: `nexus_rdr/189-research-10` through `-16`: canonical
  research registration, current census, deployment evidence, governance,
  current interfaces, roadmap ghost-source verification, and gate-readiness
  validation with its test limitation. Gate-fix evidence: research 17–22.
- Code: `src/nexus/plans/{runner,schema,bundle}.py`,
  `src/nexus/mcp/core.py`, `src/nexus/commands/enrich.py`.

## Revision History

- 2026-07-25 — created (draft) from the NL-to-X synthesis. Recommendation
  4 of that synthesis split across P1 (ungated) and P4 (gated on P2), per
  the phase-derivation note above.
- 2026-07-25 — **R4 corrected.** The first draft claimed the paper's
  curated-families position and RDR-084's auto-growth were "not
  reconcilable," and made that a BLOCKING decision. Wrong: RDR-084's
  quoted line summarizes one tier. The implementation
  (`core.py:5761-5797`) bounds auto-growth to `scope=personal` with a
  30-day TTL and routed project/global scope through the then-current human
  `/conexus:plan-promote` gate. That command was retired in August 2026; the
  P2 workflow replaces this historical reference. Curated and auto-grown
  artifacts coexist, and the scope-ladder governance concept is reusable. D2,
  the Trade-offs governance bullet, the P0 gate, the Context bullet, and the
  status note were all rewritten. The residual question narrowed from "which
  governance model" to "what promotion signal detects a silently
  under-specified family."
- 2026-07-25 — **Historical research R5, R6, R7.** These are dated
  snapshots, not current deployment claims. R5 is qualified above: target
  logic is client-side but integration has engine dependencies. R6 remains
  the basis for rejecting automatic family promotion. R7's Linker boundary
  stands. Current `AnswerShape` and closed beads are accounted for in the
  revised findings rather than treated as absent or open.
- 2026-07-25 — **R8 added; D1 answered with a recommendation.** Live-store
  measurement: 17 plans (15 builtin, 2 grown), hygiene clean, reuse ~0
  (last use 2026-05-27). RDR-179's R28/R29 found partly stale — `list`
  and `hygiene` route live via `_open_plan_library()`; the raw-SQLite
  `_open_plans_db()` half stands. Both grown plans embed multi-slot
  answer contracts in `generate` template strings (the paper's
  prompt-artifact degradation, n=2 of 2). Gap 2 refined:
  `operator_extract(fields=…)` is an existing ad-hoc slot declaration P2
  should build on. Recommendation: D1=(a) sequenced behind RDR-179;
  `depends_on: [RDR-179]` added to frontmatter.
- 2026-07-25 — **live probe corrected R8's dormancy reading.** One
  `nx_answer` research call matched plan 14, cleared the scope filter and
  0.40 gate, ran, and bumped `match_count` 136→137 / `use_count` 1→2 /
  `success` 1→2 first try. The reuse loop is sound; the April/May
  timestamps mark absent callers, not failed machinery. The provisional
  `depends_on: [RDR-179]` was therefore REMOVED — RDR-179 item 1's
  split-brain premise does not hold against the live store (its items 2,
  3, 4 are untouched by this probe). The probe additionally showed a
  zero-evidence run recording `success=True` (`core.py:5659`; the
  single-step reroute at `:5579` does the same on a `"No results."`
  branch) — success/failure is an exception counter, not an outcome
  counter. Filed `nexus-yg49g` (promotion-signal integrity) and
  `nexus-sbl4m` (grown-plan 30-day TTL blocks compounding); both now in
  `related_issues`.
- 2026-10-24 — **Shared contract with RDR-232 added.** RDR-189 owns durable
  target families, target contracts, normalized witnesses, and preservation
  semantics; RDR-232 owns deterministic-first request analysis, eligibility,
  evidence containment, and bounded repair. D3 is no longer a plan-dimension
  option: routing capability is versioned `plan_json` metadata derived from a
  target family. P2 consumes observed run/coverage shapes with human
  promotion; P3 uses `RequestCoverage`; P4 includes RDR-232's evidence-
  sufficiency ledger. Facets and treatment constraints are distinct. Phase
  sequencing and the P1 fallback are explicit. Research: `nexus_rdr/189-research-6`.
  Both documents remain draft and ungated.
- 2026-10-24 — **Substantive critique corrections (research 7).** Separated
  `RequestCoverage` from a family/version/environment-bound `TargetContract`;
  made the phase DAG explicit; required host-bound witness identity through
  hydration and both dispatch paths; distinguished pre-reduction evidence
  coverage from post-realization preservation, including continuation and
  failure states. P2 now requires reviewed exemplars/counterexamples rather
  than a fixed family quota. Historical measurements and closed prerequisite
  beads are labeled accordingly. RDR-232's shared references updated to the
  same boundary. Critique: `nexus_rdr/189-critique-shared-contract-2026-10-24`.
- 2026-10-24 — **Confirmation and follow-ups (research 8–9).** Critic confirmed
  all four blockers closed. Added an explicit all-outcomes
  `aspect_validation.jsonl` destination and a cross-path fallback matrix for
  family ambiguity/version/capability, old envelopes, persistence, redaction,
  and continuation. Addendum:
  `nexus_rdr/189-critique-shared-contract-2026-10-24-addendum-1`.
- 2026-10-24 — **Gate-fix research (17–21).** Continuation uses host assessment
  of exact output with identity/digest/evidence validation; `ok=true` is not
  preservation. Protocol payloads and persisted artifacts have separate
  redaction rules. Family pins use two validated `bindings` keys outside
  `dimensions`. Historical outcome defects and current `AnswerShape` behavior
  are distinguished from current source. Evidence: `nexus_rdr/189-research-17`
  through `-21`. No runtime implementation is claimed.
- 2026-10-24 — **Gate-readiness research completed (research 10–16).**
  Canonicalized legacy research records without upgrading their evidence;
  Layer 2 now sees the complete sequence. Refreshed D1 with 15 visible plans,
  351 post-July run rows, two current grown prompt artifacts, and run 722;
  removed the low-usage premise and mixed-era cost-baseline claim. Verified
  deployed engine 0.1.164 and the 0.1.57 prerequisite-fix ancestry. Replaced
  the retired plan-promotion command with an explicit family-review workflow.
  Verified current witness/hydration/continuation defects, including verify's
  missing evidence argument and whole-plan continuation rerun. Identified
  roadmap 1.11.197 as a ghost record and bounded P5 to source recovery before
  correction. Added critical assumptions, current API boundaries, alternatives,
  test/validation strategy, MVV, and Finalization Gate responses. Research is
  complete; proposed runtime behavior and D4 value remain unproven. No gate,
  acceptance, or implementation performed.
- 2026-10-24: Gate round 1 — BLOCKED (1 Critical, 4 Significant, 1 ship-blocker(s)); commit `6aa585629`; critique `nexus_rdr/189-gate-critique-2026-10-24-r1`.
- 2026-10-24: Gate round 2 — PASSED (0 Critical, 2 Significant, 0 ship-blocker(s)); commit `1d9b72968`; critique `nexus_rdr/189-gate-critique-2026-10-24-r2`.
- 2026-10-24 — **Fix-check repair (research 22).** Separated legacy and
  all-outcomes validation paths; defined the enrichment trace flag and default;
  made unresolved version, contract-version, and continuation evidence-id
  fields explicit; separated old-request routing from historical interpretation;
  added pending, quote/span fidelity, and negative tests; corrected the P4/P6
  keep/stop ordering. This records design repair, not runtime verification.
- 2026-10-25: Amended to match accepted RDR-232 (nexus-f1evv, Sam's
  decision): the shared contract is accepted; scope before the engine graph
  cap is a gated RDR-232 Phase 1 step; partial coverage returns prose behind
  the disclosed-gaps notice, counts as success, blocks growth with its own
  flag and stays outside `NON_ANSWER_SHAPES`; only `unavailable` is a failure.
  Status unchanged.
