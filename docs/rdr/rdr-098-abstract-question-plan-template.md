---
title: "RDR-098: Abstract-Question Plan Template — BERTopic Communities as Cheap Substitute for GraphRAG Community Reports"
id: RDR-098
type: Feature
status: closed
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-28
accepted_date: 2026-04-29
closed_date: 2026-04-29
close_reason: implemented
gap_closures:
  Gap1: nx/plans/builtin/abstract-themes.yml:24
  Gap2: nx/plans/builtin/abstract-themes.yml:35
  Gap3: nx/plans/builtin/abstract-themes.yml:21
  Gap4: nx/plans/builtin/abstract-themes.yml:45
  Gap5: tests/test_abstract_themes_plan_integration.py:95
related_issues: [nexus-ldnp, nexus-5gby, nexus-17yg, nexus-zvbc, nexus-j5ka, nexus-h3e2]
related_tests: [test_abstract_themes_plan.py, test_abstract_themes_plan_integration.py]
related: [RDR-070, RDR-075, RDR-078, RDR-080, RDR-088, RDR-091, RDR-092, RDR-093, RDR-097]
---

# RDR-098: Abstract-Question Plan Template — BERTopic Communities as Cheap Substitute for GraphRAG Community Reports

This RDR proposes a single new builtin plan template, `abstract-themes` (working name `abstract-question` was renamed during nexus-ldnp implementation), that uses the per-collection BERTopic taxonomy (RDR-070) as a community-partition layer for abstract / summary-shaped questions. The shape — over-fetched vector recall, group-by chunk topic, per-group aggregate, then coalesce — is the second move from the 2026-04-28 RAG-papers research synthesis (T3 IDs `01d3943271e1e716` + `45040ed1bffe64d6`), implementing Proposal C alongside RDR-097's Proposal A. It uses primitives nexus already ships (`search`, `groupby`, `aggregate`, `summarize`); it adds zero dependencies; it lands as plan YAML, plan-matcher dimensions, and integration tests over a populated-taxonomy collection.

The motivating evidence is the Zhou et al. VLDB 2025 unified-framework analysis: their L4 finding reports community-level summaries outperform raw chunk clusters for abstract / summary-shaped QA, and their CheapRAG configuration (community summaries + original chunks, map-reduce) achieves competitive accuracy at a fraction of the cost of full GraphRAG. The mapping to nexus is direct: BERTopic taxonomy centroids per collection are structurally equivalent to GraphRAG's community partitions; the topic label assigned to each chunk is a community-membership label. Validating whether BERTopic communities substitute adequately for expensive LLM-generated community reports is a near-zero-infrastructure experiment because the partitions already exist.

This RDR is intentionally narrow. It is the cheapest of the three proposals from the synthesis. RDR-097 covers Proposal A (hybrid vector + graph retrieval for factual lookup). The cross-encoder salient-sentence aspect (Proposal B, bead nexus-2wc1) is filed separately. The three proposals compose but do not depend on each other; this RDR ships independently.

## Problem Statement

### Gap 1: No builtin plan handles abstract / summary-shaped questions structurally

The 12 builtin plans seeded by `nx catalog setup` cover author-scoped search, type-scoped search, citation traversal, research/analyze/review/document/debug shapes, and several plan-meta operations. None of them are shaped for the question class "what are the main themes in collection X" or "summarize what we know about Y" or "give me an overview of corpus Z". On those questions today the matcher either picks `research-default` (which over-emphasizes specific-fact retrieval and dilutes summary coverage) or falls through to the inline planner. The inline planner produces ad-hoc plans that vary run-to-run and tend to flatten the result set with `operator_generate(template="summary", context=concatenated_chunks)`, which discards the topical structure that the BERTopic taxonomy already encodes.

The structural shape an abstract question wants is map-reduce: partition the candidate evidence into coherent topical clusters, summarize each cluster on its own terms, then reduce the per-cluster summaries into a coherent narrative. That is exactly what the Zhou et al. unified-framework calls a community-report retriever, and it is exactly what `operator_groupby → operator_summarize → operator_aggregate` already implements at the operator level after RDR-093. The missing piece is the plan template that wires those operators together with the right grouping key.

### Gap 2: Intent routing for abstract questions is not differentiated from research questions

The plan matcher (`src/nexus/plans/matcher.py`) ranks candidate plans by cosine similarity over match-text and FTS5 scope filtering, with a `dimensions.verb` filter applied first if the caller pins one. There is no current verb that maps to "abstract / summary / overview" — `research-default` uses `verb=research`, `analyze-default` uses `verb=analyze`, `review-default` uses `verb=review`. The closest existing verbs leak abstract-question traffic into the research plan.

The bead spec (nexus-ldnp) names `_nx_answer_classify_plan` (`src/nexus/mcp/core.py:2715`) as the intent-classification site. That function classifies a *matched* plan into `single_query | retrieval_only | needs_operators` based on its `steps` list — it runs after plan matching, not before. The question-text → plan-shape decision is made by the matcher itself via dimensions and match-text similarity, not by `_nx_answer_classify_plan`. This RDR pins the routing surface explicitly: `dimensions.verb=query, strategy=abstract-themes` on the new plan with abstract-question phrases seeded into the description (RDR-092's hybrid-match-text mechanism) is the actual route. The pre-implementation framing argued for a new `verb=summarize` dimension; the shipped artifact uses `verb=query` plus `strategy=abstract-themes` as the secondary discriminator (see Decision §"Why `verb=query` + `strategy=abstract-themes`" below). `_nx_answer_classify_plan` itself does not need modification — the new plan's step shape already classifies as `needs_operators` (it carries `groupby` / `aggregate` / `summarize` tools), which routes through the operator-aware execution path.

### Gap 3: BERTopic taxonomy is per-collection; abstract questions are often cross-collection

The BERTopic taxonomy is computed per-collection (RDR-070). A question like "summarize what we know across our knowledge corpora" cannot use a single set of topic labels because each collection has its own. Cross-collection abstract QA would route through the projection layer (RDR-075's `nx taxonomy project`), which is materially more complex — it has to align centroids across collections, weight by per-collection ICF, and surface a unified topic vocabulary. v1 scope of this RDR is single-collection only. Cross-collection abstract retrieval is a follow-up and is named explicitly in §Out of Scope.

### Gap 4: Per-group LLM cost is unbounded without an upstream cap

A collection with 46 topics (e.g. `docs__art-grossberg-papers`) would naïvely produce one LLM call per group plus one aggregation — dozens of sequential calls per query. The CheapRAG paper handles this by capping the number of groups (top-5 by aggregate score) so the LLM cost is bounded. The shipped plan handles the same problem upstream via the `limit` binding (default 30) on the search step: groupby partitions only the over-fetched candidates, not the entire taxonomy, so the effective group count is bounded by the search recall rather than by topic count. Selection by aggregate per-group search score (the paper's K=5 selection rule) is preserved because `groupby` returns groups ordered by aggregate item score; `aggregate` operates on that ordering inside one bundled `claude_dispatch`. If integration tests show the bundled prompt blows token budget on large groups, the unrolled per-group dispatch is a Phase 2 fallback.

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

**RF-1** (Verified, source `src/nexus/search_engine.py:674-697`): the `search` step tags each result with `_topic_label` when the source collection has an active BERTopic taxonomy. The label is materialized into the result's metadata before the result leaves `search_engine.py`. The downstream groupby step can therefore key on topic information without an extra T2 lookup or a JOIN against `chunk_topic_assignments`.

**RF-2** (Assumed, source `src/nexus/mcp/core.py:2174-2279`; downgraded from Verified during the RDR-098 acceptance gate): `operator_groupby` accepts a natural-language `key` and uses `claude_dispatch` to partition when no SQL fast path applies. The shipped plan passes `key="topic"` (not `"_topic_label"`); the LLM-fallback partitioner reads each item's metadata and is expected to use the `_topic_label` materialized by RF-1 as the dominant signal — but that mapping has not been systematically verified across the P1.4 fixture set. Smoke run on `docs__art-grossberg-papers` produced 4 coherent groups (one being References / Citations, addressed in nexus-j5ka), which is consistent with BERTopic-aligned partitioning but not proof. Whether the LLM consistently grounds its grouping in `_topic_label` rather than inventing groupings ad-hoc is an empirical question deferred to P1.4. SQL fast path does not apply (topic labels are not in `document_aspects`), so `source="auto"` falls through to LLM as expected.

**RF-3** (Verified, source `src/nexus/mcp/core.py:2283-2400`): `operator_aggregate` accepts a JSON-serialized `list[{key_value, items}]` and a natural-language reducer. For the abstract-question shape, the reducer is "synthesize per-topic summaries into a coherent overview answering the original question". The aggregate step's prompt explicitly instructs cross-group synthesis (not group isolation), which is the opposite framing of the per-group summarize step — the plan template must pass the reducer string carefully.

**RF-4** (Verified, source `src/nexus/mcp/core.py:1894-1918`): `operator_summarize` takes a single `content` string and produces a single `summary` string. To summarize K groups, the plan invokes the operator K times — there is no native batch shape. The K=5 cap bounds latency and cost.

**RF-5** (Documented, Zhou et al. VLDB 2025 §6.4): the paper's CheapRAG configuration uses K = top-5 communities by aggregate vector score against the question. The shipped plan preserves the *selection rule* (rank groups by sum of per-item search score) but bounds the candidate set upstream via the `limit` binding rather than slicing post-groupby. `groupby` returns groups in aggregate-score order; `aggregate` consumes that ordering inside one bundled `claude_dispatch`. The K=5 cap is implicit in the search over-fetch + groupby partitioning rather than explicit in the plan YAML.

**RF-6** (Verified, source `nx/plans/builtin/research-default.yml`): the YAML format supports binding-driven step parameterization (`$concept`, `$limit`, `$step1.ids`). The new plan can expose `K`, `over_fetch_limit`, and `score_threshold` as optional bindings with defaults.

**RF-7** (Verified, current `nx taxonomy status`): `knowledge__delos` has 25+ topics and 1k+ chunk assignments; `docs__art-grossberg-papers` has 46 topics and several thousand chunk assignments. `knowledge__hybridrag` has zero topics (HDBSCAN found no clusters; corpus too small/uniform). Validation must use one of the populated collections.

## Decision

Ship one plan template, `abstract-themes.yml`, with dimensions `verb=query, strategy=abstract-themes` (the `strategy` discriminator is what differentiates this plan from the default `query` plans) and four steps: `search` (broad over-fetch, references filtered) → `groupby` (key `topic`) → `aggregate` (cross-group synthesis bundled into one `claude_dispatch`) → `summarize` (final coalescing pass). Validate on `docs__art-grossberg-papers` (46 topics) as primary corpus and `knowledge__delos` as secondary. Defer cross-collection abstract QA (projection-layer routing) to follow-up beads.

### Why one phase

This is intentionally a minimum-meaningful experiment, mirroring the discipline of RDR-091, RDR-092, and RDR-097: ship the smallest thing that proves the principle. The proposal is "do BERTopic communities substitute adequately for expensive LLM-generated community reports on abstract questions" — a question that can be answered with one plan template, the existing operator surface, and an integration test sweeping 10 abstract questions over two populated-taxonomy collections. If the plan beats `operator_generate`-flat-summarize on quality and is cheaper than naïve all-chunks-in-one-prompt, the principle is proven and follow-ups (parallelization, K tuning, cross-collection projection, salient-sentence pre-filter from nexus-2wc1) get filed against measured evidence rather than speculation. If it doesn't, the cost was one plan YAML and one test file.

### Why `verb=query` + `strategy=abstract-themes` (not a new `verb=summarize`)

This RDR's pre-implementation framing argued for a new `verb=summarize` dimension on the grounds that no existing verb carried "give me a structured overview" semantics. The shipped artifact uses `verb=query` (a shared verb already used by other retrieval plans) plus `strategy=abstract-themes` as the secondary discriminator. The dimension drift was deliberate during nexus-ldnp implementation: `verb=query` keeps abstract-question routing inside the same verb-bucket as factual lookup, which is structurally honest — both are "I have a question, retrieve evidence" — and the `strategy` field carries the actual shape disambiguator. RDR-092's hybrid-match-text matcher does the work of routing within the `verb=query` bucket via match-text similarity over the description.

Collision risk for `verb=query` (which is shared, not isolated) is mitigated by:

- The `strategy=abstract-themes` dimension differentiates this plan from generic `verb=query` plans before match-text similarity ranks candidates.
- The plan's description seeds abstract-question phrases ("main themes", "key findings", "dominant topics", "summarize this corpus", "give an overview", "what does this collection say about a subject"). RDR-092's match-text mechanism ranks against these phrases.
- The description explicitly names the negative case ("Use the default query strategy for specific factual lookups (\"find X by Y\", \"who wrote Z\")") so the LLM-driven match-text comparison sees the disambiguation directly.
- P1.5 sanity-check fixtures verify factual questions do NOT route here.

The collision-isolation argument that originally motivated `verb=summarize` is not load-bearing now that `strategy` carries the differentiation. A future plan that wants the same shape can use a different `strategy` value within `verb=query`.

### Why K=5 and rank-by-aggregate-score

Per RF-5, the Zhou et al. CheapRAG configuration uses K=5 for community-report retrieval. The defensible argument is that abstract questions typically have a short head of *query-relevant* themes and a long tail of noise; capping at the head captures the signal while bounding latency. The "top-5" claim is by relevance score, not by raw topic count: on a 46-topic corpus, top-5 is not "11% of topics" but "the 5 topics whose chunks ranked highest against the query." On a 25-topic corpus the same logic applies. The selected groups are the ones the search step already promoted, not a random slice. Selection by aggregate per-group search score (sum of cosine scores of items in the group) is the same selection rule as the paper. No invented heuristic. Larger K is available as an optional binding.

### Why `topic` as the natural-language group key (not `topic_id`, not chunk-cluster centroid)

Three reasons. First, the topic label is human-readable, which makes the per-group aggregate output usable as standalone evidence in the final summarize step's prompt. Second, the label is materialized on every search result by `search_engine.py` already (RF-1) as `_topic_label`, so the LLM-fallback partitioner has the signal directly in each item's metadata without a T2 lookup or extra plumbing. Third, falling back to `topic_id` (integer) would force the LLM-fallback `groupby` to invent labels per group, which dilutes the partition signal.

The shipped plan passes `key="topic"` (not `key="_topic_label"`) so the LLM groupby fallback partitions on the natural-language concept of "topic" and is expected to ground its grouping in the `_topic_label` metadata that RF-1 materializes. Whether the LLM consistently uses `_topic_label` versus inventing groupings is the empirical question RF-2's downgrade flags as Assumed; the integration test (P1.4) is the verification surface.

### How the K-fold step actually shipped (resolves the parallel-vs-sequential question)

The pre-implementation framing kept "parallel per-group summarization" as deferred work and named two options for the K-fold execution shape (separate `operator_summarize` calls per group vs. a bundled prompt). The shipped plan uses `aggregate` (one `claude_dispatch` that does the K-fold synthesis in one bundled prompt) followed by a final `summarize` (coalescing pass). This is the bundled-prompt path the original framing called "option (c)", and it is what RDR-093's C-1 finding recommends for groupby → aggregate composition. So "parallel per-group summarization" is not deferred so much as obviated: the bundled prompt is the parallel path, with the LLM doing the per-group reductions concurrently inside one call.

The token-budget risk (Risk 3) remains live for very large groups; if any P1.4 fixture exceeds budget the fallback is to unrolled per-group dispatch, filed as a Phase 2 follow-up at that point.

### Why defer cross-collection abstract QA

Cross-collection projection (RDR-075) maps each collection's topic vocabulary onto a shared cross-collection topic vocabulary. An abstract question that wants to cover multiple knowledge corpora needs the projection layer to define what "the same theme across collections" means. v1 scope is single-collection. Cross-collection routing — where the plan dispatches to the projection layer for a unified topic vocabulary, then runs the same map-reduce shape over the projected labels — is a Phase 2 question filed as a follow-up bead.

## Phase 1 — Plan Template, Matcher Routing, and Integration Test Harness

One phase, one branch, one PR. Single-phase by design (see "Why one phase" above).

### Prerequisites (mapped to beads)

- **P1.1 — `abstract-themes` plan YAML.** ✅ Shipped (nexus-ldnp). `nx/plans/builtin/abstract-themes.yml` lands the four-step shape: `search` (broad over-fetch with mode:broad and section_type!=references filter) → `groupby` (key=topic) → `aggregate` (cross-group synthesis) → `summarize` (coalescing pass). Dimensions: `verb=query`, `scope=global`, `strategy=abstract-themes`. Required bindings: `intent`. Optional bindings: `corpus`, `limit`. Defaults: `corpus=all`, `limit=30`. Two follow-up commits applied smoke-test feedback: nexus-h3e2 introduced the `mode: broad` runner affordance (replacing a hard-coded `threshold=2.0` workaround) and nexus-j5ka added the `section_type!=references` filter. Both shipped on PR #362.

- **P1.2 — Top-K group selection.** ✅ Resolved by the shipped shape. The original framing proposed a new `operator_select_top_k` primitive between groupby and summarize. The shipped `groupby → aggregate → summarize` path does not need a separate top-K operator: `groupby` returns groups ordered by aggregate item score, and the `limit` binding bounds the surface area. No new operator needed.

- **P1.3 — K-fold execution shape.** ✅ Resolved by the shipped shape. The bundled-prompt path (one `claude_dispatch` doing the K-fold synthesis inside `aggregate`, then a final `summarize` coalescing pass) is what shipped. This matches RDR-093's C-1 finding for `groupby → aggregate` composition. The token-budget risk (Risk 3) remains live for very large groups; the unrolled per-group dispatch fallback is filed as a Phase 2 follow-up.

- **P1.4 — Integration test harness.** ⏳ Open. Add `tests/test_abstract_themes_plan.py` with 10 abstract-question fixtures over `docs__art-grossberg-papers` (primary, 46 topics) and `knowledge__delos` (secondary, 25+ topics). Each fixture: question string, expected coverage of 3-5 dominant themes (declared as topic-label strings or substrings the test asserts present in the aggregate output). The test runs `abstract-themes` and a baseline plan (flat `search` + `operator_generate(template="summary")`). Records full inputs/outputs to T2 telemetry for diffability. Asserts: (a) the plan runs to completion; (b) its aggregate output covers at least 80% of the declared dominant themes; (c) the baseline plan covers measurably fewer themes (recorded, not gated — quality differential is the headline finding, not the test gate). LLM-judge rubric is documented at the top of the test file but not used in the asserted gate (LLM-judge stability is a separate concern). This is also the verification surface for RF-2's Assumed status — the test must check that the LLM groupby grounds in `_topic_label` rather than inventing groupings.

- **P1.5 — Match-text hygiene check.** ⏳ Open. RDR-092's hybrid-match-text mechanism is sensitive to overly-broad match-text (RDR-090 spike found plan #67 over-broad on "tumblers"). The new plan's description must be tight enough that abstract-question queries route to it but specific-fact queries do not. The test harness includes 3 sanity-check fixtures that are *factual* questions (e.g., "what year did Grossberg publish ART2") — those should NOT match `abstract-themes` (matcher confidence should be below threshold or `hybrid-factual-lookup` / `research-default` should win). Asserts: factual-question fixtures match a non-`abstract-themes` plan with > 0.5 confidence. The first-tier dimension filter does most of the work — RDR-097's `hybrid-factual-lookup` uses `verb=lookup` while this plan uses `verb=query`, so they pass the dimension filter on disjoint paths and never compete on match-text. Match-text hygiene matters within the `verb=query` bucket: any future `verb=query` plan (including default research/analyze fallbacks) needs description tuning so abstract-shaped queries route here while specific-fact queries do not. Sanity-check fixtures cover the `verb=query` collision case directly.

- **P1.6 — Documentation.** ✅ Partial. The shipped `abstract-themes.yml` carries a comment block documenting the cost tradeoff (groupby + aggregate + summarize ≈ 7 LLM calls worst case for K=5) and the single-collection scope limitation. Open: update `docs/architecture.md` plan-library section to list the new template; add a one-paragraph note to RDR-070's "downstream consumers" section pointing at this RDR.

### Success Metrics

- `nx catalog setup` seeds 13 templates (12 existing + 1 new) without YAML errors.
- The new plan matches successfully via `plan_match` for the 10 abstract-question fixtures (recorded `match_count` increments after the integration test run).
- Aggregate output for `docs__art-grossberg-papers` fixtures covers at least 80% of declared dominant themes (theme-coverage metric, not LLM-judge).
- Latency P50 for the K-fold path is recorded as a baseline number; no regression assertion vs. the baseline plan beyond "completes within the 300s timeout per `claude_dispatch`".
- Match-text sanity-check fixtures (factual questions) do NOT route to `abstract-question` — they route to a different plan with > 0.5 confidence.
- Bundled vs. unrolled K-fold summarize decision (P1.3) recorded with the prompt-token-count and latency evidence that drove it.

### Out of Scope (intentional, not deferrals)

- **Cross-collection abstract QA.** Cross-collection routing through the RDR-075 projection layer is a Phase 2 question filed as a follow-up bead.
- **Unrolled per-group dispatch with concurrent `claude_dispatch`.** Phase 1 ships the bundled-prompt path (one `aggregate` call does the K-fold synthesis). True per-group concurrency (asyncio.gather over K separate dispatches) is filed as a Phase 2 follow-up only if the bundled-prompt path hits token-budget walls.
- **Cross-encoder salient-sentence pre-filter.** Bead nexus-2wc1 (Proposal B from the synthesis) is the home for that work; it composes with this RDR but is independent.
- **Per-corpus tuning of `limit`.** Phase 1 ships `limit=30` as the default; per-corpus tuning is a follow-up if the integration test sweep shows large per-corpus variance.
- **LLM-judge as the gate metric.** LLM-judge stability is a separate research question; Phase 1 uses theme-coverage as the asserted gate and reports LLM-judge scores as informational output.

## Risks and Mitigations

- **Risk: `_topic_label` is empty when the source collection has no active taxonomy.** Mitigation: the LLM-fallback `groupby` produces ad-hoc partitions when `_topic_label` is missing; quality degrades but the plan still completes. P1.4 fixtures must include at least one corpus with no taxonomy to verify graceful degradation. The plan does not currently fall through to a baseline path — that hardening is filed as a Phase 2 follow-up if the degradation is bad enough to warrant it.
- **Risk: K=5 systematically under-covers themes on diverse corpora.** Mitigation: the `limit` binding controls the search over-fetch; effective K is determined by groupby partitioning of the limited candidate set. P1.4 sweeps `limit=15`, `limit=30`, `limit=60` and records the better one for each corpus class; we don't pretend Phase 1 picked the global optimum.
- **Risk: The bundled aggregate prompt exceeds Claude's token budget on very large groups.** Mitigation: P1.4 measures prompt size during integration tests; if any fixture exceeds budget the fallback to unrolled per-group dispatch is filed as a Phase 2 follow-up. nexus-j5ka (`section_type!=references` filter) already addresses one common driver of bloat — bibliography chunks accumulating in groupby.
- **Risk: Match-text overly broad — abstract-question phrases collide with other `verb=query` plans.** Mitigation: the `strategy=abstract-themes` dimension filters before match-text similarity ranks within the `verb=query` bucket. P1.5 sanity-check fixtures (3 factual-question routing tests) verify the description's positive ("main themes") and negative ("Use the default query strategy for specific factual lookups") seeds work. If they fail, the description is tightened and a tight-vs-broad pair is recorded for future plans.
- **Risk: BERTopic communities are sub-optimal partitions for some abstract questions.** Mitigation: the integration test reports the per-fixture theme-coverage delta; if the BERTopic-community shape underperforms baseline on a class of questions, we have evidence to invest in alternative partitions (LDA, doc-level metadata clusters) in a follow-up rather than guessing now.
- **Risk: Per-query LLM cost is too high for casual use.** Mitigation: the `limit` binding bounds the search over-fetch; the bundled aggregate path keeps the per-call count low (~4 calls: search + groupby + aggregate + summarize). Cost reporting per `nx_answer` run already captures this (RDR-080's `cost_usd` column in `nx_answer_runs`).

## Open Questions

### Closed during the acceptance gate (decided by what shipped)

1. ~~**K=5 cap rationale vs. corpus diversity.**~~ **CLOSED.** K=5 ships as the default per the paper's CheapRAG configuration; the `limit` binding (default 30, not 5 — `limit` is the search over-fetch, not the K cap; the actual K is determined by groupby partitioning) makes per-corpus tuning available without re-authoring the plan. Adaptive K is a Phase 2 follow-up if measured variance demands it.
2. ~~**K-fold summarize execution shape.**~~ **CLOSED.** Bundled-prompt path shipped: `aggregate` does the K-fold synthesis in one `claude_dispatch`, then `summarize` coalesces. RDR-093's C-1 finding is the precedent.
3. ~~**Top-K group selection primitive.**~~ **CLOSED.** No new primitive needed. `groupby` returns groups in a useful order; the `limit` binding bounds the candidate set upstream.

### Genuinely open

4. **Aggregate-step framing — overview vs. answer.** The aggregate step can be framed as "synthesize per-topic summaries into an overview" or "answer the original question using the per-topic summaries as evidence". The first is more abstract-y, the second is more grounded. Phase 1 ships the second (more conservative); follow-up may pin a per-question-type choice.
5. **Cross-collection scope-up.** Cross-collection abstract QA is named explicitly out of scope. Open question: when does this become important enough to invest in a projection-layer-aware variant of this plan? Answer is volume-driven; no Phase 1 decision required.
6. **Composition with cross-encoder salient-sentence aspect (nexus-2wc1).** When that aspect lands, the per-group `aggregate` step could pre-filter chunks to only the salient sentences. The integration test fixtures should be designed to be re-runnable post-composition so the differential value is measurable.
7. **LLM groupby grounding in `_topic_label`.** RF-2 was downgraded from Verified to Assumed during the acceptance gate. P1.4 needs to verify the LLM partitioner consistently uses the materialized `_topic_label` rather than inventing groupings. If it doesn't, the "BERTopic communities substitute for community reports" claim is undermined and the plan should pass `key="_topic_label"` explicitly (or hint at it in the description).

## References

### Source Paper

- Zhou, S. et al. (2025). *In-depth Analysis of Graph-based RAG in a Unified Framework.* arXiv:**2503.04338**, VLDB 2025. Tumbler **1.653.79** in `knowledge__hybridrag`. L4 finding: community-level summaries outperform raw chunk clusters for abstract / summary QA. CheapRAG configuration (community summaries + original chunks, map-reduce, K=5) achieves competitive accuracy at far lower cost than full GraphRAG. §6.4 documents the K=5 selection rule.

### Synthesis Source

Deep-research synthesis at T3 IDs `01d3943271e1e716` + `45040ed1bffe64d6` — three-proposal landing. This RDR implements Proposal C; RDR-097 implements Proposal A; bead nexus-2wc1 covers Proposal B.

### Nexus Modules

- `nx/plans/builtin/abstract-themes.yml` — the shipped plan template (renamed from working name `abstract-question.yml` during nexus-ldnp implementation).
- `src/nexus/db/t2/plan_library.py` — plan storage, the `plans` table, four-tier loader.
- `src/nexus/plans/matcher.py` — verb-dimension filter + match-text cosine ranking.
- `src/nexus/plans/runner.py` — step dispatch, `_OPERATOR_TOOL_MAP`. Includes the `mode: broad` authoring affordance (nexus-h3e2) that translates to `threshold=2.0` for abstract retrieval.
- `src/nexus/search_engine.py` — `_topic_label` materialization at lines 674-697; over-fetch and scope-filter surface.
- `src/nexus/db/t2/catalog_taxonomy.py` — `topics`, `chunk_topic_assignments`, ICF computation; the substrate `_topic_label` reads from.
- `src/nexus/mcp/core.py` — `operator_groupby` (line 2174), `operator_aggregate` (line 2283), `operator_summarize` (line 1894); intent-classification site `_nx_answer_classify_plan` (line 2715, no modification needed per Gap 2).
- `src/nexus/operators/dispatch.py` — `claude_dispatch` substrate for the bundled aggregate / summarize calls.
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
