---
title: "Target-Definition Layer for NL-to-X Querying: Answer-Object Families, Target Contracts, and Witness Obligations over the Existing Plan/Operator Realization Layer"
id: RDR-189
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: unreviewed
created: 2026-07-25
related_issues: ["nexus-sbl4m", "nexus-yg49g"]
related: [RDR-078, RDR-079, RDR-080, RDR-084, RDR-088, RDR-089, RDR-090, RDR-093, RDR-134, RDR-147, RDR-179]
---

# RDR-189: Target-Definition Layer for NL-to-X Querying

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Status note**: this RDR is `draft`. D1 (does nexus want this layer at
all) awaits Hal; R8 recommends yes, with P2 gated on having a corpus of
observed answer shapes (`nexus-sbl4m`) and any success-derived promotion
signal gated on `nexus-yg49g`. P1 and P5 are unblocked. Do not quote as
design intent until gated.

## Problem Statement

The source paper, *"Natural Language to What? A Vision for Intermediate
Representations in NL-to-X Querying"* (catalog tumbler `1.14.23`,
`knowledge__semantic-operators`), splits NL querying into two layers:

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

Nexus has a mature realization layer and **no target-definition layer**.
This is not a partial gap: the concept and its vocabulary are absent.

#### Gap 1: No target identification anywhere in the answer path

`_nx_answer_plan_miss` (`src/nexus/mcp/core.py:5090`) decomposes a
question straight into a step DAG over a fixed tool vocabulary. No step,
prompt fragment, or gate asks what *kind* of answer object the question
requires. The target is not supplied by the caller either; it is simply
never modeled. Nexus answers "how do I get this?" well and has no
first-class answer to "what, structurally, am I trying to produce?"

#### Gap 2: No answer-object family, so every plan terminates in prose

`PlanResult` (`src/nexus/plans/runner.py:212`) is
`{steps: list[dict], final: dict | None}`, an untyped bag. There is no
registry of named, versioned answer shapes (the paper's `claim-conflict`,
`evidence bundle`, `comparison object`, `issue-evolution track` examples)
with declared slots and constraints. Plans terminate in whatever the last
step emits: in practice free text from `operator_generate` /
`operator_summarize` (neither has a shape parameter), or a raw
id/tumbler/distance list from a retrieval tool.

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
3. **The one production caller keeps the witness only on failure.**
   `nx enrich aspects --validate-sample N`
   (`src/nexus/commands/enrich.py:1438+`) runs `operator_verify` as a
   hallucination check. On `verified=True` it increments a counter and
   `continue`s (`enrich.py:1524-1526`); citations discarded. Only the
   failure path persists them (`enrich.py:1534`). The paper's preservation
   trace is precisely the record of obligations that *survived*; nexus
   throws away exactly that half.

#### Gap 4: Plan dimensions route retrieval, not answer shape

`_REQUIRED_DIMENSIONS = ("verb", "scope")`
(`src/nexus/plans/schema.py:94`): `verb` selects a retrieval strategy
family, `scope` selects a corpus. Neither encodes what kind of object the
answer should be. The paper's Γ(E) is about which answer objects an
environment can support: a different axis entirely.

## Context

- **Nexus's own roadmap doc is weaker than this paper and partly stale.**
  `1.11.197` ("Semantic Operator Pipelines: Nexus Roadmap & Basecamp",
  2026-06-04) targets the *sibling* paper NL2Pipe (`1.14.10`). Its row R6
  ("Abstract plan IR") frames the concern as backend portability and marks
  it "low urgency while single-backend." That reasoning does not apply
  here: knowing what you are trying to produce matters with exactly one
  backend. Adopting R6 as scoped would still miss this paper.
- **R6's prior-art citation is wrong and should not be read as a
  precedent.** R6 cites "RDR-079 abandoned" as evidence a nexus abstract
  IR was tried and failed. RDR-079 was abandoned for a concurrency
  defect (a synchronous `subprocess.run` blocking the asyncio event loop),
  not on the merits of an IR design
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
  RDR-084 shipped the plan library's growth policy, which, on inspection
  (R4), is a *three-tier ladder* that already reconciles auto-growth with
  curation and is the mechanism this layer should reuse, not fight.

## Desired End State

Deliberately stated as a target, not a commitment; Open Question 1 may
resolve to "no."

1. A question entering `nx_answer` resolves an **answer-object kind**
   before a plan is selected or generated, from a small registry of named,
   versioned families.
2. The chosen family declares its obligations: required slots, and which
   claims require a witness.
3. The plan runner is checked against that declaration, not merely against
   structural well-formedness.
4. `PlanResult` carries a **preservation trace**: which declared
   obligations were met, which were not, each anchored on `(tumbler,
   chash)` where the evidence came from the corpus.
5. `operator_check` / `operator_verify` speak one evidence shape, are
   reachable from every planning path, and their surviving evidence is
   retained rather than discarded.

## Research Findings

Findings below marked **[verified]** were confirmed by opening the cited
file at the cited line during drafting. Everything else is a claim to be
tested during `/conexus:rdr-research`.

### R1: The absence is total, not partial **[verified]**

`grep -rniE "target.contract|answer.object|answer.family|preservation.trace|witness.obligation" src/ docs/rdr/`
returns zero hits. There is no partial implementation to extend and no
prior vocabulary to reconcile with.

### R2: The realization layer is genuinely mature **[verified]**

`plan_run` (`runner.py`) executes a JSON step DAG with `$stepN.field`
inter-step references, scope injection, and operator bundling
(`bundle.py:97`). `_OPERATOR_TOOL_MAP` (`runner.py:690-691`) routes
`check`/`verify` when a *saved* plan names them. This RDR proposes a layer
above this machinery, not a replacement for it.

### R3: Witness primitives are structurally sound but mis-wired **[verified]**

Both operators dispatch through `claude_dispatch(prompt, schema)` with a
JSON schema, so the structured-output machinery needed for declared
obligations already exists. The defects are wiring, not capability: see
Gap 3 (1)(2)(3), each verified at the cited line.

### R4: Curated and auto-grown already coexist; RDR-084 is the precedent, not the obstacle **[verified]**

The paper argues answer-object families must be curated, versioned system
artifacts: *"If every query produces a new handcrafted template, target
contracts become prompt artifacts rather than system artifacts."*
`docs/rdr/rdr-084-plan-library-growth.md:33` reads as the shipped
opposite: the plan library compounds "without any human curation step."

**That sentence is a summary of one tier, not the whole policy.** The
implementation (`src/nexus/mcp/core.py:5761-5797`) auto-saves a
successful ad-hoc plan with `scope=personal` and a config-driven TTL
(`plans.ad_hoc_ttl`, 30-day default, `config.py:677`). Its own comment is
explicit: *"scope=personal keeps growth isolated to the caller (the
project/global scopes are reached only via `/conexus:plan-promote`)."*
The library therefore already runs three tiers:

| Tier | Growth | Lifetime | Gate |
|---|---|---|---|
| 14 seeded templates (`conexus/plans/*.yml`) | hand-authored | permanent | authoring |
| `scope=personal` grown plans | automatic | 30-day TTL | none |
| `scope=project` / `global` | promoted | permanent | `/conexus:plan-promote` (human) |

Auto-growth is real but **bounded**: ephemeral, caller-isolated, and
unable to bind anyone else's queries. Curation is what converts a plan
into a shared, permanent artifact. This is exactly the paper's
governance position applied to the realization layer; nexus arrived at
it independently. The framing in this RDR's first draft ("not
reconcilable") was wrong.

**What does not transfer, and is the real question.** RDR-084's
promotion signal is cheap because a plan is *empirically falsifiable per
run*: it executed without error and produced an answer, so "it worked" is
observable. An answer-object family is a **type declaration whose defects
are silent**: a family missing a required slot still executes fine and
merely produces under-specified answers forever. "It ran" cannot be the
promotion criterion. See Open Question 1.

### R5: The engine carries no IR concept; the layer is client-side **[verified 2026-07-25]**

*(Closed. Was "unchecked, not established as absent" in the first draft.)*

- **Vocabulary absent.** `grep -rniE
  "target.?contract|answer.?object|answer.?family|preservation.?trace|witness.?obligation|environment.?profile"
  service/src` returns zero hits across 83 Java files.
- **`PlanHandler.java` is CRUD, not execution.** Its route table is
  `/save /get /delete /disable /enable /set_scope_tags /list_active
  /search /list /exists /import /import_batch` plus three metrics routes,
  all delegating to `PlanRepository`. It stores `plan_json` as an opaque
  blob and never interprets it.
- **No execution engine-side.** `grep -rniE
  "plan_?run|execute.*plan|operator_?(extract|filter|rank|check|verify|generate)|claude_?dispatch"
  service/src/main/java` returns zero hits.

**Conclusion**: the engine is a persistence + retrieval substrate for the
plan library. All plan interpretation, operator dispatch, and any future
target-definition logic is client-side. P2-P4 are client-only work.

**But P0 must weigh one consequence.** The engine *does* own the
`nx_answer_runs` telemetry surface: `/metrics/match`,
`/metrics/run_start`, `/metrics/run_outcome`. If D2 resolves to option
(c) (automatic promotion on a measurable adequacy signal), that signal
has to be persisted and queried through those engine routes: a
**second-lifecycle change** (engine-service tag + `REQUIRED_ENGINE_VERSION`
bump), not a client-only edit. Option (c) is therefore materially more
expensive than (a) or (b), and any P0 costing that assumes client-only
scope for D2(c) is wrong.

### R6: The paper's §6 benchmark supplies no automatic adequacy signal **[verified 2026-07-25]**

*(Closes Open Question 2.)*

§6.5 "Direction 5: Target-Fidelity Benchmarks" is a research agenda, not a
metric suite. A benchmark instance is query + Γ(E) + available families +
acceptable contract set + expected witnesses/provenance/trace-status,
scored on three subtasks: regime-identification correctness, trace
fidelity (does ρ record preserved/approximated/weakened/requiresReview or
silently upgrade a degradation), and answer-object correctness. Proposed
scale is 10-20 human-annotated queries; the paper calls scaling past that
"a hard open problem."

**None of it is computable from nexus telemetry today.**
`_nx_answer_record_run` (`src/nexus/mcp/core.py:4961-4991`) persists
exactly `question, plan_id, matched_confidence, step_count, final_text,
cost_usd, duration_ms`; `_nx_answer_record_outcome` (`core.py:4994-5009`)
is a binary success/failure counter on a matched plan. No field backs any
of the three subtasks, and they cannot be added incrementally; each
presupposes P2 (families) or P4 (traces) already existing.

**Decisive negative for D2.** §6 supplies **no** human-label-free
adequacy signal. Every benchmark input is asserted as human-curated, and
the paper's own validity criterion is correlation with *expert* judgment.
The one self-computable candidate (`fit()` obligation status) is
circular: the system grading its own proposed contract against its own
environment profile, with no independent check. Combined with R5's
finding that D2(c) would also be a second-lifecycle change, **(c) is now
the expensive option with no known signal to justify it.**

### R7: NL2Pipe's Phase A Linker is grounding, not target identification **[verified 2026-07-25]**

*(Closes Open Question 3.)*

The Linker (paper `1.14.10`) classifies question phrases as
matched/topical/absent against the data, then discovers "bridge entities"
needed to join across sources, LLM-verifying they exist. Its output feeds
the Planner as grounded referents that become filter predicates and join
keys. The paper's own ablation, "Linker and Planner are coupled, not
additive," confirms grounding is its whole job.

By this RDR's own Gap 4 distinction it sits on the **referent** axis
(which concrete things does this query mention), never the
**answer-shape** axis (what kind of object should exist). NL2Pipe has no
analogue of τ at all; its target is implicitly whatever the σ→⋈→γ
pipeline emits. P3 would get nothing from it.

**Already covered, and on record.** The roadmap doc `1.11.197` §4 row R1
already states: *"Query Linker: pre-grounding before inline planning...
Covered. RDR-147's ingest-time entity-linker + type-mismatch trigger
(Gaps 1,3) is NL2Pipe's Linker."* RDR-147 (`accepted`, world-blocked,
resume bead `nexus-3lu23`) subsumes it. OQ3 resolves to **no new work
(cross-reference RDR-147)**; P3 needs a genuinely different mechanism
(answer-object-family classification) that nothing in NL2Pipe attempts.

### R8: D1 evidence: the pain is real but the layer below it is dormant **[verified 2026-07-25]**

*(Bears directly on D1. Live-store measurements, 2026-07-25.)*

**The live plan library is clean and very small.** `nx plan list` routes
through `_open_plan_library()` (`src/nexus/commands/plan.py:434`), the
service-backed path, **not** the frozen SQLite snapshot. (RDR-179's R28
"all 12 `nx plan` sites hardcode local SQLite" and R29 "79% pollution" are
**stale for `list`/`hygiene`**: `nx plan hygiene` now reports *"Plan
library clean: no bead-dumps, null-verb, or always-failing plans."* Other
`nx plan` verbs still use the raw-SQLite `_open_plans_db()` at
`plan.py:71`; that half of R28 stands.)

Live contents: **17 plans: 15 builtin seeds, 2 auto-grown** (ids 353,
354, both `scope=personal`).

**Reuse is near-dormant.** Every plan shows `use=0` except three at
`use=1`, most recent `2026-05-27`, roughly two months stale. RDR-084
shipped auto-growth and it has produced **two** plans. *Caveat*: RDR-179
item 3 flags that `use_count` disagrees with run records, so treat these
as directional, not exact.

**But the paper's failure mode is already observable in-tree.** Both
grown plans hand-roll answer structure into a `generate` template string:

- Plan **354** terminates in `generate(template="Structured comparison
  report: (1) core mechanism of each approach, (2) shared goals, (3)
  divergent design decisions, (4) tradeoffs (latency, cost, correctness,
  staleness), (5) when to prefer each")`, a five-slot answer-object
  contract, expressed as prose inside a prompt.
- Plan **353** does the same at lower resolution ("Answer two things
  precisely: (1)... (2)...").

That is exactly *"target contracts become prompt artifacts rather than
system artifacts"*, observed, not hypothesized. **n=2**, which is small,
but it is the entire grown population: 2 of 2.

**Refinement to Gap 2.** `operator_generate`/`operator_summarize` still
have no shape parameter (Gap 2 stands). However `operator_extract(fields=…)`
*is* an existing ad-hoc slot declaration, used in both grown and seeded
plans, e.g. `fields: decision,rationale,rdr`
(`conexus/plans/builtin/debug-default.yml` family),
`fields: verb,scope,strategy,object,domain`, and plan 353's
`client_package_repo_owner,edge_auth_mechanism,token_type,tenant_data_path_flow`.
P2 should build on `extract`'s field list as the seed of a declared slot
set rather than starting from zero.

**Live probe: the reuse machinery is NOT broken.** A single
`nx_answer(dimensions={"verb":"research"}, scope="knowledge__semantic-operators")`
call on 2026-07-25 matched builtin plan 14, passed the scope filter and
the 0.40 confidence gate, was selected, executed, and incremented its
counters: `match_count` 136→137, `use_count` 1→2, `success` 1→2,
`last_used` → `2026-07-25T04:34:51Z`. **First try, post-cutover, end to
end.** The dormancy above is therefore an absence of *callers*, not a
broken loop; the April/May timestamps mark the last time someone drove
it, nothing more. Any reading of this RDR that treats plan reuse as
failed machinery is wrong.

**The probe also exposed a defect that sharpens D2.** That run returned
*"The plan's retrieval steps returned zero results"* and was still
recorded `success=True`. `_nx_answer_record_outcome(..., success=True)`
(`core.py:5659`) is the fall-through after execution; `success=False`
happens only on a raised exception (`core.py:5654`, `:5598`), and the
single-step reroute records success unconditionally on a branch where the
result can be the literal string `"No results."` (`core.py:5563,5579`).
Success/failure is an **exception counter, not an outcome counter**.
Filed as `nexus-yg49g`.

**D1 reading.** The degradation the paper predicts is real here (2/2
grown plans embed contracts in prompt strings), the machinery below is
sound, and the calling volume is low. **Recommendation: D1 = yes in
principle.** Two sequencing caveats, both narrower than the first draft
of this finding claimed:

- D2(b)'s "auto-propose from observed shapes" needs shapes to observe.
  Today that is n=2, and `nexus-sbl4m` shows the grown tier cannot
  accumulate past a 30-day TTL without manual promotion. P2 should not
  start until there is a corpus of observed shapes to derive families
  from.
- `nexus-yg49g` must be fixed before *any* success-derived promotion
  signal is trusted; a plan that reliably retrieves nothing currently
  accrues a perfect success record.

P1 and P5 remain unaffected and can ship now.

## Decision (draft: options to resolve in research)

**D1. Does nexus adopt a target-definition layer at all?**
Options: (a) full layer per Desired End State; (b) evidence-shape hygiene
only (P1) and stop; (c) no. **R8 recommends (a).** The degradation is
real (2/2 grown plans embed answer contracts in prompt strings) and a
live probe confirmed the reuse machinery works end to end, so the
sequencing constraint is narrower than "relight RDR-179": P2 needs a
corpus of observed shapes to derive families from (`nexus-sbl4m`), and
any success-derived promotion signal needs `nexus-yg49g` fixed first.
P1/P5 are unaffected.

**D2. What is the promotion criterion for a family?** Not
curated-vs-auto-grown: R4 establishes those already coexist, and the
`personal → project → global` ladder plus `/conexus:plan-promote` is the
mechanism to reuse. The open part is the *signal*, since "it ran without
error" (RDR-084's criterion) cannot detect a silently under-specified
family. Options: (a) human-only promotion, ladder reused unchanged;
(b) automatic proposal from observed answer shapes in `nx_answer_runs`,
human promotion; (c) automatic promotion on a measurable adequacy signal.
**R6 largely settles this against (c)**: the paper's own benchmark supplies
no human-label-free signal, and R5 shows (c) would additionally be a
second-lifecycle engine change. Choosing (c) now means inventing a signal
the source literature does not have and paying an engine tag for it.
Recommend (b) unless P0 surfaces a signal R6 missed.

**D3. Where does target identification live?** Options: (a) a step inside
`_nx_answer_plan_miss` before decomposition; (b) a dimension alongside
`verb`/`scope` in `_REQUIRED_DIMENSIONS`; (c) a separate pre-plan MCP
surface.

**D4. Is adequacy-checking (regime diagnosis) worth its cost**, given
`plan_match`'s cosine-similarity reuse gate already does something
directionally similar for free?

## Approach (phased, draft)

**Note on phase derivation**: the synthesis recommendation "extend
`operator_check`/`verify` into a declared witness mechanism" is split
across **P1 and P4**, because its two halves have different gating. The
evidence-shape work stands alone and pays off whether or not this RDR
proceeds; the declared-obligation work is meaningless without a family
registry to declare against. Splitting avoids touching the same RDR-088
contract twice.

### P0: Decision gate (no code)

Resolve D1-D4. (R5 closed 2026-07-25: engine surface is clear; note its
D2(c) second-lifecycle consequence when costing.) D1 is decisive: if
it resolves to (b) or (c), P1 and P5 still ship and P2-P4 are dropped.
D2 no longer blocks (R4): the promotion ladder exists; only its signal
is open, and that can be settled inside P2.

### P1: Evidence-shape unification (NOT gated on P0's outcome)

- Align `operator_verify`'s `citations: list[str]` to the structured
  evidence-item shape `operator_check` already uses
  (`_CHECK_EVIDENCE_ITEM_SCHEMA`, `core.py:4061`), anchored on
  `(tumbler, chash)` where the input carries them.
- Add `check` / `verify` to `_ALLOWED_TOOLS` (`core.py:5192`) so the
  inline planner can emit witness steps.
- Retain surviving evidence in `nx enrich --validate-sample`'s
  `verified=True` path (`enrich.py:1524-1526`).
- **Requires an RDR-088 amendment**: this is a return-contract change to a
  shipped surface with a live CLI consumer (`enrich.py:1533-1534`) and
  documented shapes in `docs/plan-centric-retrieval.md:265`,
  `docs/mcp-servers.md:78`, `docs/cli-reference.md:457`. Not a quiet edit.

### P2: Answer-object family registry (gated on D1=a, shape by D2)

Minimal registry: 3-4 families derived from actual `nx_answer_runs`
telemetry, not invented. Named, versioned, with declared slots.

### P3: Target identification in the answer path (gated on D1=a, D3)

A target-identification step ahead of plan selection/generation, emitting
a target contract as an artifact distinct from the realization plan.

### P4: Declared witness obligations + preservation trace (gated on P2)

Per-family W declarations auto-dispatched through the P1-normalized
operators; results accumulated into a preservation trace on `PlanResult`
(`runner.py:212`). This is the second half of the synthesis's
recommendation 4.

### P5: Roadmap-doc correction (independent, trivial)

Correct `1.11.197`'s R6: replace the "RDR-079 abandoned" prior-art
citation with an accurate note, and re-scope "low urgency while
single-backend."

### P6: Target-fidelity benchmark (candidate, gated on P2 + P4)

Per R6: the paper's §6 benchmark scores regime identification, trace
fidelity, and answer-object correctness, none of which have anything to
score until families (P2) and preservation traces (P4) exist. Scoped here
rather than in RDR-090 (different paper, different axis, parked). Expect
10-20 human-annotated queries and no automatic scaling path; if that cost
is unacceptable, this phase is the first to drop.

## Alternatives Considered

- **Fold into RDR-147 / RDR-090 / RDR-134**, as `1.11.197` plans to fold
  NL2Pipe's ideas. Rejected as the default: those RDRs address entity
  grounding, benchmarking, and taxonomy-aware recall, all realization-layer
  concerns. Filing a layer-above concern inside them repeats the structural
  misfiling R6 already shows.
- **Adopt R6 as scoped** (abstract plan IR for backend portability).
  Insufficient: orthogonal to this paper's argument, which holds at one
  backend.
- **Do nothing.** Defensible if D4 resolves against adequacy-checking and
  the free-text default is judged good enough. P1 and P5 should ship
  regardless.

## Trade-offs

- **Cost**: a target-definition step adds an LLM round-trip ahead of
  planning, on a path where `nx_answer` latency is already poor (23% of
  runs 2-5min per the tool's own docstring). D4 must weigh this honestly.
- **Governance**: cheaper than the first draft assumed. The
  `personal → project → global` ladder already reconciles auto-growth with
  curation (R4) and can be reused rather than rebuilt. The residual cost
  is the promotion signal: a family's defects are silent, so either a
  human stays in the loop or an adequacy signal must be invented and
  justified against D4.
- **Blast radius**: P1 touches a shipped RDR-088 contract with a live
  consumer and three documented surfaces.

## Open Questions

1. **What promotes a family?** *(Downgraded from BLOCKING after R4:
   this no longer reverses RDR-084 and no longer needs a decision before
   any work can start.)* Curated and auto-grown already coexist in the
   plan library via the `personal → project → global` ladder; the
   mechanism transfers. What does not transfer is the promotion signal:
   RDR-084 promotes on observed success, which works because a plan is
   falsifiable per run, and does not work for a type declaration whose
   defects are silent. Either name a family-adequacy signal (and defend
   it against D4's cost objection) or accept human-only promotion and say
   so deliberately.
2. ~~Does the paper's §6 benchmark say anything to `nx_answer_runs`
   telemetry, and does it belong in RDR-090?~~ **CLOSED 2026-07-25: see
   R6.** Nothing computable today; supplies no automatic adequacy signal;
   belongs here, not in RDR-090 (different paper, different axis: NDCG@3
   retrieval quality; parked). Filed as P6 below.
3. ~~Could NL2Pipe's Phase A Linker double as target-identification
   evidence?~~ **CLOSED 2026-07-25: see R7.** No: it is referent
   grounding, and RDR-147 already subsumes it. No new work; cross-reference
   RDR-147.
4. ~~Does `service/` carry parallel IR concepts?~~ **CLOSED 2026-07-25:
   see R5.** It does not; the layer is client-side. Consequence for D2(c)
   recorded in R5.

## References

- Paper: "Natural Language to What? A Vision for Intermediate
  Representations in NL-to-X Querying," catalog `1.14.23`,
  `knowledge__semantic-operators__voyage-context-3__v1`, DEVONthink
  `x-devonthink-item://18F066AE-6141-47FE-B310-33848562203B`.
- Sibling paper: "Bridge the Last-Mile Gap to Semantic Analytics,"
  catalog `1.14.10`.
- Nexus roadmap doc: `1.11.197` (2026-06-04, NL2Pipe alignment).
- Synthesis: T3 `research-nl-to-x-ir-vs-nexus-2026-07-25`
  (`a337b930…`), same collection.
- Code: `src/nexus/plans/{runner,schema,bundle}.py`,
  `src/nexus/mcp/core.py`, `src/nexus/commands/enrich.py`.

## Revision History

- 2026-07-25: created (draft) from the NL-to-X synthesis. Recommendation
  4 of that synthesis split across P1 (ungated) and P4 (gated on P2), per
  the phase-derivation note above.
- 2026-07-25: **R4 corrected.** The first draft claimed the paper's
  curated-families position and RDR-084's auto-growth were "not
  reconcilable," and made that a BLOCKING decision. Wrong: RDR-084's
  quoted line summarizes one tier. The implementation
  (`core.py:5761-5797`) bounds auto-growth to `scope=personal` with a
  30-day TTL and routes project/global scope through the human
  `/conexus:plan-promote` gate; curated and auto-grown already coexist,
  and the ladder is reusable. D2, Open Question 1, the Trade-offs
  governance bullet, the P0 gate, the Context bullet, and the status note
  were all rewritten. The residual question narrowed from "which
  governance model" to "what promotion signal detects a silently
  under-specified family."
- 2026-07-25: **research pass: R5, R6, R7 added; OQ2/OQ3/OQ4 closed.**
  R5: the Java engine has no IR concept: `PlanHandler` is CRUD over an
  opaque `plan_json` blob, no execution engine-side, so P2-P4 are
  client-only; but the engine owns the `nx_answer_runs` metrics routes,
  making D2(c) a second-lifecycle change. R6: §6's benchmark is a research
  agenda, computable from none of the seven fields
  `_nx_answer_record_run` persists, and supplies no human-label-free
  adequacy signal; filed as new phase P6, scoped here not in RDR-090.
  R7: NL2Pipe's Phase A Linker is referent grounding, subsumed by
  RDR-147; no new work. Net: D2 recommendation firmed to (b); every open
  question except D1 and the D4 cost judgment is now closed.
- 2026-07-25: **R8 added; D1 answered with a recommendation.** Live-store
  measurement: 17 plans (15 builtin, 2 grown), hygiene clean, reuse ~0
  (last use 2026-05-27). RDR-179's R28/R29 found partly stale: `list`
  and `hygiene` route live via `_open_plan_library()`; the raw-SQLite
  `_open_plans_db()` half stands. Both grown plans embed multi-slot
  answer contracts in `generate` template strings (the paper's
  prompt-artifact degradation, n=2 of 2). Gap 2 refined:
  `operator_extract(fields=…)` is an existing ad-hoc slot declaration P2
  should build on. Recommendation: D1=(a) sequenced behind RDR-179;
  `depends_on: [RDR-179]` added to frontmatter.
- 2026-07-25: **live probe corrected R8's dormancy reading.** One
  `nx_answer` research call matched plan 14, cleared the scope filter and
  0.40 gate, ran, and bumped `match_count` 136→137 / `use_count` 1→2 /
  `success` 1→2 first try. The reuse loop is sound; the April/May
  timestamps mark absent callers, not failed machinery. The provisional
  `depends_on: [RDR-179]` was therefore REMOVED: RDR-179 item 1's
  split-brain premise does not hold against the live store (its items 2,
  3, 4 are untouched by this probe). The probe additionally showed a
  zero-evidence run recording `success=True` (`core.py:5659`; the
  single-step reroute at `:5579` does the same on a `"No results."`
  branch); success/failure is an exception counter, not an outcome
  counter. Filed `nexus-yg49g` (promotion-signal integrity) and
  `nexus-sbl4m` (grown-plan 30-day TTL blocks compounding); both now in
  `related_issues`.
