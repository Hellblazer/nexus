---
title: "RDR-100: Plan-Cache Improvements Inspired by CacheRAG (Diversity, Floor, Dispatcher, Hierarchy)"
id: RDR-100
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-30
related_issues: []
related_tests: []
related: [RDR-079, RDR-080, RDR-091, RDR-092]
---

# RDR-100: Plan-Cache Improvements Inspired by CacheRAG (Diversity, Floor, Dispatcher, Hierarchy)

The CacheRAG paper (Cao et al., indexed in `knowledge__rag-papers` as tumbler `1.653.83`) presents a cache-augmented KGQA architecture whose plan-cache machinery solves four problems that nexus's current `nx_answer` plan-match flow does not. Each problem maps cleanly onto a specific gap in `src/nexus/plans/`. This RDR proposes four phased changes to close those gaps in priority order.

The relevant CacheRAG primitives are:

- A two-layer hierarchical Domain → Aspect index over the plan cache that bounds retrieval to one bucket and prevents cross-domain contamination of in-context examples.
- Maximal Marginal Relevance (MMR, λ=0.5) over the plan cache to enforce structural diversity and prevent reasoning homogeneity (mode collapse) when the planner LLM consumes cached examples as context.
- Bounded subgraph operators (`σ_depth`, `σ_breadth`) with hard complexity guarantees that act as a deterministic floor when targeted reasoning fails. Recall improvement attributed in the paper: 0.756 → 0.927.
- A non-LLM Lightweight Heuristic Dispatcher `T_dispatcher(Q_NL, t, G_t, C)` that decides whether to take the cached path or the bounded-operator path without paying for an LLM call per turn.

Nexus's `plan_match` (`src/nexus/plans/matcher.py`) and `PlanSessionCache` (`src/nexus/plans/session_cache.py`) currently implement: top-N cosine NN over a single flat T1 ChromaDB collection, FTS5 fallback, post-filter on `dimensions` (superset equality), `scope_tags` re-ranker (RDR-091 Phase 2b: conflict-drop + 15% per-fit boost + specificity tie-break), `min_confidence=0.40` calibrated F1 optimum (RDR-079 P5), and an inline LLM planner on every miss (`mcp/core.py:3160`). No diversity, no hierarchical bucketing, no deterministic floor, no non-LLM dispatch.

This RDR is intentionally bounded to the plan-match path. It does not propose changes to the plan execution runner (`runner.py`), the plan storage schema (`plan_library.py:_init_schema`, beyond Phase 4's optional partition keys), or the upstream `nx_answer` dispatcher beyond the four named insertion points. CacheRAG's deeper graph-traversal machinery (`σ_depth` chain following, `σ_breadth` star scans over a knowledge graph) is out of scope: nexus's analogue is across-collection chunk retrieval, not knowledge-graph traversal, so the floor-guarantee idea translates but the operator shape does not.

## Problem Statement

### Gap 1: Cache returns near-duplicates, wasting planner-LLM context budget

`PlanSessionCache.query` (`session_cache.py:79`) returns top-N nearest neighbors by cosine distance. The over-fetch budget (`matcher.py:224`, `_over = max(n*2, n + len(filter_dims)*2)`) widens the candidate pool to absorb post-filter and threshold attrition, but never deduplicates. When five plans cluster tightly in embedding space (e.g., five variants of a `verb=research` plan that differ only in scope tags), the matcher returns five near-duplicates. Downstream, when `nx_answer` falls back to the inline planner with the failed candidates as context (`mcp/core.py:3160`), those near-duplicates eat the planner's attention budget without informing it.

CacheRAG names this exact failure mode as "reasoning homogeneity (mode collapse)" and uses MMR (λ=0.5 default) over the bucket-local cache to enforce structural diversity. The change is local: add an MMR pass after `cache.query(intent, _over)` and before the candidate-scoring loop in `plan_match`.

### Gap 2: No deterministic fallback when all upstream layers miss

The current `nx_answer` chain is cosine cache → FTS5 fallback → inline LLM planner → nothing. Each layer is "probably will work, might not." There is no proof-bearing floor: when the LLM planner returns an unusable plan or the FTS5 matcher returns zero rows and the cache returns zero rows, `nx_answer` returns the empty result. The user sees no chunks, no synthesis, no evidence that retrieval was attempted at all.

CacheRAG's bounded operators (`σ_depth`, `σ_breadth`) are deterministic and always return something within `O(K_depth · K_degree)` cost. The analogue for nexus is not graph traversal but a default single-step `query` plan: across `corpus="all"`, with the raw intent as the query string, no metadata filters, returning the top-K chunks. It is guaranteed to surface something whenever T3 has any data. It does not pretend to be a good answer; it ensures the user sees retrieval evidence rather than silence.

### Gap 3: Every miss pays for an LLM planner call, including obviously-cheap-to-decide intents

When the cache misses (`confidence < min_confidence`) and the FTS5 fallback also misses, `nx_answer` invokes the inline planner via `claude -p` (`mcp/core.py:3160`). For many intents this is wasteful: bare keywords (`"chromadb quotas"`), shell-shaped strings (`"git status"`), single-token queries, and obviously-out-of-scope strings do not need a multi-step plan. They need a direct `query` or a friendly error.

CacheRAG's `T_dispatcher` is a non-LLM heuristic that classifies the intent before deciding the path. The nexus analogue is a tiny pre-filter that runs before the inline planner: classify the intent shape (keyword-only, verb-shaped, out-of-scope), short-circuit the cheap cases, only invoke the LLM planner when the intent actually warrants planning.

### Gap 4: Plan library scales linearly into a flat embedding space; cosine top-N degrades

`PlanSessionCache._col` is a single ChromaDB collection. As the plan library grows (RDR-080 promotes plans from runs into the library, RDR-092 added confidence calibration), all plans compete in the same flat semantic space. The over-fetch budget (`_over = max(n*2, n + len(filter_dims)*2)`) is a partial mitigation, but it scales linearly and post-filtering by `dimensions` drops most candidates after they have already cost a cosine comparison.

CacheRAG's two-layer Domain → Aspect index pre-filters by partition: retrieval is bounded to one bucket, cost is `O(k log k)` per bucket regardless of total cache size. The nexus analogue is partitioning the plan cache by primary dimension (likely `verb`) so retrieval is namespace-scoped before cosine. This is a schema change with migration cost, which is why it is the lowest-priority phase.

## Context

### Existing primitives in `src/nexus/plans/`

- `PlanSessionCache` (`session_cache.py`): T1 ChromaDB Ephemeral or per-session HTTP collection. Cosine distance, top-N, session-keyed via `where={"session_id": self._session_id}`. Distances clamped to `[0.0, 2.0]` after FP-noise compensation.
- `PlanLibrary` (`db/t2/plan_library.py`): SQLite-backed durable store. Columns include `dimensions` (JSON), `scope_tags` (CSV), `ttl`, `disabled_at`, plus FTS5 mirror in `plans_fts`. `search_plans` is the FTS5 fallback; `save_plan` / `delete_plan` / `set_plan_disabled` are the mutation surface.
- `plan_match` (`matcher.py:164`): orchestrates T1 cosine + dimensions post-filter + scope re-rank → T2 FTS5 fallback. Returns `list[Match]` sorted by adjusted score.
- `mcp/core.py:3160`: `nx_answer` invokes `_plan_match` and falls through to the inline LLM planner via `claude -p` on miss.

### Calibration heritage

- RDR-079 P5 set `min_confidence=0.40` (F1-optimal across the bundled MiniLM embedder). Precision-first callers override to 0.50 (precision 0.90, recall 0.19).
- RDR-091 Phase 2b added `_SCOPE_FIT_WEIGHT=0.15` for the scope re-ranker.
- RDR-092 Phase 2 added per-call `min_confidence` override.
- None of these have touched diversity, hierarchy, the floor, or dispatch shape.

### Why these four ideas, not the rest of CacheRAG

CacheRAG's full architecture also includes: a two-stage schema-constrained semantic parser (Intermediate Semantic Representation + Backend Adapter); LLM-synthesized cache warm-up via star-schema sampling; an offline auto-evaluation critic (Llama-3.1-70B, 98.4% reliability). These are KGQA-specific: nexus has no knowledge graph schema to constrain against, no star-schema to sample, and no separate evaluation corpus to warm against. The four ideas adopted here are the architecture-shape ideas (diversity, floor, dispatch, hierarchy) that translate to flat-document retrieval without a KG substrate.

## Proposal

Four phases, in priority order. Each phase is independently shippable; later phases assume earlier phases are in place but do not require them for correctness.

### Phase 1: MMR diversity over the cache over-fetch pool (highest priority)

**Surface**: `src/nexus/plans/matcher.py::plan_match`, after `cache.query(intent, _over)` returns and before the candidate scoring loop.

**Change**: implement MMR re-ranking on the over-fetch pool. For each candidate `(plan_id, distance)`, fetch the plan embedding from the T1 cache (already there, was the basis of the cosine query), then iteratively select candidates that maximize `λ · sim(intent, candidate) − (1 − λ) · max_j sim(candidate, selected_j)`. Default `λ = 0.5` per the CacheRAG default. Pass `λ` through as a per-call parameter on `plan_match` for callers that need precision-only behavior (`λ → 1.0`, equivalent to current top-N).

**Cost**: one extra `O(k²)` pass over the over-fetch pool, where `k = _over` (typically `≤ 20`). Negligible vs the cosine query itself.

**Tests**: `tests/test_plan_match_mmr.py`. Five near-duplicate plans + one outlier in cache, `n=3`, MMR returns three diverse candidates (the outlier is in the result). Same fixture without MMR returns three near-duplicates (regression baseline). Calibration: re-run RDR-079 P5 evaluation harness with MMR enabled, confirm F1 does not drop at the 0.40 threshold (precision-recall tradeoff is the relevant metric).

**Acceptance**: with MMR enabled, the plan-match top-3 over a synthetic cluster of 5 near-duplicates contains at most 1 cluster-member; without MMR, contains 3.

### Phase 2: Deterministic fallback floor in `nx_answer`

**Surface**: `src/nexus/mcp/core.py::nx_answer`, in the branch where both the plan-match gate and the inline planner have failed to produce a usable plan.

**Change**: add a `_default_fallback_plan` constant: a single-step `query` plan with `corpus="all"`, `limit=10`, `intent` passed through as the query string, no metadata filters. When all upstream paths return empty or unusable, run this plan and return its results with a structured envelope flag (`source: "fallback_floor"`) so callers can distinguish "best effort, no plan" from "real plan match." Telemetry: increment a `nx_answer_fallback_floor_invoked` counter so we can measure how often the floor fires.

**Cost**: one extra T3 cosine query when the floor fires. Bounded by `MAX_QUERY_RESULTS = 300`.

**Tests**: `tests/test_nx_answer_fallback_floor.py`. Mock both plan-match and inline planner to return empty, assert the fallback floor fires and returns chunks with the `source: "fallback_floor"` marker. Negative case: assert the floor does NOT fire when plan-match returns a valid plan, even if that plan's execution returns zero chunks (the floor is for plan-failure, not retrieval-failure).

**Acceptance**: `nx_answer` never returns a "no plan, no chunks" result when T3 has any data in the queried corpus. Floor invocations are observable via telemetry.

### Phase 3: Non-LLM dispatcher pre-filter for the inline planner

**Surface**: `src/nexus/mcp/core.py::nx_answer`, immediately before the inline planner invocation.

**Change**: a `_classify_intent_shape(intent: str) -> Literal["keyword", "verb_shaped", "out_of_scope", "needs_planner"]` heuristic. Classification rules (no model, just pattern-matching):

  * `keyword`: < 4 tokens, no question word, no verb. Route directly to `query` over `corpus="all"`, no plan.
  * `verb_shaped`: contains a verb keyword (`how`, `why`, `compare`, `explain`, `analyze`, etc.) or `?`. Pass through to the inline planner as today.
  * `out_of_scope`: matches a deny-pattern (`shell command shapes like ^git\s|^ls\s|^cat\s`, code snippets, single tokens that look like identifiers). Return a friendly error pointing the user at `nx search` or the relevant CLI verb.
  * `needs_planner`: everything else. Pass through to the inline planner.

**Cost**: zero. Pure-Python pattern matching over the intent string. No model, no embedding, no I/O.

**Tests**: `tests/test_nx_answer_dispatcher.py`. Table-driven: 20+ intent fixtures, one per classification path, asserting the right downstream is invoked. Calibration corpus: extract intents from `nx_answer_runs` T2 telemetry, label by whether the inline planner produced a useful plan, confirm the dispatcher's `keyword` and `out_of_scope` classes have ≥ 80% precision (false positives waste user time, but the cost is bounded by the user re-running with explicit `--planner`).

**Acceptance**: median wall-clock of `nx_answer` on the keyword-class fixtures drops by > 50% (from one `claude -p` round trip to one T3 cosine query). Verb-shaped fixtures unchanged.

### Phase 4: Hierarchical Domain partition over the plan cache (lowest priority, schema change)

**Surface**: `src/nexus/plans/session_cache.py::PlanSessionCache`, plus a migration in `db/migrations.py`.

**Change**: partition the T1 ChromaDB collection by primary dimension (`verb`), creating one collection per verb (`plans__session__verb_<verb>`). `PlanSessionCache.query` selects the partition first, then runs cosine within. Pre-filter, not post-filter. The `dimensions` post-filter remains as a secondary trim for non-`verb` dimensions.

**Migration**: per-session collections are ephemeral, no migration required. The mtime-guarded refresh in `mcp_infra.py::get_t1_plan_cache` (line 158) repopulates from `PlanLibrary`, so changing the partition shape is a one-line change in the populator.

**Cost**: one extra collection per active verb. ChromaDB collection limit is high (1000s), nexus has < 20 verbs. Cosine queries are scoped to one collection, so they get faster as the library grows (the win), at the cost of one extra dimension lookup per `query` call (negligible).

**Tests**: `tests/test_plan_session_cache_partitioned.py`. Populate cache with 100 plans across 5 verbs, query with `dimensions={"verb": "research"}`, assert the cosine query is scoped to the `verb_research` partition (mock the chromadb call, assert it received the partitioned collection name). Performance regression test: with 1000 synthetic plans, partitioned query is at least 3× faster than a flat-collection query at the same `n_results=10`.

**Acceptance**: cosine top-N per call costs `O(k log k)` per partition rather than `O(k log N)` over the global pool. Plan-library growth no longer degrades match latency.

## Risks

### Phase 1 (MMR)

The biggest risk is that MMR over a small over-fetch pool (`k = 10–20`) does not have enough diversity to matter. Mitigation: the calibration test on RDR-079's harness will surface this directly. If F1 drops, the MMR pass is gated behind a config flag (`mmr_enabled: bool` in plan-match config) and not enabled by default until tuned.

A secondary risk: the `Match.confidence` field semantics change. Today it carries the raw cosine; under MMR it could carry the MMR-adjusted score. Decision: keep `Match.confidence` as raw cosine (so `min_confidence` still applies cleanly), add a separate `Match.mmr_score` field for ranking. The two-field shape mirrors RDR-091's `confidence` vs `adjusted_score` separation.

### Phase 2 (Deterministic floor)

The floor's "always return something" property risks training users to expect useful results from intents that should fail loudly. Mitigation: the structured envelope's `source: "fallback_floor"` flag is loud, and the CLI rendering in `commands/answer.py` (or wherever) should explicitly mark fallback-floor results as low-confidence. Acceptance gate: the floor's returned chunks must carry a metadata flag that the renderer surfaces in human-readable output.

### Phase 3 (Dispatcher)

The classification heuristics will inevitably mis-classify some intents. The user-visible cost is "I asked for analysis and got raw chunks" or "I asked for a keyword search and got an LLM plan," both recoverable but annoying. Mitigation: the dispatcher's classification is logged to the `nx_answer_runs` telemetry with the classification label. We can mine that for misclassifications and tune the rules. Add an explicit `--planner` / `--no-planner` CLI override so the user can force the path when the heuristic gets it wrong.

### Phase 4 (Hierarchical partition)

The biggest risk is that the partition key (`verb`) is wrong: many plans are dimension-poor or dimensionless, so they land in a default `verb_unknown` partition that ends up dominating the cache. Mitigation: measure the verb distribution in the existing plan library before shipping. If > 30% of plans are in `verb_unknown`, the partition key needs to be richer (`verb` + secondary dimension) before the change is worth it.

A secondary risk: ChromaDB collection-creation cost. Each new collection is a small per-collection overhead (metadata, embedding fn instantiation). With < 20 verbs this is negligible; the test should still confirm cold-start latency does not regress.

## Acceptance

This RDR is accepted when all four phases have a corresponding bead with acceptance criteria, the `related_issues` field is populated, and the priority ordering is reflected in bead dependencies (Phase 1 ready immediately, Phase 2 ready after Phase 1, etc.). The RDR transitions to `closed` when all four phases ship to main and their beads close.

## Research Findings

### 2026-04-30 (T2: `nexus_rdr/100-research-1`)

Empirical investigation against the live plan library and `nx_answer_runs` telemetry produced findings that invalidate the original priority ordering:

- **Q1 (MMR)**: 55 plans in the library, 49 unique strategies, all topically distinct match texts. No near-duplicate clustering observed. Over-fetch budget at `n=3` is 9-12 candidates. **Verdict: re-prioritize to lowest. MMR solves a problem that does not exist at current scale. Revisit above 200 plans.**
- **Q2 (Floor)**: 93% cache hit rate (68/73 runs). True zero-chunk silence: 0 occurrences. The 2 observed "failures" are operator execution errors ("missing required argument"), not plan-miss silence. **Verdict: keep Phase 2 but re-frame. The floor targets operator-execution failures, not retrieval silence.**
- **Q3 (Dispatcher)**: Only 5 of 73 runs (6.8%) invoke the inline planner. Planner average latency is 108s vs 51s for plan hits, a real delta, but affecting too few calls. 68/73 questions are redacted, so keyword-class fraction is unmeasurable. **Verdict: defer until an unredacted intent corpus and >500 runs are available.**
- **Q4 (Hierarchy)**: `verb=research` dominates at 58% (32/55 plans). 0 dimensionless plans. Partition would reduce the hot bucket from 55 to 32 candidates, not the 3x speedup the acceptance criterion requires. **Verdict: correct design, not yet needed. Revisit above 300 plans per verb bucket.**

### Revised priority ordering

The findings invalidate the priority ordering in the original Proposal. New order:

1. **Phase 2 (Deterministic floor)** is now the only phase addressing an observed real failure mode. Re-frame: the floor catches operator-execution failures (the 2 observed "missing required argument" errors), not retrieval silence (0 observed). The fallback should fire on `OperatorError` from the runner, not on empty `nx_answer` results.
2. **Phase 3 (Dispatcher)** stays but moves up to second. Defer the actual implementation until the redaction policy is loosened and the intent corpus exceeds 500 runs. Until then, keep the design recorded but do not file a bead.
3. **Phase 1 (MMR)** moves to third. The failure mode (mode collapse from near-duplicate ICL examples) is real in principle but not observed at current scale (49/55 unique strategies). Park behind Phase 2 + Phase 3.
4. **Phase 4 (Hierarchy)** stays last. The verb skew (58% research) means partition would not deliver the targeted 3x speedup. Defer until per-bucket count exceeds 300.

### What changes in the RDR

- The Phase 2 acceptance criterion ("`nx_answer` never returns a 'no plan, no chunks' result") needs to shift from "retrieval silence" to "operator-execution failure surface a clean fallback rather than an unhandled error." The `_default_fallback_plan` constant remains, but the trigger condition is `runner.OperatorError`, not `len(chunks) == 0`.
- Phase 1's acceptance gate should add a "library size > 200 plans" precondition. Until then, the cluster fixture is synthetic and the test cannot assert real-world behavior.
- Phase 4's acceptance gate should add a "per-verb bucket > 300 plans" precondition. Currently the dominant bucket is 32; the partition is premature optimization at this scale.
- The Open Questions section should add: telemetry redaction policy. The dispatcher phase is blocked on having enough unredacted intents to validate the heuristic classifier.

## Open Questions

- **MMR `λ` calibration**: 0.5 is the CacheRAG default but they were running over a much larger plan pool with KG-shaped diversity. The right `λ` for nexus's MiniLM embedder over typed plans (verbs + scope_tags) is empirical. Phase 1's calibration test should sweep `λ ∈ {0.3, 0.5, 0.7}` and pick the F1-optimal value.
- **Floor query embedder model**: when the floor fires across `corpus="all"`, the cosine query has to embed the intent against multiple embedder targets (`voyage-context-3` for `knowledge__/docs__/rdr__`, `voyage-code-3` for `code__`). Today `query` handles this by routing per-collection. Confirm the floor inherits the same routing.
- **Dispatcher overrides**: should `nx_answer` accept a per-call `dispatch_hint` parameter that the dispatcher honors? Or do we lean on the `--planner` / `--no-planner` CLI flag exclusively? Both have call sites; pick one before Phase 3 ships.
- **Partition migration story**: if Phase 4's partition key changes after ship (e.g., we switch from `verb` to `verb + scope_root`), the T1 cache repopulates on next session start, but live sessions hold stale partitions. Decision: a startup-time partition-key version check that triggers a full repopulate when the version mismatches. Cost is one populate per session start when the version bumps; negligible.
- **Telemetry redaction policy** (added 2026-04-30 from research findings): 68 of 73 recent `nx_answer_runs` rows are redacted. Phase 3's dispatcher heuristic cannot be validated against an opaque corpus. Either loosen the redaction default for the dispatcher-validation window, or build a separate opt-in instrumentation pass before Phase 3 can ship.
