---
title: "RDR-088: AgenticScholar Operator-Set Completion"
id: RDR-088
type: Feature
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-17
accepted_date:
related_issues: []
related_tests: [test_operators.py, test_plan_matcher.py]
related: [RDR-042, RDR-078, RDR-079, RDR-080]
---

# RDR-088: AgenticScholar Operator-Set Completion

The AgenticScholar retrospective (persisted to `knowledge__nexus`, 2026-04-17)
identified four concrete operator-layer gaps between Nexus and the paper
we built against. Three are missing paper operators
(`Check`, `Verify`, `Filter`); one is a calibration gap in `plan_match`
where the ambiguous 0.40–0.65 confidence zone produces low-signal decisions
at current scale and will degrade further as the plan library grows.

All four are small: two–three days per gap, no new dependencies, no
persistent storage. Grouping them in one RDR produces a coherent narrative
— each closes a specific paper capability and together they unblock
multi-hop query classes we currently cannot express.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: `operator_check` (cross-doc consistency) missing

Paper §D.2, Query 16 defines `Check(items, check_instruction) → {ok, evidence}`
— validates a claim's consistency across peer documents and returns a
structured boolean plus grounding evidence. Nexus's nearest analog is
`operator_compare` (`src/nexus/mcp/core.py:1373`) which returns free-text
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

#### Gap 4: `plan_match` has no signal in the 0.40–0.65 ambiguous zone

`src/nexus/plans/matcher.py:59` applies a dimension hard-filter then
MiniLM cosine similarity, gating at `min_confidence=0.40` (calibrated
in RDR-079 against 9 plans). Above 0.65 the match is strong; below 0.40
we fall through to dynamic generation. In the 0.40–0.65 band, MiniLM
on 1-2 sentence plan descriptions cannot reliably disambiguate between
within-domain candidates. The paper's &gt;90% LLM-rerank gate does one
thing ours doesn't: it recognizes when a candidate is *plausible but
unsafe* and treats it as ad-hoc.

**Impact**: today this is tolerable at 9 plans; RDR-079 promotion
discipline will grow the library. At 30+ plans the false-positive rate
in the ambiguous band becomes a correctness risk.

## Context

### Background

The AgenticScholar paper (indexed at `knowledge__agentic-scholar`, 172
chunks, voyage-context-3) is Nexus's reference architecture. RDR-042
(accepted 2026-03-29) adopted the operator model; RDR-080 (accepted
2026-04-15) consolidated retrieval around the `operator_*` family and
`plan_run` parallel DAG executor. The retrospective on 2026-04-17
(stored in `knowledge__nexus`) found that paper → Nexus operator
coverage is ~77% by count (10 of 13 operators matched) but several
missing ones are load-bearing for flagship compositional queries.

### Technical Environment

- `src/nexus/mcp/core.py` — operator definitions (10 current operators,
  all async FastMCP tool decorators).
- `src/nexus/operators/dispatch.py` — `claude_dispatch(prompt, schema, timeout)`
  is the substrate for LLM-backed operators.
- `src/nexus/plans/matcher.py` — dimension-filter + MiniLM-cosine + FTS5 fallback.
- `src/nexus/plans/runner.py` — plan DAG executor, handles operator composition.

## Research Findings

### Investigation

Primary source: `knowledge__agentic-scholar` — chunks defining the
operator algebra (§D.2 Check/Verify; §D.4 Filter/GroupBy/Aggregate).
Cross-referenced against `src/nexus/mcp/core.py` for current operator
inventory.

Planning calibration: RDR-079 (plan library promotion) calibrated
`min_confidence=0.40` against a 9-plan corpus. Precision ≈ 0.72 at
that threshold. Above 30 plans the precision degrades because
within-domain plans share vocabulary.

### Key Discoveries

- **Verified** (`src/nexus/operators/dispatch.py`): the `claude_dispatch`
  substrate accepts an arbitrary JSON schema and returns the parsed
  `structured_output`. No new infrastructure needed for Check/Verify/Filter
  implementations — they fit the existing pattern.
- **Verified** (`src/nexus/mcp/core.py:1373`): `operator_compare` exists
  with a Markdown text output. It is not a drop-in for Check because
  callers cannot branch on a boolean.
- **Verified** (`src/nexus/plans/matcher.py:59–74`): `min_confidence`
  is a single scalar gate; no tiered logic.
- **Documented** (paper §6.1): the paper's LLM rerank uses a chain-of-thought
  plan-description-vs-query scoring prompt gated at &gt;90% confidence.
- **Assumed** (needs validation): that a JSON-schema-constrained
  `operator_check` output is sufficiently reliable to drive boolean
  plan branching. Requires a spike: run 20 consistency questions
  against `knowledge__delos` and measure verdict stability.

### Critical Assumptions

- [ ] `claude_dispatch` schema-conformance is reliable enough for
      boolean-decision operators — **Status**: Unverified — **Method**: Spike
- [ ] LLM rerank in the 0.40–0.65 band adds discriminative signal
      beyond MiniLM cosine — **Status**: Unverified — **Method**: Spike on
      the 9-plan corpus with synthetic ambiguous queries

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
# Illustrative — verify API signatures during implementation
if 0.40 <= top_confidence <= 0.65 and llm_rerank_enabled:
    rerank_result = await claude_dispatch(rerank_prompt, confidence_schema, timeout=60)
    if rerank_result.confidence < 0.90:
        return NoMatch  # fall through to dynamic generation
```

Config flag `plan_match.llm_rerank` defaults False. Opt-in via `.nexus.yml`
or env `NEXUS_PLAN_MATCH_LLM_RERANK=1`. Latency cost ~1s per ambiguous
match; off the happy path for high-confidence matches.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `operator_check` | `operator_compare` | **Keep both**: compare returns text for human presentation; check returns boolean for plan branching. Different contracts. |
| `operator_verify` | `operator_check` (above) | **Different granularity**: verify is single-claim; check is cross-doc. Paper explicitly separates them. |
| `operator_filter` | ChromaDB `where=` filter in `search_engine.py` | **Extend, don't reuse**: where-filter operates at retrieval; operator_filter composes over prior-step results. Complementary. |
| LLM rerank in plan_match | `src/nexus/plans/matcher.py` | **Extend** with optional second-pass reranker gated by top-1 confidence band. |

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

- Operator surface area grows from 10 → 13 — matches paper's count exactly.
- `plan_match` becomes config-sensitive; tests must cover both rerank-on and rerank-off paths.
- Plan authors gain structured-branching capability for integrity-checking and filtering pipelines.
- Latency: Check/Verify/Filter each add ~3–10s per call (Haiku roundtrip); no change to retrieval hot path.

### Risks and Mitigations

- **Risk**: `claude_dispatch` JSON-schema conformance drifts across Claude model versions, silently producing malformed check/verify outputs.
  **Mitigation**: Validate schema at boundary with typed exceptions; add a contract test that invokes each operator against a golden input.

- **Risk**: LLM rerank in plan_match produces confidence scores disconnected from cosine-space calibration, making 0.90 threshold arbitrary.
  **Mitigation**: Calibrate threshold against the existing 9-plan corpus with synthetic ambiguous queries (spike).

### Failure Modes

- **Visible**: schema-validation failure → `OperatorOutputError` raised with snippet; plan runner surfaces typed exception.
- **Silent**: rerank threshold miscalibration → subtle precision loss in plan selection; detected by the benchmark in RDR-090 (retrieval quality regression).

## Implementation Plan

### Prerequisites

- [ ] Spike: run `operator_check` prototype against 20 consistency questions on `knowledge__delos`; measure verdict stability.
- [ ] Spike: run `plan_match` LLM rerank prototype on 20 synthetic ambiguous queries against the 9-plan corpus; measure precision delta.

### Minimum Viable Validation

A plan chaining `search → operator_filter → operator_check` runs
end-to-end via `plan_run`, returns structured output, and each operator's
contract is exercised.

### Phase 1: `operator_filter`

#### Step 1: Add `operator_filter` to `src/nexus/mcp/core.py`

Follow the `operator_extract` pattern (lines ~1318–1345). Define JSON
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

Add optional second-pass reranker gated by `0.40 ≤ top_confidence ≤ 0.65`
band. Config: `.nexus.yml` key `plan_match.llm_rerank: bool`.

#### Step 2: Calibration

Run against the 9-plan corpus with synthetic ambiguous queries
(prerequisite spike). Document precision delta.

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
- **Scenario**: `plan_match` with ambiguous 0.50-confidence query and rerank on — **Verify**: either selects a high-confidence plan or falls through to dynamic; never middle-grounds.

## Validation

### Testing Strategy

Unit tests per operator (mocked `claude_dispatch`), plus an integration
test composing `search → filter → check` via `plan_run` against
`knowledge__delos`. Matcher regression tested against the 9-plan
corpus.

## Finalization Gate

_To be completed during /nx:rdr-gate._

## References

- Paper: `knowledge__agentic-scholar` §D.2 (Check/Verify), §D.4 (Filter)
- Retrospective: `knowledge__nexus` → "AgenticScholar Retrospective 2026-04-17"
- `src/nexus/mcp/core.py:1318–1457` — current operator implementations
- `src/nexus/plans/matcher.py:59–74` — current plan matching logic
- `src/nexus/operators/dispatch.py` — claude_dispatch substrate
- RDR-079 — plan library promotion and calibration
- RDR-080 — retrieval layer consolidation
