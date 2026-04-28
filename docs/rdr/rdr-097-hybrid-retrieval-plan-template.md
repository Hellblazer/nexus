---
title: "RDR-097: Hybrid Retrieval Plan Template — Fusing Catalog Traversal and Vector Search for Factual QA"
id: RDR-097
type: Feature
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-28
related_issues: []
related_tests: []
related: [RDR-049, RDR-050, RDR-052, RDR-078, RDR-080, RDR-084, RDR-091, RDR-092, RDR-093, RDR-098]
---

# RDR-097: Hybrid Retrieval Plan Template — Fusing Catalog Traversal and Vector Search for Factual QA

This RDR proposes a single new builtin plan template, `hybrid-factual-lookup`, that fuses catalog link-graph traversal with vector search before generation. The shape — vector recall over-fetched, then link-traversal expansion from the matched tumblers, then a hybrid-score merge, then generate — is the highest-ROI move from the recent RAG-paper synthesis (T3 IDs `01d3943271e1e716` + `45040ed1bffe64d6`). It uses primitives nexus already ships (`search`, `traverse`, `operator_rank`, `operator_generate`); it adds zero dependencies; it lands as plan YAML plus integration tests over `knowledge__hybridrag`.

The motivating evidence is two finance-and-graph-RAG papers: HybridRAG (Sarmah et al., 2024) shows dual-stream context (KG-derived + vector-derived, concatenated before the LLM) raises faithfulness from 0.94 to 0.96 on financial QA; the Zhou et al. VLDB 2025 unified-framework analysis shows the top-performing graph-RAG variants all retain original chunks alongside graph-derived context rather than replacing them. The two findings together justify a *fused* retrieval plan rather than a graph-only or vector-only one.

A simpler companion template, `traverse-then-generate`, is included for the case where seed tumblers are explicit caller inputs (the "I already know which document I want to expand from" pathway) so the same merge-and-generate scaffolding doesn't fork into ad-hoc plan code.

## Problem Statement

### Gap 1: No builtin plan exercises the link graph as a first-class retrieval signal

The 12 builtin plans seeded by `nx catalog setup` (`src/nexus/commands/catalog.py:_seed_plan_templates`, with YAMLs under `nx/plans/builtin/`) cover author-scoped search, type-scoped search, citation traversal for provenance, and several research/analyze/review/document/debug shapes. Every retrieval-shaped plan that does use `traverse` (`citation-traversal.yml`, `research-default.yml`) treats traversal as a *second* recall pass — vector search produces seed tumblers, traversal expands from them, then `store_get_many` hydrates and `summarize` or `generate` consumes the union. That is exactly the dual-stream shape HybridRAG argues for at the architecture level, but no builtin plan packages it for *factual lookup* — the question shape where the win is largest. Factual-lookup queries (point answers, single-entity attribute lookups, "what does paper X say about Y") are precisely where graph-augmented retrieval beats vector-only by the largest margin in the unified-framework benchmark, because vector recall on short factual queries is noisy and the link graph supplies disambiguation.

### Gap 2: The `operator_rank` surface may not support hybrid-score merging with weights

The current `operator_rank` (`src/nexus/mcp/core.py:1782`) accepts `items` (text or JSON array) and `criterion` (natural-language string) and dispatches to `claude_dispatch` with a schema requiring a single `ranked: array[string]` output. It is a Claude-prompted ranking pass, not a numeric-score merge. A hybrid plan that wants to combine `vector_score` (cosine similarity from the `search` step) with `link_distance` (BFS hop count from the `traverse` step) under explicit weights cannot express that through `operator_rank` today — it can only ask Claude to "rank these by relevance to the question". This RDR has to make a call: either (a) drive `operator_rank` with a richer criterion that includes the hybrid scoring rule in natural language and trust Claude to apply it, or (b) introduce a new structural operator (`operator_merge_streams` or similar) that takes two scored ID lists and a weight tuple and emits a fused ranking deterministically. Option (a) is cheaper and matches how the rest of the operator surface treats Claude as the ranker; option (b) is more honest about what a hybrid plan actually computes. Decision is recorded in §Decision below.

### Gap 3: Catalog link-graph density is not measured per collection

The hybrid plan's value depends on the seed tumblers having outgoing links of the right types (`cites`, `implements`, `relates`). Collections that were not enriched by the citation linker (`src/nexus/catalog/link_generator.py:generate_citation_links`) — code-only repos, knowledge stubs that arrived without bibliographic metadata, RDRs that haven't yet been linked to their implementations — will produce empty `traverse` results, and the hybrid plan degrades to a vector-only plan with extra latency. There is no current observability surface that tells a plan author *before they write a plan* which collections have enough link density to justify hybrid retrieval. `nx catalog coverage` reports link counts by content type globally; it does not report per-collection link-degree distributions. The RDR proposes a measurement step before plan rollout to ground the routing logic.

### Gap 4: Context-precision tradeoff is real and unbounded

HybridRAG's known weakness, called out in the paper's §5.4 limitations: dual-stream context lowers context-precision because the model sees more tokens, some of them off-topic, and is more likely to ground a generation step on a low-relevance chunk. A token budget per stream is the standard mitigation. Nexus has no plan-level token-budget primitive today — `store_get_many` returns whatever the catalog points to, and `operator_generate` consumes whatever it's given. The RDR cannot punt this entirely; the plan needs to specify a per-stream limit that is honored by the hydration step.

## Context

### Technical Environment

- **Plan library**: `src/nexus/db/t2/plan_library.py`, the `plans` SQLite table seeded from YAML files at `nx/plans/builtin/*.yml` via the four-tier loader.
- **Plan matching**: `src/nexus/plans/matcher.py` (T1 cosine + FTS5 fallback, scope-aware after RDR-091, hybrid match-text after RDR-092).
- **Plan execution**: `src/nexus/plans/runner.py`, dispatches each step's `tool` to the corresponding MCP tool via the registry.
- **Search step**: `src/nexus/search_engine.py:search` — supports per-corpus over-fetch (`max(5, n_results * mult)`, capped at `QUOTAS.MAX_QUERY_RESULTS = 300`), `where` predicates, topic pre-filter and grouping; returns chunks with `score`, `tumblers`, `ids`, `collections`.
- **Traverse step**: `src/nexus/mcp/core.py:traverse` — accepts seed tumblers, `link_types` or `purpose`, depth (capped at 3), direction; returns `{tumblers, ids, collections}` mirroring the `search` step output contract so steps compose.
- **Operator surface**: `src/nexus/operators/dispatch.py:claude_dispatch` is the substrate; MCP tools in `src/nexus/mcp/core.py` expose `operator_rank`, `operator_generate`, `operator_filter`, `operator_compare`, `operator_extract`, `operator_summarize`, `operator_verify`, `operator_check`, `operator_aggregate`, `operator_groupby` (the post-RDR-088/093 set).
- **Catalog density observability**: `nx catalog coverage` reports global link coverage by content type. Per-collection link-degree distribution is not currently surfaced.

### Source Corpus

Three papers in `knowledge__hybridrag` ground the proposal:

- **HybridRAG** (Sarmah et al., 2024) — tumbler **1.653.15**, arxiv 2408.04948. Demonstrates dual-stream KG + vector context concatenated before generation, faithfulness 0.94 → 0.96 on financial QA, and identifies context-precision regression as the principal tradeoff.
- **In-depth Analysis of Graph-based RAG** (Zhou et al., VLDB 2025) — tumbler **1.653.79**, arxiv 2503.04338. Compares graph-RAG variants in a unified framework; finds the best-performing methods retain original chunks alongside graph-derived context; reports PPR (personalized PageRank) traversal outperforms BFS for specific-fact QA; cites the context-precision-vs-recall frontier explicitly.
- **SAGE** (Wang et al., PVLDB 2026 preprint) — tumbler **1.653.77**. Orthogonal companion: structure-aware graph evidence selection; relevant only as a future operator (not Phase 1 scope).

### Research Findings

**RF-1** (Verified, source `src/nexus/mcp/core.py:1782-1804`): `operator_rank` accepts a free-form `criterion` string. The criterion is interpolated directly into the prompt template `Rank the following items by {criterion}.`. A criterion of the form `"hybrid score = 0.6 * vector_score + 0.4 * (1 / (1 + link_distance))"` is syntactically valid and the LLM will attempt to apply it; whether the application is *deterministic* across runs is an empirical question the integration tests answer.

**RF-2** (Verified, source `src/nexus/mcp/core.py:2380-2470`): the `traverse` tool returns `{tumblers, ids, collections}` and accepts seeds as either a list or a single string. It composes cleanly with `search` step output via `$stepN.tumblers` references, as `citation-traversal.yml` and `research-default.yml` already demonstrate.

**RF-3** (Verified, source `src/nexus/search_engine.py:201-320`): the `search` step honors a `limit` arg that drives over-fetch internally (`per_k = min(max(5, n_results * mult), 300)`). Setting `limit: 40` on the vector step gives the hybrid plan enough recall to compensate for the precision dilution introduced by the merge step.

**RF-4** (Verified, source `nx/plans/builtin/research-default.yml`): plans can express scope filters (`taxonomy_domain`, `topic`) per step, so the vector step can be scoped to `knowledge__*` collections while leaving the `traverse` step unscoped to follow links across content types. This matters because `cites` links from a `knowledge__*` paper often land in `rdr__*` or `code__*` collections, and forcing scope on the traverse step would prune that.

**RF-5** (Verified, source `nx/plans/builtin/citation-traversal.yml:30-35`): the `purpose` arg (`reference-chain`, `find-implementations`) is the canonical way to bundle link types for a traverse step; it's resolved through `nexus.plans.purposes.resolve_purpose`. A new purpose, `factual-evidence` or `hybrid-context`, that bundles `["cites", "implements", "relates"]` would give this plan a stable name to refer to even if the link-type set evolves.

**RF-6** (Documented, HybridRAG paper §4 + Zhou et al. §6.2): both papers cap context per stream at the model's input budget divided proportionally between vector and graph streams. HybridRAG uses a 50/50 split in their reported configuration; Zhou et al. find 60/40 (vector/graph) marginally better on factual QA. The plan should default to 60/40 with the split exposed as an optional binding.

**RF-7** (Empirical, abstract-themes smoke run 2026-04-28 on `docs__art-grossberg-papers`, 19,417 chunks / 46 BERTopic topics): the four-step operator-chain pattern (search → operator step → operator step → summarize) executes end-to-end at production-corpus scale. Captured output (`/tmp/plan_smoke3.out` on `feature/nexus-ldnp-abstract-plan`): 20 retrieved chunks → 4 thematic groups (LAMINART, Embedding Fields, What/Where/ART, References) → coherent unified summary. The bundled-step optimization (`_bundled_intermediate` from RDR-093) fired correctly: groupby + aggregate ran in one `claude -p` dispatch instead of two separate ones. RDR-097's hybrid plan reuses the same orchestration shape; this finding constitutes pattern validation, not a guarantee about hybrid-specific recall.

**RF-8** (Verified, `src/nexus/mcp/core.py:3225`, commit 715fcd6 on 2026-04-28): `nx_answer` now auto-aliases the question text into any `required_bindings` declared by the matched plan that the caller didn't pre-supply. This removes the constraint that builtin plan templates use only `$intent` to be dispatchable from a bare question. RDR-097's `hybrid-factual-lookup` can use semantic binding names (`$concept`, `$topic`, `$query`, etc.) freely; skill callers that pre-extract entities (find-by-author's `$author`) bypass this alias by calling `plan_run` directly with explicit bindings. Test coverage: `tests/test_nx_answer.py::TestNxAnswerBindingAlias` (4 cases pin the contract).

**RF-9** (Empirical, search MCP tool 2026-04-28 against `docs__art-grossberg-papers`): the search step's `threshold` arg is applied to *pre-boost* cosine distance, but the result envelope reports *post-boost* distance. Concrete numbers: question "main themes in cognitive and neural mechanisms" returned 5 chunks at threshold=2.0 with reported distances 0.85–0.90; the same chunks dropped at threshold=0.95 because their pre-boost distance was higher. **Implication for RDR-097**: the per-corpus default thresholds (0.65 for prose, 0.55 for code) are tuned for narrow-target search and will drop most candidates for broad/abstract phrasings. The hybrid plan's vector step should set `threshold: 2.0` explicitly to disable filtering during over-fetch, then rely on the merge/rank step to surface the precise matches. Systemic fix tracked as bead `nexus-h3e2` (per-corpus broad-query threshold tier or `mode: broad` plan-step flag).

## Decision

Ship one plan template, `hybrid-factual-lookup`, plus a simpler `traverse-then-generate` variant for the explicit-seeds case. Use the existing `operator_rank` surface with a richer criterion string for the merge step (Option (a) from Gap 2). Defer the deterministic numeric-merge operator (`operator_merge_streams`) to a follow-up bead. Add a one-shot density-measurement command and a per-stream token-budget binding before plan rollout.

### Why one phase

This is intentionally a minimum-meaningful experiment. The proposal is "does fused retrieval beat vector-only on factual QA over our actual knowledge collections" — a question that can be answered with one plan template, five integration test questions, and a weekend's measurement. Multi-phase scope expansion would dilute the signal: if the plan beats vector-only with the simplest possible merge step, then we know to invest in a deterministic merge operator, PPR traversal, glossary-aware ranking, and so on. If it doesn't, we don't, and the cost was one PR. RDR-091 and RDR-092 are the prior art for "land the smallest thing that proves the principle, file the bigger ideas as follow-ups".

### Why `operator_rank` with a richer criterion (Gap 2 resolution)

Three reasons. First, every other operator in the surface treats Claude as the structural primitive — adding `operator_merge_streams` for one plan ahead of evidence that prompted ranking is unreliable would be premature. Second, the integration tests measure end-to-end quality (does the plan surface more relevant context than vector-only); they do not require deterministic ranking, so a Claude-prompted merge is fit-for-purpose for the experiment. Third, if the experiment validates the plan's value, a follow-up bead can introduce `operator_merge_streams` with the deterministic numeric-merge contract that this experiment will have produced evidence for. The risk we accept: the merge step is non-deterministic. Mitigation: integration tests record their full inputs and outputs to T2 telemetry so reruns can be diffed against the original answer set.

### Why depth-2 BFS, not PPR (open question 2 resolution)

Zhou et al. (§6.2 of the unified framework, tumbler 1.653.79) report PPR > BFS for specific-fact QA, but PPR requires a graph-wide stationary distribution computation that nexus does not currently support. Adding PPR is a separate operator (`operator_traverse_ppr` or extending `traverse` with a `strategy` arg), and the work is non-trivial. Depth-2 BFS is what `traverse` already gives us, it produces a tractable candidate set (typically 5-20 tumblers per seed at depth 2 on `knowledge__hybridrag`), and it's a known-imperfect baseline. File PPR as `nexus-XXX` follow-up bead after Phase 1 measurement.

### Why a token budget per stream (Gap 4 resolution)

The plan exposes two optional bindings, `vector_budget_chunks` (default 6) and `graph_budget_chunks` (default 4), giving a 60/40 split per RF-6. **In Phase 1, these are plan-author conventions, not runner-enforced caps.** The plan YAML uses Python-style slice expressions in `store_get_many` step args (e.g. `ids: $step1.ids[:vector_budget_chunks]`) so each stream is hydrated only up to its budget before merge. The runner's auto-hydration applies a single global `_OPERATOR_MAX_INPUTS=100` cap; per-stream budget enforcement at the runner level is filed as `nexus-uwkw` follow-up bead. Phase 1 is honest about this: the integration test harness (P1.5) verifies the plan-level slicing produces the right hydrated counts; it does NOT verify a runner-level mechanism that doesn't yet exist.

### Why pin `dimensions.verb: lookup` (open question 5 resolution)

Both new templates use `verb: lookup` so they share matcher space and disambiguate on `strategy`: `hybrid-factual-lookup` vs `traverse-then-generate`. RDR-091's scope-aware matching keys on `(verb, scope, strategy)` triples, and RDR-092's hybrid match-text synthesizes its embedding anchor from the same fields plus name and description. Pinning verb at gate time matches the contract RDR-098 used (`verb: abstract`) and avoids carrying an indeterminate dimension into accept.

### Why a density measurement step before rollout (Gap 3 resolution)

Phase 1 includes a one-shot diagnostic command, `nx catalog link-density --by-collection`, that reports outgoing-link counts per collection at the depth-2 BFS frontier. The output gates plan rollout: collections with median frontier < 3 nodes are flagged as poor candidates for hybrid retrieval and the plan author is told to use a vector-only plan instead. This is observability, not gating logic — the plan still runs against any collection; the diagnostic just tells authors which collections will benefit.

## Phase 1 — Hybrid Plan Template, Density Measurement, and Integration Test Harness

One phase, one branch, one PR. Single-phase by design (see "Why one phase" above).

### Prerequisites (mapped to beads)

- **P1.1 — `hybrid-factual-lookup` plan YAML.** Create `nx/plans/builtin/hybrid-factual-lookup.yml` with the four-step shape (search → traverse → operator_rank → operator_generate) parameterized by seed-or-question, depth, link-type purpose, and the two stream budgets. Reuse `research-default.yml` and `citation-traversal.yml` as structural references. Run `nx catalog setup` against a clean DB to verify it loads without YAML errors.
- **P1.2 — `traverse-then-generate` plan YAML.** Create `nx/plans/builtin/traverse-then-generate.yml`, a three-step variant (traverse → store_get_many → operator_generate) for the explicit-seeds path. Same YAML conventions as P1.1.
- **P1.3 — `factual-evidence` purpose.** Add a new purpose alias to `src/nexus/plans/purposes.py` mapping `factual-evidence` → `["cites", "implements", "relates"]`, so both plans reference link types by stable name. Test via `nexus.plans.purposes.resolve_purpose("factual-evidence")`. *P1.5 validation: if `implements` fires zero times across the 5 test fixtures on `knowledge__hybridrag`, drop it from the alias before the PR lands. The `knowledge__*` corpus has sparse `implements` links by construction (those are usually code↔RDR pairs), so this is a real possibility.*
- **P1.4 — Density measurement CLI.** Add `nx catalog link-density --by-collection` to `src/nexus/commands/catalog.py`. Output: one row per collection with `frontier_p50`, `frontier_p90`, `link_types_present`. Reuses the existing `Catalog.graph_many` machinery; no new SQL.
- **P1.5 — Integration test harness.** Add `tests/test_hybrid_plan_factual_qa.py` with 5 question fixtures over `knowledge__hybridrag`. Each fixture: question string, expected-relevant tumbler set, expected-relevant chunk substrings. The test runs both `hybrid-factual-lookup` and a vector-only baseline plan against the same questions, computes recall@10 and overlap-with-expected, asserts the hybrid plan is *not worse* than vector-only on any question (the actual claim — "is it better" — is reported in the test output for measurement, not asserted as a gate). Records full inputs/outputs to T2 telemetry for diffability.
- **P1.6 — Plan-level documentation.** Add a comment block at the top of `hybrid-factual-lookup.yml` documenting the context-precision tradeoff (HybridRAG §5.4) and the per-stream token budget rationale. Update `docs/architecture.md` plan-library section to list the new templates.

### Success Metrics

- `nx catalog setup` seeds two new templates (`hybrid-factual-lookup`, `traverse-then-generate`) without YAML errors. Total count is `current_count + 2`; conditioned on RDR-098's abstract-themes template (PR #342) merge ordering — if RDR-098 lands first, current_count is 13 and the post-seed total is 15.
- Both new plans match successfully via `plan_match` for the test fixtures' question shapes (recorded `match_count` increments after the integration test run).
- Hybrid plan's recall@10 over the 5 fixtures is recorded as a baseline number; no regression assertion against vector-only beyond "not worse on any question".
- Density CLI output has at least one row per collection in `knowledge__*` and reports a non-zero `frontier_p50` for `knowledge__hybridrag` (negative test: code-only collection should report `frontier_p50 = 0`).

### Out of Scope (intentional, not deferrals)

- **PPR traversal.** Filed as follow-up bead after Phase 1 measurement.
- **Deterministic numeric merge operator (`operator_merge_streams`).** Phase 1 uses `operator_rank` with a hybrid-score criterion string. If the integration tests show non-determinism is a problem, file a follow-up.
- **SAGE-style structure-aware ranking.** Companion paper, separate plan template, not Phase 1.
- **Cross-collection scope auto-routing based on density.** The density CLI is observability-only; it does not auto-rewrite plans.

## Risks and Mitigations

- **Risk: `operator_rank` hybrid criterion is non-deterministic across runs.** Mitigation: integration tests record full I/O to T2 telemetry; if rerun deltas exceed a threshold the test reports them as warnings. Follow-up bead introduces `operator_merge_streams` if deltas are material.
- **Risk: Sparse-link collections produce vector-equivalent results with extra latency.** Mitigation: the density CLI tells authors which collections to use the plan against; the plan still runs against any collection (no gating).
- **Risk: Token budget defaults (6/4) are wrong for non-financial-QA corpora.** Mitigation: budgets are optional bindings; the integration test sweeps two settings (6/4 and 4/2) and records the better one in the test output; we don't pretend Phase 1 picked the global optimum.
- **Risk: Context-precision regression on long-context questions.** Mitigation: the test harness includes one long-context fixture explicitly designed to exercise the regression; if it fires, we tighten budgets in a follow-up.
- **Risk: Plan name collision or confusion with `citation-traversal.yml`.** Mitigation: the existing template has `dimensions.verb=research`; the new plans use `dimensions.verb=lookup` (pinned in §"Why pin `dimensions.verb: lookup`") so the matcher disambiguates by verb.
- **Risk: T1 plan-session cache is stale after `nx catalog setup` seeds new templates mid-session, so `plan_match` returns wrong-plan or no-match results.** This was empirically observed during the RDR-098 abstract-themes smoke run on the same orchestration shape — the implementer had to bypass `nx_answer` and call `plan_run` directly via Python to get clean dispatch numbers. Mitigation: the P1.5 integration test harness explicitly resets `plans__session` (or uses a fresh `EphemeralClient` T1) before each test run; the test docstring calls out the cache contract so future maintainers don't waste an afternoon debugging the symptoms. Tracked as bead `nexus-qgjr` for the systemic fix (plan-cache invalidation on T2 mutation).

## Open Questions

1. **PPR vs BFS** — depth-2 BFS as MVP per Zhou et al. §6.2. PPR filed as follow-up after Phase 1 measurement evidence is in.
2. **Hybrid-merge operator shape** — Phase 1 uses `operator_rank` with a hybrid-score criterion. If non-determinism shows up materially in test reruns, follow-up introduces `operator_merge_streams` with a deterministic numeric contract.
3. **Per-collection density gating policy** — Phase 1 ships density observability only. Whether density should *route* (auto-fall-back to vector-only on sparse-link collections) is a Phase 2 question.
4. **Token budget tuning per corpus class** — Phase 1 ships defaults from RF-6. Per-corpus tuning is a follow-up if the integration test sweep shows large per-corpus variance.

## References

### Source Papers

- Sarmah, B. et al. (2024). *HybridRAG: Integrating Knowledge Graphs and Vector Retrieval Augmented Generation for Efficient Information Extraction.* arXiv:2408.04948. Tumbler **1.653.15** in `knowledge__hybridrag`. Faithfulness 0.94 → 0.96 on financial QA via dual-stream KG + vector context concatenation; §5.4 documents the context-precision tradeoff.
- Zhou, S. et al. (2025). *In-depth Analysis of Graph-based RAG in a Unified Framework.* arXiv:2503.04338, VLDB 2025. Tumbler **1.653.79** in `knowledge__hybridrag`. Top methods retain original chunks alongside graph-derived context; PPR > BFS for specific-fact QA per §6.2; 60/40 vector/graph budget split marginally optimal.
- Wang, Y. et al. (2026). *SAGE: Structure-Aware Graph Evidence Selection.* PVLDB preprint. Tumbler **1.653.77** in `knowledge__hybridrag`. Orthogonal companion; relevant as a future operator, not Phase 1 scope.

### Synthesis Source

Deep-research synthesis at T3 IDs `01d3943271e1e716` + `45040ed1bffe64d6` — three-proposal landing with this RDR implementing Proposal A (highest-ROI, zero-new-deps).

### Nexus Modules

- `src/nexus/operators/` — operator dispatch surface (`dispatch.py:claude_dispatch`, `aspect_sql.py`).
- `src/nexus/mcp/core.py` — MCP tool surface including `traverse` (line 2380), `operator_rank` (line 1782), `operator_generate`, and the rest of the post-RDR-088/093 operator set.
- `src/nexus/search_engine.py` — vector retrieval with per-corpus over-fetch, scope filters, topic pre-filter and grouping.
- `src/nexus/db/t2/plan_library.py` — plan storage, the `plans` table, four-tier loader (after RDR-091 scope-aware matching, RDR-092 hybrid match-text).
- `src/nexus/plans/matcher.py`, `src/nexus/plans/runner.py`, `src/nexus/plans/purposes.py` — matching, execution, and link-type alias resolution.
- `nx/plans/builtin/citation-traversal.yml`, `nx/plans/builtin/research-default.yml` — structural references for the new plan YAMLs.
- `src/nexus/catalog/link_generator.py` — citation linker that populates the link graph the new plan reads.
- `src/nexus/commands/catalog.py:_seed_plan_templates` — the seeding entry point.

### Related RDRs

- **RDR-049** Git-Backed Catalog — the catalog substrate this plan reads.
- **RDR-050** Knowledge Graph and Catalog-Aware Query Planning — the conceptual frame for plan-level catalog use.
- **RDR-052** Catalog-First Query Routing — pushed planning into MCP, the substrate this RDR extends.
- **RDR-078** Plan-Centric Retrieval — semantic plan matching, typed-graph traversal, scenario plans; introduced `traverse`.
- **RDR-080** Retrieval Layer Consolidation — `nx_answer` as the canonical retrieval entry point.
- **RDR-084** Plan Library Growth — auto-save successful ad-hoc plans; the discipline this template feeds into.
- **RDR-091** Scope-Aware Plan Matching — the matcher behavior the new plan's `dimensions` will be ranked under.
- **RDR-092** Plan Match-Text from Dimensional Identity — the hybrid match-text mechanism the new plan participates in.
