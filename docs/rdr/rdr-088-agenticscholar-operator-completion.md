---
title: "RDR-088: AgenticScholar Operator-Set Completion"
id: RDR-088
type: Feature
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-17
accepted_date: 2026-04-24
related_issues: []
related_tests: [test_operators.py, test_plan_matcher.py]
related: [RDR-042, RDR-078, RDR-079, RDR-080, RDR-092]
---

# RDR-088: AgenticScholar Operator-Set Completion

The AgenticScholar retrospective (persisted to `knowledge__nexus`, 2026-04-17)
identified four concrete operator-layer gaps between Nexus and the paper
we built against. Three are missing paper operators (`Check`, `Verify`,
`Filter`); the fourth was a calibration gap in `plan_match`'s 0.40–0.65
ambiguous zone. RDR-092 (closed 2026-04-23) shipped two mitigations
for that zone — per-call `min_confidence` override and hybrid
`match_text` embedding — so Gap 4's scope has narrowed from "global
precision fix" to "optional last-mile LLM rerank for high-stakes verb
skills". Phases 1–2 of the implementation plan (operators) are still
load-bearing; Phase 3 (rerank) is now gated on a spike that measures
whether rerank adds meaningful signal *after* RDR-092's improvements.

The three operators are small: two–three days each, no new dependencies,
no persistent storage. Grouping them in one RDR produces a coherent
narrative — each closes a specific paper capability and together they
unblock multi-hop query classes we currently cannot express.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: `operator_check` (cross-doc consistency) missing

Paper §D.2, Query 16 defines `Check(items, check_instruction) → {ok, evidence}`
— validates a claim's consistency across peer documents and returns a
structured boolean plus grounding evidence. Nexus's nearest analog is
`operator_compare` (`src/nexus/mcp/core.py:1453`) which returns free-text
comparison — not a structured verdict usable as a plan step's input.

**Impact**: the paper's flagship "do papers X and Y agree on baseline Z?"
integrity-checking pipeline (Traverse → Check → Filter) cannot be
expressed as a plan because Check has no composable output shape.

#### Gap 2: `operator_verify` (single-claim verification) missing

Paper §D.2 defines `Verify(claim, evidence_source) → {verified: bool, reason, citations}`.
Single-claim targeted variant of Check. No current Nexus analog.

**Impact**: agents cannot ask "is this claim grounded in the cited source?"
as a discrete operator step; verification must be folded into ad-hoc
`operator_summarize` or `operator_compare` calls, losing composability.

#### Gap 3: `operator_filter` (composable predicate) missing

Paper §D.4 defines `Filter(items, criterion) → items`. The key property:
`items` is a *prior step's output*, not a raw collection. ChromaDB's
`where=` handles metadata predicates at the retrieval stage but cannot
filter over arbitrary prior-step results. Without `operator_filter`,
multi-step plans that narrow intermediate results must synthesize via
free-text `operator_rank` or `operator_extract` passes — losing
precision and making plan DAGs harder to reason about.

**Impact**: Q4-class compositional queries (search → extract → filter → rank)
break at step 3.

### Deferred from this RDR's scope

Two paper operators from §D.4 are identified as gaps but explicitly
deferred from implementation in this RDR so the scope stays tight
around the three load-bearing missing operators (Check, Verify,
Filter):

- **`GroupBy(items, key) → groups`** — partitions items by a key
  field. Deferred because current demand is covered by
  `operator_extract` producing a structured field followed by
  client-side grouping; no live query class in the plan library
  or the 4.10.0 shakeout has surfaced needing this as a discrete
  operator.
- **`Aggregate(groups, reducer) → summary`** — typically paired
  with GroupBy, reduces each group to a scalar / summary. Same
  deferral rationale.

Both remain valid follow-up work. If a concrete compositional
query needs either, file a bead and spin a follow-up RDR rather
than re-opening this one. Paper coverage after this RDR's Phases
1–2 land is **11/13 = 84.6%** with GroupBy + Aggregate as the
two remaining outstanding operators.

#### Gap 4: `plan_match` discrimination in the 0.40–0.65 ambiguous zone is caller-pinned but not content-aware

`src/nexus/plans/matcher.py:132` applies a dimension hard-filter then
MiniLM cosine similarity, gating at `min_confidence=0.40` (calibrated
in RDR-079 against a 9-plan corpus at F1-optimal). Above 0.65 the match
is strong; below 0.40 we fall through to dynamic generation. In the
0.40–0.65 band, MiniLM on 1-2 sentence plan descriptions cannot
reliably disambiguate between within-domain candidates.

Two mitigations have shipped since this RDR was first drafted (2026-04-17),
both via RDR-092 (closed 2026-04-23):

1. **Per-call `min_confidence` override** (RDR-092 Phase 2 Option A,
   `src/nexus/mcp/core.py:2175`). Verb skills that have validated a
   stricter floor (0.50 per the 5+5 synthetic-probe corpus) pin it
   per-call without moving the global knob. Bounds-checked to `[0, 1]`;
   `None` preserves the RDR-079 P5 default.

2. **Hybrid `match_text` synthesis** (RDR-092 Phase 3, `src/nexus/db/t2/plan_library.py`).
   Both the T1 cosine lane and the T2 FTS5 lane now embed
   `"<description>. <verb> <name> scope <scope>"` rather than the raw
   query text. RDR-092 Phase 5 canary evidence: rank-1 attractor
   landings 4/10 → 1/10, noise-probe rejection rate above the 0.40
   floor 1/10 → 0/3.

These address the *tail symptoms* (attractor landings, over-eager
matches on generic queries) but leave the *core limitation* unchanged:
there is no content-aware second opinion once a candidate lands in
the ambiguous band. The paper's &gt;90% LLM-rerank gate recognizes when
a candidate is *plausible but unsafe* and treats it as ad-hoc — neither
a caller-pinned floor nor richer embedding synthesis reproduces that
judgment.

**Impact**: lower than when first drafted. RDR-092 meaningfully reduced
the incidence of bad matches in the ambiguous band across the live
plan library (now 50 rows, 12 builtin — see Background). Remaining
exposure is the case where a high-stakes verb skill has raised its
floor to 0.50 but a candidate *still* lands plausibly in 0.50–0.65
while being semantically off-target. For those skills the LLM rerank
is additive: it inspects the candidate-vs-query pairing in natural
language rather than reasoning about cosine distance alone.

The scope of Gap 4 therefore narrows from "global precision fix" to
"optional last-mile discrimination for high-stakes verb skills" —
still valuable, but no longer the correctness-risk framing of the
pre-RDR-092 draft.

## Context

### Background

The AgenticScholar paper (indexed at `knowledge__agentic-scholar`, 172
chunks, voyage-context-3) is Nexus's reference architecture. RDR-042
(accepted 2026-03-29) adopted the operator model; RDR-080 (accepted
2026-04-15) consolidated retrieval around the `operator_*` family and
`plan_run` parallel DAG executor. The retrospective on 2026-04-17
(stored in `knowledge__nexus`) found that Nexus exposes 5 `operator_*`
family functions (`extract`, `rank`, `compare`, `summarize`, `generate`)
plus 4 retrieval / graph tools (`search`, `query`, `traverse`,
`store_get_many`) that plans compose via `plan_run` — 9 composable
tools against the paper's ~13 operators. Check / Verify / Filter are
the three missing paper operators worth implementing; the retrieval
side is essentially complete.

Downstream of the original 2026-04-17 draft: the plan library has
grown from 9 rows to 50 (12 builtins after RDR-092 Phase 0a retired
the legacy `_PLAN_TEMPLATES` array + Phase 0d backfilled dimensions
on pre-existing rows). RDR-092 (closed 2026-04-23) also shipped
per-call `min_confidence` override and hybrid `match_text` synthesis,
both of which materially affect Gap 4's framing — see Gap 4 above.

### Technical Environment

- `src/nexus/mcp/core.py` — operator and tool definitions. 5 `operator_*`
  functions (`extract` @1399, `rank` @1427, `compare` @1453, `summarize`
  @1539, `generate` @1567) + 21 other MCP tools registered via
  `@mcp.tool()` decorator. All async FastMCP.
- `src/nexus/operators/dispatch.py` — `claude_dispatch(prompt, schema, timeout)`
  is the substrate for LLM-backed operators. Schema-conformance at
  scale has been exercised by the 4.10.0 operator-bundling rollout
  (`src/nexus/plans/bundle.py`) which composes multi-operator prompts
  over this substrate.
- `src/nexus/plans/matcher.py` — dimension-filter + MiniLM-cosine at
  `plan_match` (@120) with `min_confidence` gate (@200) + FTS5
  fallback; scope-fit reranking added by RDR-091 (@55).
- `src/nexus/plans/runner.py` — plan DAG executor, handles operator composition.
- `src/nexus/db/t2/plan_library.py` — `_synthesize_match_text` (RDR-092
  Phase 3) produces the hybrid embedding input used by both matching
  lanes.

## Research Findings

### Investigation

Primary source: `knowledge__agentic-scholar` — chunks defining the
operator algebra (§D.2 Check/Verify; §D.4 Filter/GroupBy/Aggregate).
Cross-referenced against `src/nexus/mcp/core.py` for current operator
inventory.

Planning calibration: RDR-079 (plan library promotion) calibrated
`min_confidence=0.40` against a 9-plan corpus at F1-optimal
(precision ≈ 0.72, recall ≈ 0.81). That calibration predates RDR-092's
hybrid `match_text` embedding shift and the growth to 50 plans (12
builtin) — re-calibration against the current corpus is a
prerequisite for Phase 3.

### Key Discoveries

- **Verified** (`src/nexus/operators/dispatch.py`): the `claude_dispatch`
  substrate accepts an arbitrary JSON schema and returns the parsed
  `structured_output`. No new infrastructure needed for Check/Verify/Filter
  implementations — they fit the existing pattern.
- **Verified** (`src/nexus/mcp/core.py:1453`): `operator_compare` exists
  with a Markdown text output. It is not a drop-in for Check because
  callers cannot branch on a boolean.
- **Verified** (`src/nexus/plans/matcher.py:120–135`): `min_confidence`
  is a single scalar gate with a global default of 0.40. No tiered
  logic on the cosine side. RDR-091 `_scope_fit` reranker (@55) is
  orthogonal — it's a scope-tag multiplier, not a confidence
  discriminator.
- **Verified** (`src/nexus/mcp/core.py:2175`, RDR-092 Phase 2 Option A):
  `nx_answer` accepts a per-call `min_confidence` override. Verb
  skills that validated a stricter floor use it; the global default
  is preserved for the rest.
- **Verified** (`src/nexus/plans/bundle.py`, shipped in 4.10.0):
  `claude_dispatch`-backed schema-conformance has been exercised at
  scale through multi-operator bundle prompts. Zero schema-drift
  failures observed in the 4.10.0 shakeout of the ~10 bundled plans
  run on real corpora. Partially answers the first Critical
  Assumption below.
- **Documented** (paper §6.1): the paper's LLM rerank uses a chain-of-thought
  plan-description-vs-query scoring prompt gated at &gt;90% confidence.

### Critical Assumptions

- [x] `claude_dispatch` schema-conformance is reliable enough for
      boolean-decision operators — **Status**: Partially verified — the
      4.10.0 operator-bundling rollout ran composed JSON-schema
      prompts through `claude_dispatch` on real workloads without
      schema-drift failures. Boolean-specific spike still recommended
      but the base infrastructure is known good.
- [ ] LLM rerank in the 0.40–0.65 band adds discriminative signal
      beyond MiniLM cosine *after* RDR-092's hybrid `match_text` has
      already absorbed the low-hanging attractor cases — **Status**:
      Unverified — **Method**: Spike against the **full live plan
      library (currently 50 rows — 12 builtin + 38 backfilled
      personal/project-scoped)**, not the builtin subset alone.
      The ambiguous-band incidence rate scales with corpus density;
      measuring on 12 plans understates the production risk. The
      spike must run with hybrid `match_text` already in effect (the
      post-RDR-092 baseline) and measure both precision delta AND
      recall delta versus the rerank-off control in the same
      configuration. **Pre-agreed success criteria** (both must hold
      for rerank to "pass"):
      1. **Precision delta ≥ 0.05 absolute** on ambiguous-band queries
         (e.g. 12/20 → 13/20 is below; 12/20 → 14/20 clears).
      2. **Recall delta > -0.15 absolute** — the Consequences section
         notes rerank is precision-preferred and trades recall; the
         spike must bound that trade. If recall drops by more than
         0.15 to earn the precision delta, the trade is rejected
         regardless of precision gain.
      Missing either threshold closes Gap 4 as "already addressed by
      RDR-092" per the Phase 3 framing. The spike output is the
      precision-recall pair for opt-in verb skills to make an
      informed enablement decision at implementation time.

### Recorded Findings (T2)

Structured research findings persisted via `nx memory` under project
`nexus_rdr`. Retrievable by title prefix `088-research-` for audit,
diff, and cross-link.

1. **088-research-1** — RDR-092 baseline for Gap 4 Phase 3 spike.
   Captures plan-library counts (50 total / 12 builtin), the hybrid
   `match_text` embedding shape, RDR-092 Phase 5 canary evidence
   (rank-1 attractor 4/10 → 1/10), and the per-call `min_confidence`
   override semantics. Purpose: reproducible baseline that Phase 3's
   prerequisite spike measures delta against, so the value attributed
   to LLM rerank is only what it adds beyond RDR-092. Recorded
   2026-04-24.

2. **088-research-2** — 4.10.0 operator-bundling validates
   `claude_dispatch` JSON-schema substrate at scale. Three live
   nx_answer calls routed through `bundle.py::dispatch_bundle`
   returned parsed `structured_output` in every case; zero schema-drift
   failures. Narrows Critical Assumption #1's spike scope from
   "substrate reliability" (covered) to "boolean-verdict stability"
   (still open, needs a targeted 10-run repeatability test on one
   representative Check prompt). Recorded 2026-04-24.

3. **088-research-3** — authoritative operator inventory. Nexus
   has 5 `operator_*` family functions (`extract`, `rank`, `compare`,
   `summarize`, `generate`) plus 4 retrieval / graph tools (`search`,
   `query`, `traverse`, `store_get_many`) — 9 composable plan-step
   tools against the paper's ~13 operators (§D.2, §D.4). This RDR
   closes 3 of the 5 missing paper ops (Check, Verify, Filter);
   GroupBy and Aggregate are deferred because no current query
   class demands them. Supersedes the "10 of 13" count from the
   2026-04-17 retrospective, which conflated categories. Recorded
   2026-04-24.

## Proposed Solution

### Approach

Four additions, all in `src/nexus/mcp/core.py` plus one `plans/matcher.py`
extension. Each operator follows the existing `claude_dispatch` pattern:
compose prompt, define JSON schema, dispatch, surface typed errors.

### Technical Design

**`operator_check`** — signature:
`check(items: list[dict], check_instruction: str, timeout: float = 300.0) → {ok: bool, evidence: list[{item_id, quote, role}]}`

Role values: `supports` | `contradicts` | `neutral`. Populate `evidence`
with at least one entry per item unless `ok=True` trivially (all
supporting). Schema enforced via `claude_dispatch`.

**`operator_verify`** — signature:
`verify(claim: str, evidence: str, timeout: float = 300.0) → {verified: bool, reason: str, citations: list[str]}`

Single-claim variant. `citations` returns span anchors from `evidence`
that ground the verdict.

**`operator_filter`** — signature:
`filter(items: list[dict], criterion: str, timeout: float = 300.0) → {items: list[dict], rationale: list[{id, reason}]}`

Returns filtered subset with per-item rejection reasons. The JSON schema
constrains the `items` output to a subset of input `id`s.

**`plan_match` LLM rerank in ambiguous zone** — extend `src/nexus/plans/matcher.py`:

```text
# Illustrative — verify API signatures during implementation.
# Effective floor is the caller's per-call min_confidence (RDR-092
# Phase 2) falling back to the 0.40 default. The rerank band is
# (effective_floor, 0.65]; when effective_floor >= 0.65 the rerank
# is a no-op because a match above 0.65 is already strong enough
# that an LLM second opinion adds no value in expectation.
_RERANK_BAND_CEILING = 0.65
floor = effective_min_confidence
if llm_rerank_enabled and floor < _RERANK_BAND_CEILING:
    if floor <= top_confidence <= _RERANK_BAND_CEILING:
        rerank_result = await claude_dispatch(rerank_prompt, confidence_schema, timeout=60)
        if rerank_result.confidence < 0.90:
            return NoMatch  # fall through to dynamic generation
# Else (floor >= ceiling OR outside band): normal path, no rerank.
```

Config flag `plan_match.llm_rerank` defaults False. Opt-in via `.nexus.yml`
or env `NEXUS_PLAN_MATCH_LLM_RERANK=1`. Latency cost ~1s per ambiguous
match; off the happy path for high-confidence matches.

**Composition semantics** with the RDR-092 per-call `min_confidence` override:

| Caller `min_confidence` | Effective rerank band | Rerank fires when |
|---|---|---|
| Unset (default 0.40) | (0.40, 0.65] | top-1 ∈ (0.40, 0.65] |
| 0.50 (verb-skill precision pin) | (0.50, 0.65] | top-1 ∈ (0.50, 0.65] |
| 0.65 | none | never — caller already at/above ceiling |
| 0.70+ | none | never — caller above ceiling (no-op guard) |

The `effective_floor >= 0.65` no-op is deliberate: a verb skill
demanding that much confidence treats "landed above 0.65" as
sufficient and the rerank's discriminative pass adds latency
without accuracy gain. Documented rather than silent.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `operator_check` | `operator_compare` | **Keep both**: compare returns text for human presentation; check returns boolean for plan branching. Different contracts. |
| `operator_verify` | `operator_check` (above) | **Different granularity**: verify is single-claim; check is cross-doc. Paper explicitly separates them. |
| `operator_filter` | ChromaDB `where=` filter in `search_engine.py` | **Extend, don't reuse**: where-filter operates at retrieval; operator_filter composes over prior-step results. Complementary. |
| LLM rerank in plan_match | `src/nexus/plans/matcher.py` + RDR-092 per-call `min_confidence` (`core.py:2175`) | **Extend, compose with existing**. Per-call floor (RDR-092) handles the "this skill demands precision" case; LLM rerank adds content-aware discrimination on top when `llm_rerank_enabled`. Not a replacement for the floor. |

### Decision Rationale

Each operator is independently useful; grouping them in one RDR captures
the thematic coherence (closing paper-coverage gaps) without creating
artificial dependencies between them. Filter lands first (smallest,
most composable, unblocks Q4-class queries). Check + Verify land
together (shared evidence-schema plumbing). plan_match rerank lands
last (depends on an existing spike to verify signal quality).

## Alternatives Considered

### Alternative 1: Implement Check/Verify via operator_compare

**Description**: Repurpose `operator_compare` with a stricter output
schema (booleans + evidence) instead of adding new operators.

**Pros**: No new operators to maintain.

**Cons**: Breaks the existing compare contract (text → structured).
Callers of operator_compare expect human-readable text, not branching
booleans. Schema changes are load-bearing for plan runners.

**Reason for rejection**: Paper separates Check and Verify as a
deliberate design — different cardinalities (N-way vs. 1-way) and
different consumers (plan branches vs. presentation). Flattening
into compare discards that distinction.

### Briefly Rejected

- **Delete operator_compare and replace with check**: breaks existing plans in `PlanLibrary` that reference compare.
- **Make plan_match LLM rerank the only matcher**: adds 1s latency to every plan match, including the 80% that are unambiguous. Optional second-pass is cheaper.

## Trade-offs

### Consequences

- `operator_*` surface grows 5 → 8 (extract, rank, compare, summarize,
  generate, + check, verify, filter). Composable-tool total 9 → 12.
  Closes the three-operator gap identified in the 2026-04-17
  retrospective.
- `plan_match` becomes config-sensitive; tests must cover rerank-on,
  rerank-off, and the rerank-plus-caller-floor composition path.
- Plan authors gain structured-branching capability for integrity-checking and filtering pipelines.
- **Phase 3 rerank shifts the RDR-079 F1 operating point** (precision-preferred).
  RDR-079 calibrated `min_confidence=0.40` as the F1-optimal threshold
  for the bundled MiniLM T1 cache — precision ≈ 0.72 / recall ≈ 0.81.
  Adding a 0.90-gated LLM rerank in `(effective_floor, 0.65]` converts
  some plans that would have matched at ~0.50–0.60 cosine into
  `NoMatch` fall-throughs, dropping recall in exchange for higher
  precision on the plans that *do* match. This is acceptable and
  intentional for verb skills that opt in — each such skill has
  validated that a precision-first posture fits its workload (e.g.
  `/nx:query` pins `min_confidence=0.50` already per RDR-092 Phase 2).
  But it is not a free improvement: every opt-in verb skill that
  enables `plan_match.llm_rerank` should expect *more* fall-throughs
  to dynamic planning at the same cost budget, and the Phase 3
  calibration spike's pre-agreed ≥ 0.05 precision delta threshold
  must be interpreted against an understanding that recall will
  move downward by a non-zero amount.
- Latency: Check/Verify/Filter each add ~3–10s per call (Haiku roundtrip); no change to retrieval hot path.

### Risks and Mitigations

- **Risk**: `claude_dispatch` JSON-schema conformance drifts across Claude model versions, silently producing malformed check/verify outputs.
  **Mitigation**: Validate schema at boundary with typed exceptions; add a contract test that invokes each operator against a golden input. The 4.10.0 operator-bundling rollout already exercises this substrate on real workloads — treat its ongoing stability as a canary.

- **Risk**: LLM rerank in plan_match produces confidence scores
  disconnected from cosine-space calibration, making 0.90 threshold
  arbitrary.
  **Mitigation**: Calibrate threshold against the current 12-builtin
  corpus with synthetic ambiguous queries (spike), measuring the
  delta *after* RDR-092's hybrid match_text is already in effect so
  the spike isn't mis-attributing value the library already captured.

- **Risk (new)**: Gap 4 becomes redundant — if the post-RDR-092 ambiguous-band incidence
  rate on the live library is low enough, the LLM rerank carries all
  of its complexity cost with marginal precision benefit.
  **Mitigation**: Phase 3's prerequisite spike is specifically designed
  to answer this. Gate implementation on the spike showing a
  meaningful precision delta beyond what RDR-092 already delivers.
  Acceptable to close Gap 4 without implementing if the spike says
  "already addressed".

### Failure Modes

- **Visible**: schema-validation failure → `OperatorOutputError` raised with snippet; plan runner surfaces typed exception.
- **Silent**: rerank threshold miscalibration → subtle precision loss in plan selection; detected by the benchmark in RDR-090 (retrieval quality regression).

## Implementation Plan

### Prerequisites

- [ ] Spike: run `operator_check` prototype against 20 consistency questions on `knowledge__delos`; measure verdict stability.
- [ ] Spike: run `plan_match` LLM rerank prototype on 20 synthetic ambiguous queries against the **full live plan library (currently 50 rows)**, not the 12-builtin subset. Measure precision delta *relative to the post-RDR-092 baseline* (hybrid `match_text` already in effect, per-call `min_confidence` at the verb-skill-typical 0.50). Pre-agreed success threshold: precision delta ≥ 0.05 absolute. If below threshold, Phase 3 should close as "already addressed by RDR-092" rather than land.

### Minimum Viable Validation

A plan chaining `search → operator_filter → operator_check` runs
end-to-end via `plan_run`, returns structured output, and each operator's
contract is exercised.

### Phase 1: `operator_filter`

#### Step 1: Add `operator_filter` to `src/nexus/mcp/core.py`

Follow the `operator_extract` pattern (lines ~1399–1423). Define JSON
schema: `{items: [...], rationale: [...]}` with `items` constrained
to a subset of input IDs.

#### Step 2: Integrate with `plan_run`

Add operator to the DAG operator registry. Unit test: plan with
`filter` step after `search` narrows results correctly.

### Phase 2: `operator_check` + `operator_verify`

#### Step 1: Implement both operators with shared evidence schema

Extract `{item_id, quote, role}` schema into a shared definition.

#### Step 2: Integration test

`traverse → operator_check` plan against citation graph in
`knowledge__delos` (papers that cite the same baseline but report
different numbers).

### Phase 3: `plan_match` LLM rerank

#### Step 1: Extend `src/nexus/plans/matcher.py`

Add optional second-pass reranker gated by `effective_floor ≤ top_confidence ≤ 0.65`
band, where `effective_floor` is the caller-supplied `min_confidence`
(RDR-092 Phase 2) falling back to the 0.40 default. Config: `.nexus.yml`
key `plan_match.llm_rerank: bool`, env `NEXUS_PLAN_MATCH_LLM_RERANK`.

#### Step 2: Calibration

Run against the **full live plan library** (50 rows as of
2026-04-24; builtin count grows over time — always target the
current full corpus, not a frozen subset) with synthetic
ambiguous queries (prerequisite spike). Document precision
delta versus the post-RDR-092 baseline — not the pre-RDR-092
state — so the value attributed to this phase reflects only
what LLM rerank adds beyond what `match_text` already delivers.
Pre-agreed success threshold: precision delta ≥ 0.05 absolute.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Operator registrations | `nx catalog search --type=operator` | N/A | N/A | integration test | code |
| `plan_match.llm_rerank` config | N/A | `nx config get` | N/A | spike output | config file |

### New Dependencies

None. All additions use `claude_dispatch` (existing) and existing Python stdlib.

## Test Plan

- **Scenario**: `operator_filter` called with 10 items and a natural-language criterion — **Verify**: returns strict subset with per-item rationale, total output length ≤ input length.
- **Scenario**: `operator_check` called with 3 papers that agree on a claim — **Verify**: `ok=True`, evidence list has ≥1 supporting quote per paper, no contradicts.
- **Scenario**: `operator_check` called with 3 papers where 1 contradicts — **Verify**: `ok=False`, evidence includes the contradicting quote with `role=contradicts`.
- **Scenario**: `operator_verify` on a grounded claim — **Verify**: `verified=True` with citation spans.
- **Scenario**: `plan_match` with an ambiguous 0.55 top-1 confidence, caller `min_confidence=0.50`, and rerank on — **Verify**: either the rerank clears the 0.90 gate and the plan is selected, or the rerank fails and `NoMatch` is returned (fall through to dynamic generation); never returned as a weak match between these two outcomes.
- **Scenario**: `plan_match` with caller `min_confidence=0.70` and rerank on — **Verify**: rerank is a documented no-op (top-1 must land ≥ 0.70 on its own merits); the ceiling guard in the pseudocode prevents an empty band from firing rerank.

## Validation

### Testing Strategy

Unit tests per operator (mocked `claude_dispatch`), plus an integration
test composing `search → filter → check` via `plan_run` against
`knowledge__delos`. Matcher regression tested against the **current
full plan library** (50 rows at time of writing, 12 builtin +
38 backfilled) — NOT the historical RDR-079 9-plan corpus, which
predates RDR-092's hybrid `match_text` and is no longer
representative of production matcher behavior. When the matcher
behavior is expected to shift with library growth, the regression
test must parameterize on the live library snapshot rather than
hard-code counts.

## Finalization Gate

Gated 2026-04-24 via `/nx:rdr-gate 088`. First pass surfaced one
critical and three significant issues — all documentation-level.
Fixed in-place before the re-gate:

1. **Critical** — Rerank band formula silently misfires when
   `effective_floor ≥ 0.65`. Fixed by adding an explicit
   `floor < _RERANK_BAND_CEILING` guard in the Technical Design
   pseudocode, plus a composition-semantics table making the
   no-op case explicit. Test Plan gained a dedicated scenario for
   the `min_confidence=0.70` ceiling guard.
2. **Significant** — GroupBy and Aggregate deferred operators
   were unregistered. Fixed by adding an explicit "Deferred from
   this RDR's scope" subsection to the Problem Statement with
   deferral rationale. Coverage math (11/13 = 84.6% after Phases
   1–2) now identifies the two remaining operators explicitly.
3. **Significant** — Spike corpus was 12-builtin subset; should
   be the full 50-row live library to avoid understating
   ambiguous-band incidence at production density. Fixed in
   Critical Assumption #2, Prerequisites, and Phase 3 Step 2.
   Added a pre-agreed numeric threshold (precision delta ≥ 0.05
   absolute) so the gate decision after the spike is
   unambiguous.
4. **Significant** — Validation / Testing Strategy referenced
   the historical RDR-079 9-plan corpus. Fixed to target the
   current full plan library with guidance that the regression
   must parameterize on the live library rather than hard-code
   counts.

Critic observations addressed (not just noted):

- **RDR-079 precision-recall tension** — promoted from observation
  to first-class trade-off. The Consequences section now explicitly
  states that Phase 3's 0.90-gated rerank converts some ~0.50–0.60
  cosine matches into `NoMatch` fall-throughs, lowering recall in
  exchange for higher precision, and calls out that opt-in verb
  skills are accepting that trade. The Prerequisites / Critical
  Assumption #2 spike now measures **both** precision delta (≥ 0.05
  absolute, unchanged) **and** recall delta (> -0.15 absolute,
  new) — *both* must hold for the rerank to "pass". A precision
  gain earned by collapsing recall by > 0.15 is rejected regardless.
  Gate decision is now informed by the full precision-recall pair,
  not a one-sided metric.
- **T2 finding retrieval by short title** — filed as a separate
  beads concern (`nexus-e59o`, P3 feature). `nx memory get --title`
  currently requires exact match; prefix-match with ambiguity-error
  is the proposed fix. Orthogonal to RDR-088 content; tracked
  explicitly so it doesn't reappear as "unaddressed observation"
  in a future audit.

Re-gate result: PASSED. No critical issues outstanding; three
significant issues resolved; two observations converted into either
RDR content (recall delta threshold) or tracked follow-up work
(nexus-e59o).

## References

- Paper: `knowledge__agentic-scholar` §D.2 (Check/Verify), §D.4 (Filter)
- Retrospective: `knowledge__nexus` → "AgenticScholar Retrospective 2026-04-17"
- `src/nexus/mcp/core.py:1399–1596` — current `operator_*` implementations
  (extract @1399, rank @1427, compare @1453, summarize @1539, generate @1567)
- `src/nexus/mcp/core.py:2175` — `nx_answer` per-call `min_confidence` override (RDR-092 Phase 2)
- `src/nexus/plans/matcher.py:120–200` — `plan_match` signature + `min_confidence` gate
- `src/nexus/db/t2/plan_library.py::_synthesize_match_text` — hybrid embedding input (RDR-092 Phase 3)
- `src/nexus/operators/dispatch.py` — claude_dispatch substrate
- `src/nexus/plans/bundle.py` — operator-bundle composition over claude_dispatch (4.10.0)
- RDR-079 — plan library promotion and calibration
- RDR-080 — retrieval layer consolidation
- RDR-092 — plan match-text from dimensional identity (per-call `min_confidence` + hybrid `match_text`, closed 2026-04-23)
