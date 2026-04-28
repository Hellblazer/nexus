---
title: "RDR-098: Abstract-Question Plan Template — BERTopic Communities as Cheap Substitute for GraphRAG Community Reports"
id: RDR-098
type: Feature
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-28
related_issues: []
related_tests: []
related: [RDR-070, RDR-075, RDR-078, RDR-080, RDR-088, RDR-091, RDR-092, RDR-093, RDR-097]
---

# RDR-098: Abstract-Question Plan Template — BERTopic Communities as Cheap Substitute for GraphRAG Community Reports

This RDR proposes a single new builtin plan template, `abstract-question`, that uses the per-collection BERTopic taxonomy (RDR-070) as a community-partition layer for abstract / summary-shaped questions. The shape — over-fetched vector recall, group-by chunk topic label, per-group summarization, then aggregate — is the second move from the 2026-04-28 RAG-papers research synthesis (T3 IDs `01d3943271e1e716` + `45040ed1bffe64d6`), implementing Proposal C alongside RDR-097's Proposal A. It uses primitives nexus already ships (`search`, `operator_groupby`, `operator_summarize`, `operator_aggregate`); it adds zero dependencies; it lands as plan YAML, plan-matcher dimensions, and integration tests over a populated-taxonomy collection.

The motivating evidence is the Zhou et al. VLDB 2025 unified-framework analysis: their L4 finding reports community-level summaries outperform raw chunk clusters for abstract / summary-shaped QA, and their CheapRAG configuration (community summaries + original chunks, map-reduce) achieves competitive accuracy at a fraction of the cost of full GraphRAG. The mapping to nexus is direct: BERTopic taxonomy centroids per collection are structurally equivalent to GraphRAG's community partitions; the topic label assigned to each chunk is a community-membership label. Validating whether BERTopic communities substitute adequately for expensive LLM-generated community reports is a near-zero-infrastructure experiment because the partitions already exist.

This RDR is intentionally narrow. It is the cheapest of the three proposals from the synthesis. RDR-097 covers Proposal A (hybrid vector + graph retrieval for factual lookup). The cross-encoder salient-sentence aspect (Proposal B, bead nexus-2wc1) is filed separately. The three proposals compose but do not depend on each other; this RDR ships independently.

## Problem Statement

### Gap 1: No builtin plan handles abstract / summary-shaped questions structurally

The 12 builtin plans seeded by `nx catalog setup` cover author-scoped search, type-scoped search, citation traversal, research/analyze/review/document/debug shapes, and several plan-meta operations. None of them are shaped for the question class "what are the main themes in collection X" or "summarize what we know about Y" or "give me an overview of corpus Z". On those questions today the matcher either picks `research-default` (which over-emphasizes specific-fact retrieval and dilutes summary coverage) or falls through to the inline planner. The inline planner produces ad-hoc plans that vary run-to-run and tend to flatten the result set with `operator_generate(template="summary", context=concatenated_chunks)`, which discards the topical structure that the BERTopic taxonomy already encodes.

The structural shape an abstract question wants is map-reduce: partition the candidate evidence into coherent topical clusters, summarize each cluster on its own terms, then reduce the per-cluster summaries into a coherent narrative. That is exactly what the Zhou et al. unified-framework calls a community-report retriever, and it is exactly what `operator_groupby → operator_summarize → operator_aggregate` already implements at the operator level after RDR-093. The missing piece is the plan template that wires those operators together with the right grouping key.

### Gap 2: Intent routing for abstract questions is not differentiated from research questions

The plan matcher (`src/nexus/plans/matcher.py`) ranks candidate plans by cosine similarity over match-text and FTS5 scope filtering, with a `dimensions.verb` filter applied first if the caller pins one. There is no current verb that maps to "abstract / summary / overview" — `research-default` uses `verb=research`, `analyze-default` uses `verb=analyze`, `review-default` uses `verb=review`. The closest existing verbs leak abstract-question traffic into the research plan.

The bead spec (nexus-ldnp) names `_nx_answer_classify_plan` (`src/nexus/mcp/core.py:2715`) as the intent-classification site. That function classifies a *matched* plan into `single_query | retrieval_only | needs_operators` based on its `steps` list — it runs after plan matching, not before. The question-text → plan-shape decision is made by the matcher itself via dimensions and match-text similarity, not by `_nx_answer_classify_plan`. This RDR pins the routing surface explicitly: a new `verb=summarize` dimension on the new plan, with abstract-question phrases seeded into the match-text via RDR-092's hybrid-match-text mechanism, is the actual route. `_nx_answer_classify_plan` itself does not need modification — the new plan's step shape already classifies as `needs_operators` (it carries `operator_*` tools), which routes through the operator-aware execution path.

### Gap 3: BERTopic taxonomy is per-collection; abstract questions are often cross-collection

The BERTopic taxonomy is computed per-collection (RDR-070). A question like "summarize what we know across our knowledge corpora" cannot use a single set of topic labels because each collection has its own. Cross-collection abstract QA would route through the projection layer (RDR-075's `nx taxonomy project`), which is materially more complex — it has to align centroids across collections, weight by per-collection ICF, and surface a unified topic vocabulary. v1 scope of this RDR is single-collection only. Cross-collection abstract retrieval is a follow-up and is named explicitly in §Out of Scope.

### Gap 4: Per-group LLM cost is unbounded if K is not capped

`operator_summarize` invokes `claude_dispatch` once per group. A collection with 46 topics (e.g. `docs__art-grossberg-papers`) would naïvely produce 46 sequential LLM calls before the aggregate step — minutes of latency, dollars of LLM spend per query. The CheapRAG paper handles this by capping K (the number of groups summarized) and selecting top-K by aggregate group score. The plan must pin a K cap and the selection rule explicitly. K=5 is the proposal; the rationale is that abstract questions typically have a short set of dominant themes and the long tail is noise. If the integration test sweep shows K=5 systematically under-covers, the cap is tuned in a follow-up.

### Gap 5: Test corpora suitability — `knowledge__hybridrag` has too few chunks

`knowledge__hybridrag` was used as the corpus for RDR-097's hybrid-retrieval validation. It is unsuitable for this RDR because HDBSCAN found no clusters during BERTopic discover (too few chunks, too uniform). Two corpora are populated and large enough for abstract QA validation: `knowledge__delos` (25+ topics) and `docs__art-grossberg-papers` (46 topics). The integration test fixtures must use one of those, and the corpus choice is a load-bearing decision — running on a sparse-taxonomy collection would produce a vacuous green test.

## Context

### Technical Environment

- **Plan library**: `src/nexus/db/t2/plan_library.py`, the `plans` SQLite table seeded from YAML files at `nx/plans/builtin/*.yml` via the four-tier loader.
- **Plan matching**: `src/nexus/plans/matcher.py` — T1 cosine over match-text + FTS5 fallback, scope-aware after RDR-091, hybrid match-text after RDR-092. Verb dimension filters before similarity ranking.
- **Plan execution**: `src/nexus/plans/runner.py` — dispatches each step's `tool` to the corresponding MCP tool via `_OPERATOR_TOOL_MAP`.
- **Search step**: `src/nexus/search_engine.py:search` — supports per-corpus over-fetch, scope filters, topic pre-filter and grouping. Critically, it tags each result with `_topic_label` (`search_engine.py:674-697`) when the source collection has an active BERTopic taxonomy, so the downstream `operator_groupby` step can group by `_topic_label` without an extra lookup.
- **Group/aggregate operators**: `src/nexus/mcp/core.py:2174` (`operator_groupby`), `:2283` (`operator_aggregate`). Both have a SQL fast path against `document_aspects` (RDR-089) and a `claude_dispatch` fallback. For grouping by `_topic_label`, the fast path does not apply (topic labels are not in `document_aspects`); the fallback path applies and is fit-for-purpose.
- **Summarize operator**: `src/nexus/mcp/core.py:1894` (`operator_summarize`) — Claude-dispatched, optional citations, 300s timeout default.
- **Taxonomy substrate**: `src/nexus/db/t2/catalog_taxonomy.py` — `topics`, `chunk_topic_assignments` tables; per-collection topic_id → label map; ICF computation; hub detection. Populated for 30+ collections per current `nx taxonomy status`.
- **Intent classifier site**: `src/nexus/mcp/core.py:2715` (`_nx_answer_classify_plan`). Per Gap 2 above, this site does not need modification for the new plan — the routing happens through `dimensions.verb` and match-text in the matcher, and the new plan's operator shape correctly classifies as `needs_operators`.

### Source Paper

- **In-depth Analysis of Graph-based RAG in a Unified Framework** (Zhou et al., VLDB 2025) — tumbler **1.653.79** in `knowledge__hybridrag`, arxiv **2503.04338**. The L4 finding reports community-level summaries outperform raw chunk clusters for abstract / summary QA. The CheapRAG configuration (community summaries + original chunks, map-reduce) is competitive with full GraphRAG at materially lower cost. The paper's methodology section pins the map-reduce shape: per-community summary, then a reduce step over the per-community summaries.

### Synthesis Source

Deep-research synthesis at T3 IDs `01d3943271e1e716` + `45040ed1bffe64d6` — three-proposal landing. RDR-097 implements Proposal A (hybrid retrieval). This RDR implements Proposal C (CheapRAG / community-report substitution via BERTopic). Bead nexus-2wc1 covers Proposal B (cross-encoder salient-sentence aspect) and is filed separately.

### Research Findings

**RF-1** (Verified, source `src/nexus/search_engine.py:674-697`): the `search` step tags each result with `_topic_label` when the source collection has an active BERTopic taxonomy. The label is materialized into the result's metadata before the result leaves `search_engine.py`. The downstream `operator_groupby` step can therefore group by `_topic_label` without an extra T2 lookup or a JOIN against `chunk_topic_assignments`.

**RF-2** (Verified, source `src/nexus/mcp/core.py:2174-2279`): `operator_groupby` accepts a natural-language `key` and uses `claude_dispatch` to partition. Passing `key="_topic_label"` works in the LLM-fallback path because each input item carries a `_topic_label` field; the LLM is instructed to use that field directly. SQL fast path does not apply (topic labels are not in `document_aspects`), so `source="auto"` falls through to LLM as expected.

**RF-3** (Verified, source `src/nexus/mcp/core.py:2283-2400`): `operator_aggregate` accepts a JSON-serialized `list[{key_value, items}]` and a natural-language reducer. For the abstract-question shape, the reducer is "synthesize per-topic summaries into a coherent overview answering the original question". The aggregate step's prompt explicitly instructs cross-group synthesis (not group isolation), which is the opposite framing of the per-group summarize step — the plan template must pass the reducer string carefully.

**RF-4** (Verified, source `src/nexus/mcp/core.py:1894-1918`): `operator_summarize` takes a single `content` string and produces a single `summary` string. To summarize K groups, the plan invokes the operator K times — there is no native batch shape. The K=5 cap bounds latency and cost.

**RF-5** (Documented, Zhou et al. VLDB 2025 §6.4): the paper's CheapRAG configuration uses K = top-5 communities by aggregate vector score against the question. The plan replicates this: rank groups by sum of per-item search score, then take top-5. Implementation: between `operator_groupby` and the K-fold `operator_summarize`, a plan-level slice on the groups list ordered by aggregate score.

**RF-6** (Verified, source `nx/plans/builtin/research-default.yml`): the YAML format supports binding-driven step parameterization (`$concept`, `$limit`, `$step1.ids`). The new plan can expose `K`, `over_fetch_limit`, and `score_threshold` as optional bindings with defaults.

**RF-7** (Verified, current `nx taxonomy status`): `knowledge__delos` has 25+ topics and 1k+ chunk assignments; `docs__art-grossberg-papers` has 46 topics and several thousand chunk assignments. `knowledge__hybridrag` has zero topics (HDBSCAN found no clusters; corpus too small/uniform). Validation must use one of the populated collections.

## Decision

Ship one plan template, `abstract-question.yml`, with verb dimension `summarize` and four steps: `search` (over-fetch 40, low threshold) → `operator_groupby` (key `_topic_label`) → top-K slice (K=5) → `operator_summarize` per group → `operator_aggregate` (cross-group synthesis). Validate on `docs__art-grossberg-papers` (46 topics) as primary corpus and `knowledge__delos` as secondary. Defer cross-collection abstract QA (projection-layer routing) and parallel per-group summarization (concurrent `claude_dispatch`) to follow-up beads.

### Why one phase

This is intentionally a minimum-meaningful experiment, mirroring the discipline of RDR-091, RDR-092, and RDR-097: ship the smallest thing that proves the principle. The proposal is "do BERTopic communities substitute adequately for expensive LLM-generated community reports on abstract questions" — a question that can be answered with one plan template, the existing operator surface, and an integration test sweeping 10 abstract questions over two populated-taxonomy collections. If the plan beats `operator_generate`-flat-summarize on quality and is cheaper than naïve all-chunks-in-one-prompt, the principle is proven and follow-ups (parallelization, K tuning, cross-collection projection, salient-sentence pre-filter from nexus-2wc1) get filed against measured evidence rather than speculation. If it doesn't, the cost was one plan YAML and one test file.

### Why `verb=summarize`, not `verb=analyze` or `verb=research`

The matcher's verb dimension is the cheapest disambiguation surface available — pin the verb correctly and the cosine over match-text only needs to disambiguate within a small candidate pool. Existing verbs (`research`, `analyze`, `review`, `document`, `debug`) all carry a different semantic shape: `research` walks evidence chains; `analyze` ranks and compares candidates; `review` audits a change set; `document` cross-references prose to code; `debug` investigates a failing path. None of those are "give me a structured overview". `summarize` is the precise verb for the question class and adds no collision risk to the existing verb space. The plan's match-text seeds abstract-question phrases — "summarize", "overview of", "main themes", "what does X say about Y", "key findings about Z" — so RDR-092's hybrid-match-text matcher routes those questions to this plan.

### Why K=5 and rank-by-aggregate-score

Per RF-5, the Zhou et al. CheapRAG configuration uses K=5 for community-report retrieval. The defensible argument is that abstract questions typically have a short head of dominant themes and a long tail of noise; capping at the head captures the signal while bounding latency at five sequential LLM calls (roughly 30-60s wall-clock for `operator_summarize` against 6-12 chunks per group). Larger K is straightforwardly available as an optional binding. Selection by aggregate per-group search score (sum of cosine scores of items in the group) is the same selection rule as the paper. No invented heuristic.

### Why the `_topic_label` field as group key (not topic_id, not chunk-cluster centroid)

Three reasons. First, `_topic_label` is human-readable, which makes the per-group summary usable as standalone evidence in the aggregate step's prompt. Second, the label is materialized on every search result by `search_engine.py` already (RF-1), so the `operator_groupby` step does not need a T2 lookup or extra plumbing. Third, falling back to `topic_id` (integer) would force the LLM-fallback `operator_groupby` to invent labels per group, which dilutes the partition signal and produces per-group summaries that are harder to compose in the aggregate step. The label-as-key choice is load-bearing.

### Why defer parallel per-group summarization

`operator_summarize` is sequential by construction (one `claude_dispatch` per call). Parallelizing the K=5 calls would require either an `operator_summarize_batch` (new MCP tool) or asyncio.gather at the runner level over a list comprehension of summarize steps. Both are non-trivial and orthogonal to the question this RDR is asking. Phase 1 measures the sequential latency; if that latency is a UX problem, a follow-up bead introduces the parallel path with the latency target as evidence.

### Why defer cross-collection abstract QA

Cross-collection projection (RDR-075) maps each collection's topic vocabulary onto a shared cross-collection topic vocabulary. An abstract question that wants to cover multiple knowledge corpora needs the projection layer to define what "the same theme across collections" means. v1 scope is single-collection. Cross-collection routing — where the plan dispatches to the projection layer for a unified topic vocabulary, then runs the same map-reduce shape over the projected labels — is a Phase 2 question filed as a follow-up bead.

## Phase 1 — Plan Template, Matcher Routing, and Integration Test Harness

One phase, one branch, one PR. Single-phase by design (see "Why one phase" above).

### Prerequisites (mapped to beads)

- **P1.1 — `abstract-question` plan YAML.** Create `nx/plans/builtin/abstract-question.yml` with the four-step shape (`search` → `operator_groupby` → top-K slice → K-fold `operator_summarize` → `operator_aggregate`) and the `summarize` verb dimension. Bindings: `question` (required), `K` (default 5), `over_fetch_limit` (default 40), `score_threshold` (default 0.3). Match-text seeds the abstract-question phrase set. Run `nx catalog setup` against a clean DB to verify it loads without YAML errors. Reference: `nx/plans/builtin/research-default.yml` for structural conventions, `nx/plans/builtin/analyze-default.yml` for the operator-shape conventions. Parent bead: nexus-ldnp.

- **P1.2 — Top-K slice contract for plan-level group selection.** The plan template needs a way to express "take top-K groups by aggregate item score" between `operator_groupby` and the K-fold `operator_summarize`. Options: (a) a new tiny operator `operator_select_top_k(groups, k, score_field)` that does the slice deterministically; (b) inline plan-runner logic for `$stepN.groups[:K]` syntax. Decide during P1.2 implementation. (a) is cleaner, lands the slice as a reusable primitive; (b) is faster but couples plan syntax to a one-shot need. Lean: (a) — the operator surface is the right home and other plans will want top-K group selection.

- **P1.3 — Plan-runner support for K-fold operator dispatch.** The K-fold `operator_summarize` step (one summarize per group) requires the plan runner to fan out a single template step into K parallel-or-sequential calls. Options: (a) explicit per-iteration unrolling in plan YAML (hard-coded 5 step entries with `$stepN.groups[i]` references); (b) a new step `tool` (`for_each` or `map`) that takes a list and a sub-step template; (c) drive sequential summarize calls from a single bundled `claude_dispatch` (one prompt does all 5 summaries in one LLM call). Decide during P1.3 implementation. Lean: (c) — the bundled-dispatch path is what `operator_groupby → operator_aggregate` already does at the runner level (RDR-093 C-1), and a one-shot bundled summarize prompt is well-shaped for the LLM. Risk: the bundled prompt may exceed token budget on large groups; if the integration test sweeps show that, fall back to (a) or (b) in a follow-up.

- **P1.4 — Integration test harness.** Add `tests/test_abstract_question_plan.py` with 10 abstract-question fixtures over `docs__art-grossberg-papers` (primary, 46 topics) and `knowledge__delos` (secondary, 25+ topics). Each fixture: question string, expected coverage of 3-5 dominant themes (declared as topic-label strings or substrings the test asserts present in the aggregate output). The test runs `abstract-question` and a baseline plan (flat `search` + `operator_generate(template="summary")`). Records full inputs/outputs to T2 telemetry for diffability. Asserts: (a) the abstract-question plan runs to completion; (b) its aggregate output covers at least 80% of the declared dominant themes; (c) the baseline plan covers measurably fewer themes (recorded, not gated — quality differential is the headline finding, not the test gate). LLM-judge rubric is documented at the top of the test file but not used in the asserted gate (LLM-judge stability is a separate concern).

- **P1.5 — Match-text hygiene check.** RDR-092's hybrid-match-text mechanism is sensitive to overly-broad match-text (RDR-090 spike found plan #67 over-broad on "tumblers"). The new plan's match-text must be tight enough that abstract-question queries route to it but specific-fact queries do not. The test harness includes 3 sanity-check fixtures that are *factual* questions (e.g., "what year did Grossberg publish ART2") — those should NOT match `abstract-question` (matcher confidence should be below threshold or `research-default` should win). Asserts: factual-question fixtures match a non-`abstract-question` plan with > 0.5 confidence.

- **P1.6 — Documentation.** Add a comment block at the top of `abstract-question.yml` documenting the K cap rationale, the per-group-cost tradeoff, and the single-collection scope limitation. Update `docs/architecture.md` plan-library section to list the new template. Add a one-paragraph note to RDR-070's "downstream consumers" section pointing at this RDR.

### Success Metrics

- `nx catalog setup` seeds 13 templates (12 existing + 1 new) without YAML errors.
- The new plan matches successfully via `plan_match` for the 10 abstract-question fixtures (recorded `match_count` increments after the integration test run).
- Aggregate output for `docs__art-grossberg-papers` fixtures covers at least 80% of declared dominant themes (theme-coverage metric, not LLM-judge).
- Latency P50 for the K-fold path is recorded as a baseline number; no regression assertion vs. the baseline plan beyond "completes within the 300s timeout per `claude_dispatch`".
- Match-text sanity-check fixtures (factual questions) do NOT route to `abstract-question` — they route to a different plan with > 0.5 confidence.
- Bundled vs. unrolled K-fold summarize decision (P1.3) recorded with the prompt-token-count and latency evidence that drove it.

### Out of Scope (intentional, not deferrals)

- **Cross-collection abstract QA.** Cross-collection routing through the RDR-075 projection layer is a Phase 2 question filed as a follow-up bead.
- **Parallel per-group summarization.** Phase 1 ships sequential or bundled-prompt K-fold; if latency is a UX problem, a follow-up bead introduces parallel `claude_dispatch`.
- **Cross-encoder salient-sentence pre-filter.** Bead nexus-2wc1 (Proposal B from the synthesis) is the home for that work; it composes with this RDR but is independent.
- **K tuning per corpus class.** Phase 1 ships K=5 from RF-5; per-corpus tuning is a follow-up if the integration test sweep shows large per-corpus variance.
- **LLM-judge as the gate metric.** LLM-judge stability is a separate research question; Phase 1 uses theme-coverage as the asserted gate and reports LLM-judge scores as informational output.

## Risks and Mitigations

- **Risk: `_topic_label` is empty when the source collection has no active taxonomy.** Mitigation: the plan adds a guard step that checks `_topic_label` presence on at least 50% of search results; if not, it falls through to the baseline `search` + `operator_generate` path (graceful degradation). The fall-through is logged so the user knows their corpus is ill-suited to the plan.
- **Risk: K=5 systematically under-covers themes on diverse corpora.** Mitigation: K is an optional binding; the integration test sweeps K=3, K=5, K=10 and records the better one; we don't pretend Phase 1 picked the global optimum.
- **Risk: The bundled K-fold summarize prompt exceeds Claude's token budget on large groups.** Mitigation: P1.3 measures prompt size during integration tests; if any fixture exceeds budget the fallback to unrolled per-group dispatch lands in Phase 1 itself rather than as a follow-up.
- **Risk: Match-text overly broad — abstract-question phrases collide with research-default phrases.** Mitigation: P1.5 sanity-check fixtures (3 factual-question routing tests) catch this. If they fail, match-text is tightened and a tight-vs-broad pair is recorded for future plans.
- **Risk: BERTopic communities are sub-optimal partitions for some abstract questions.** Mitigation: the integration test reports the per-fixture theme-coverage delta; if the BERTopic-community shape underperforms baseline on a class of questions, we have evidence to invest in alternative partitions (LDA, doc-level metadata clusters) in a follow-up rather than guessing now.
- **Risk: Per-group LLM cost is still too high for casual use even at K=5.** Mitigation: the plan exposes `K` as a binding; the doctor / CLI can warn when K is high and a query is wide. Cost reporting per `nx_answer` run already captures this (RDR-080's `cost_usd` column in `nx_answer_runs`).

## Open Questions

1. **K=5 cap rationale vs. corpus diversity.** Phase 1 ships K=5 from the paper's CheapRAG configuration. Per-corpus tuning is a follow-up if the test sweep shows large variance. If a single-K cap is structurally wrong, a follow-up explores adaptive K (e.g., elbow rule on group score distribution).
2. **K-fold summarize execution shape.** Bundled prompt (one `claude_dispatch` for all K summaries) vs. unrolled (K calls). Decided during P1.3 implementation with measured prompt-size and latency evidence.
3. **Top-K group selection primitive.** New `operator_select_top_k` operator vs. inline plan-runner slice syntax. Decided during P1.2 implementation. Lean: new operator.
4. **Aggregate-step framing — overview vs. answer.** The aggregate step can be framed as "synthesize per-topic summaries into an overview" or "answer the original question using the per-topic summaries as evidence". The first is more abstract-y, the second is more grounded. Phase 1 ships the second (more conservative); follow-up may pin a per-question-type choice.
5. **Cross-collection scope-up.** Cross-collection abstract QA is named explicitly out of scope. Open question: when does this become important enough to invest in a projection-layer-aware variant of this plan? Answer is volume-driven; no Phase 1 decision required.
6. **Composition with cross-encoder salient-sentence aspect (nexus-2wc1).** When that aspect lands, the per-group `operator_summarize` step could pre-filter chunks to only the salient sentences. The integration test fixtures should be designed to be re-runnable post-composition so the differential value is measurable.

## References

### Source Paper

- Zhou, S. et al. (2025). *In-depth Analysis of Graph-based RAG in a Unified Framework.* arXiv:**2503.04338**, VLDB 2025. Tumbler **1.653.79** in `knowledge__hybridrag`. L4 finding: community-level summaries outperform raw chunk clusters for abstract / summary QA. CheapRAG configuration (community summaries + original chunks, map-reduce, K=5) achieves competitive accuracy at far lower cost than full GraphRAG. §6.4 documents the K=5 selection rule.

### Synthesis Source

Deep-research synthesis at T3 IDs `01d3943271e1e716` + `45040ed1bffe64d6` — three-proposal landing. This RDR implements Proposal C; RDR-097 implements Proposal A; bead nexus-2wc1 covers Proposal B.

### Nexus Modules

- `nx/plans/builtin/` — plan template location; new `abstract-question.yml` lands here.
- `src/nexus/db/t2/plan_library.py` — plan storage, the `plans` table, four-tier loader.
- `src/nexus/plans/matcher.py` — verb-dimension filter + match-text cosine ranking.
- `src/nexus/plans/runner.py` — step dispatch, `_OPERATOR_TOOL_MAP`, K-fold execution shape (P1.3).
- `src/nexus/search_engine.py` — `_topic_label` materialization at lines 674-697; over-fetch and scope-filter surface.
- `src/nexus/db/t2/catalog_taxonomy.py` — `topics`, `chunk_topic_assignments`, ICF computation; the substrate `_topic_label` reads from.
- `src/nexus/mcp/core.py` — `operator_groupby` (line 2174), `operator_aggregate` (line 2283), `operator_summarize` (line 1894); intent-classification site `_nx_answer_classify_plan` (line 2715, no modification needed per Gap 2).
- `src/nexus/operators/dispatch.py` — `claude_dispatch` substrate for the K-fold summarize step.
- `src/nexus/commands/catalog.py:_seed_plan_templates` — the seeding entry point.

### Related RDRs

- **RDR-070** Incremental Taxonomy & Clustered Search — the BERTopic taxonomy substrate this plan reads. The community-partition primitive originates here.
- **RDR-075** Cross-Collection Topic Projection — the projection layer cross-collection abstract QA would route through. Out of scope for this RDR's Phase 1.
- **RDR-078** Plan-Centric Retrieval — semantic plan matching, typed-graph traversal, scenario plans.
- **RDR-080** Retrieval Layer Consolidation — `nx_answer` as the canonical retrieval entry point; the `nx_answer_runs` cost-reporting surface this plan participates in.
- **RDR-088** AgenticScholar Operator-Set Completion — the operator surface this plan composes from.
- **RDR-091** Scope-Aware Plan Matching — the matcher behavior this plan's `dimensions` participate in.
- **RDR-092** Plan Match-Text from Dimensional Identity — the hybrid match-text mechanism the new plan's match-text uses.
- **RDR-093** GroupBy and Aggregate Operators — the canonical filter→groupby→aggregate pipeline this plan uses; the inline-items contract (C-1 finding) the bundled execution path depends on.
- **RDR-097** Hybrid Retrieval Plan Template — sibling RDR implementing Proposal A from the same synthesis. Independent and composable.
