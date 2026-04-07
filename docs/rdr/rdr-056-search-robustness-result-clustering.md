---
title: "Search Robustness and Result Clustering"
id: RDR-056
type: Feature
status: accepted
accepted_date: 2026-04-07
reviewed-by: self
priority: high
author: Hal Hildebrand
created: 2026-04-07
related_issues: [RDR-052, RDR-053, RDR-055]
---

# RDR-056: Search Robustness and Result Clustering

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus uses ChromaDB with HNSW indexing across all T3 collections. Three recent papers (Robustness-δ@K, BBC, Compass) reveal that:

1. **HNSW has catastrophic tail failures**: 2-5% of queries return zero relevant results, invisible to average recall metrics. Multi-hop agent workflows compound this — 5% per-query failure over 6 search calls gives ~26% probability of at least one bad retrieval.
2. **Search results are flat lists**: Agents receive ranked chunks without thematic grouping. The "LLM data understanding paradox" (HoldUp) shows that row-by-row processing degrades quality, while dumping all context causes long-context degradation.
3. **Metadata filtering is fragile**: ChromaDB's `where=` clause uses post-filter over HNSW, which stalls in predicate-sparse graph regions (Compass finding).

### Baseline Evidence (RF-13 — Measured)

10 queries × 20 results across knowledge/docs/code/cross-corpus (200 total):

| Corpus | n | Min | Max | Mean | >0.7 | >0.8 |
|--------|---|-----|-----|------|------|------|
| knowledge | 60 | 0.42 | 0.89 | 0.70 | 58% | 20% |
| docs | 40 | 0.41 | 0.87 | 0.69 | 70% | 25% |
| code | 40 | 0.86 | 0.93 | 0.89 | 100% | 100% |
| cross-corpus | 60 | 0.38 | 0.88 | 0.62 | 20% | 7% |

**Key empirical findings**:
1. **Code corpus is broken for NL queries** — all results at 0.86-0.93 plateau, 0.038 spread = no discrimination. This is a `voyage-code-3` model mismatch (NL→code), not an HNSW indexing problem.
2. **Docs shows bimodal distribution** — 5 relevant at 0.41-0.45, then a 0.25 gap, then noise at 0.69+. Clear "relevance cliff."
3. **Knowledge has long tails** — top-1 great (0.42-0.46), by rank 3 jumps to 0.71+ noise.
4. **Cross-corpus reranking works best** — lowest mean distance (0.62), fewest >0.7 results (20%).
5. **Tail problem is low-precision (noise padding), not zero-recall** — all queries returned 20 results, but bottom 50%+ is noise.
6. **Corpus-specific thresholds needed**: ~0.55 for docs/knowledge, ~0.85 for code, ~0.65 for cross-corpus.

## Research Findings

### RF-1: HNSW Tail Failures Hide Behind Average Recall

**Source**: Robustness-δ@K (arxiv 2507.00379, indexed: docs__default)

Average Recall@K masks per-query failure rates. DiskANN shows 4.8% zero-recall queries on MSMARCO while reporting avg Recall@10=0.90. Graph-based indexes (HNSW, DiskANN) have 3.4-7.6x more zero-recall queries than partition-based indexes (ScaNN, IVFFlat) at equivalent average recall. Robustness-δ@K = fraction of queries achieving at least δ recall. Setting `hnsw:search_ef` higher directly improves tail performance with moderate throughput cost.

### RF-2: Bucket-Based Collection for Large-k ANN

**Source**: BBC (arxiv 2604.01960, indexed: docs__default)

For large-k queries (k≥1000), the result collector — not candidate traversal — is the bottleneck. Binary-heap priority queues suffer O(log k) insertion + L1 cache thrashing. BBC replaces heap with distance-quantized bucket array: O(1) insertion, cache-friendly. 3.8x speedup at recall@k=0.95 for k=5000. Relevant to Nexus for future corpus-wide similarity (dedup, graph construction) scenarios.

### RF-3: Hybrid Vector+Relational Filtered Search

**Source**: Compass (arxiv 2510.27141, indexed: docs__default)

Post-filtering (HNSW traverse → filter) fails at low passrate. Pre-filtering disconnects the proximity graph. Compass uses a shared candidate queue fed by both HNSW and clustered B+-trees, with a neighborhood passrate monitor that triggers B-tree probes when graph traversal stalls. Key insight for Nexus: pre-fetch candidate IDs from catalog SQLite for selective metadata queries, then pass as ChromaDB ID-set filter.

### RF-4: Clustering Eliminates the Context Paradox

**Source**: HoldUp (arxiv 2604.02655, indexed: docs__default), Memory in the LLM Era (arxiv 2604.01707, indexed: docs__default)

HoldUp proves that clustering dataset records before LLM processing improves accuracy by 20-30% vs row-by-row. The Memory paper confirms: at 200% context load, even the best memory methods degrade. Solution: k-means on existing ChromaDB embeddings (zero additional embedding cost) + cluster summaries before agent consumption.

### RF-5: Multi-Hop Compounding of Tail Failures (Nexus-Specific)

**Source**: 720 synthesis cross-paper analysis

Multi-hop agent workflows compound tail failures multiplicatively. A 5% per-query failure rate becomes ~10% over 2 hops, ~26% over 6 hops. Nexus research-synthesizer and analytical-operator routinely chain 3-6 search calls per task. `verify_collection_deep()` uses a single-probe health check — blind to this compounding effect. Partition-based indexes show 3.4-7.6x fewer zero-recall queries, suggesting a hybrid HNSW+FTS5 approach as a robustness floor.

### RF-6: Embeddings Not Returned by T3 Search (Codebase Audit)

**Source**: `src/nexus/db/t3.py:515-521`

`t3.search()` uses `include=["documents", "metadatas", "distances"]` — embeddings are NOT included. Phase 2a's clustering sketch assumes `r.embedding` exists. Options: (a) add `"embeddings"` to include list (increases payload significantly for cloud mode), (b) post-fetch via `col.get(ids=..., include=["embeddings"])`, (c) reconstruct approximate distances from the distance values already returned. Option (b) is one extra API call per search; option (c) avoids API cost but is less accurate.

### RF-7: HNSW Configuration — Mutable search_ef (ChromaDB Source Audit)

**Source**: ChromaDB v1.5.1 source (`hnsw_params.py`, `collection_configuration.py`)

ChromaDB defaults: `search_ef=100`, `construction_ef=100`, `M=16`. `get_or_create_collection()` currently passes NO metadata — all Nexus collections run at default `search_ef=100`.

**Critical correction**: `search_ef` is **MUTABLE** after creation via `collection.modify(configuration={"hnsw": {"ef_search": 256}})`. No data migration needed. Only `space`, `ef_construction`, and `M` are immutable. Two valid syntaxes: legacy `metadata={"hnsw:search_ef": 256}` and new `configuration={"hnsw": {"ef_search": 256}}`.

**Cloud: CONFIRMED SPANN, not HNSW (RF-12)**: Chroma Cloud uses SPANN index. `hnsw:*` params are irrelevant in cloud mode. The equivalent is `spann.ef_search`. Phase 1a must be dual-path: HNSW config for local, SPANN config for cloud.

### RF-8: Reranker Is External Voyage AI API Call (Codebase Audit)


**Source**: `src/nexus/scoring.py:159-169`

`rerank_results()` calls the Voyage AI reranker API (external HTTP, not local). Phase 2b's 4-6x over-fetch + rerank means each search triggers a Voyage API call with 4-6x more candidates. Cost: billed per request. Latency: ~200-500ms per call. For 6-search agent workflows, adds 6 reranker calls. Alternative: distance-based pruning before rerank (send only top-2x to reranker, not full 4-6x), or local cross-encoder reranking.

### RF-9: verify_collection_deep() Single-Probe Design (Codebase Audit)

**Source**: `src/nexus/db/t3.py:797-861`

Current implementation: peek first doc → extract first 50 words → search top-10 → check if probe doc appears. Binary pass/fail (healthy/broken/skipped). No multi-probe hit rate, no distance distribution analysis. Called from collection verify `--deep`, post-reindex, and MCP `collection_verify` tool. Multi-probe upgrade path: `peek(limit=5)`, loop queries, compute `probe_hit_rate = found / total`. Add `probe_hit_rate: float | None` field to `VerifyResult`.

### RF-10: Clustering Implementation — scipy Available, Embeddings Required (Implementation Research)

**Source**: Dependency audit + clustering algorithm analysis

`scipy 1.17.1` is already transitively installed via docling. Ward hierarchical clustering (`scipy.cluster.hierarchy.linkage`) is deterministic, produces compact clusters, and runs in <2ms for N=50, D=1024. numpy-only k-means fallback is <1ms. scikit-learn is NOT needed.

Query-to-result distances cannot reconstruct inter-result distances (triangle inequality gives only bounds). Embeddings are required for semantic clustering. Recommended: post-fetch via `col.get(ids=..., include=["embeddings"])` — one extra ~50-100ms API call. Cluster count heuristic: `k = max(2, ceil(n/5))`. Cluster label: title of lowest-distance chunk (zero cost).

### RF-11: ChromaDB search_ef Is MUTABLE — No Migration Needed (ChromaDB Source Audit)

**Source**: ChromaDB v1.5.1 source (`hnsw_params.py`, `collection_configuration.py`, `UpdateHNSWConfiguration`)

ChromaDB defaults: `search_ef=100`, `construction_ef=100`, `M=16`. **`search_ef` is mutable** after creation via `collection.modify(configuration={"hnsw": {"ef_search": 256}})`. No data migration, no recreation. Only `space`, `ef_construction`, and `M` are immutable.

This corrects RF-7's original claim. Existing Nexus collections can be updated instantly via a one-time script or `nx doctor --fix`. Throughput cost: ef=256 ≈ 2.5x compute vs ef=100 — acceptable for interactive workload.

**Cloud caveat**: Chroma Cloud uses SPANN, not HNSW (RF-12). The equivalent param is `spann.ef_search`.

### RF-12: Chroma Cloud Uses SPANN, Not HNSW (Confirmed)

**Source**: ChromaDB Cookbook (`cookbook.chromadb.dev/core/configuration`)

Confirmed: "SPANN is the vector index used in Chroma Cloud and distributed Chroma deployments." Cannot specify both `hnsw` and `spann` — one index type per collection. Cloud creation: `configuration={"spann": {"space": "cosine", "search_nprobe": 64, "ef_search": 200}}`. Phase 1a must be dual-path branching on `T3Database._local_mode`.

### RF-13: Baseline Distance Distributions (Empirical)

**Source**: 10 representative queries × 20 results across 4 corpus types

Global: mean 0.71, median 0.74, range 0.38-0.93. 57.5% of all results >0.7 (likely noise). Code corpus broken for NL queries (100% >0.8, model mismatch). Docs bimodal with clear relevance cliff at ~0.55. Knowledge long-tailed. Cross-corpus reranking produces best results (mean 0.62).

**Critical implication**: The tail problem is low-precision noise padding, not zero-recall. 50%+ of returned results are irrelevant. Clustering will naturally separate signal from noise due to the bimodal gap. Distance thresholding before presentation would cut noise dramatically — corpus-specific thresholds: ~0.55 for docs/knowledge, ~0.65 for cross-corpus.

### RF-14: No Distance Thresholds Exist; L2 Default (Codebase Audit)

**Source**: Full search pipeline audit (`t3.py`, `mcp_server.py`, `search_engine.py`, `scoring.py`, `config.py`)

All collections default to L2 distance (`hnsw:space="l2"`). No distance thresholds exist anywhere in the search pipeline — raw ChromaDB distances returned directly to agents. Both MCP `search` and `query` tools handle fewer-than-requested results gracefully. Distance IS exposed to agents as `[0.XXXX]` in output. Embedding models: code__→voyage-code-3 index/voyage-4 query (broken, see RDR-059), docs/knowledge/rdr→voyage-context-3 (CCE).

### RF-15: Integration Architecture — search_clusterer.py Module (Pipeline Audit)

**Source**: Full data flow trace from MCP entry points through t3.search()

Three entry points: MCP `search` (flat chunks), MCP `query` (already groups by document), CLI `search` (hybrid scoring + reranking). Cleanest integration: new `src/nexus/search_clusterer.py` module called optionally from `search_engine.py` with `cluster_by` parameter. MCP `query()` already groups by document — semantic clustering is complementary, not replacement.

### RF-16: Code Search Was Broken — Embedding Model Mismatch (FIXED in RDR-059 / v3.2.5)

**Source**: Codebase audit + RF-13 empirical falsification → **RDR-059 (fixed)**

code__ was indexed with voyage-code-3 but queried with voyage-4. Fixed in v3.2.5: query model now matches index model. Code search distances dropped from 0.86-0.93 (noise) to 0.28-0.29 (relevant). See RF-18.

### RF-18: Post-Fix Code Search Validation (v3.2.5 Live Measurement)


**Source**: Live `nx search` against `code__nexus` after RDR-059 fix deployed

Before: distances 0.858-0.931, spread 0.038 (random noise, GPU shaders for NL queries).
After: distances 0.287-0.289, spread 0.002 (tight cluster of relevant verify_collection_deep code).

**Impact on RF-13 baseline**: Code corpus data invalidated — distances are now comparable to knowledge/docs (~0.28-0.45). The "corpus-specific thresholds" recommendation may simplify to a single universal threshold (~0.55) now that all corpora have coherent embedding spaces. Knowledge/docs long-tail and bimodal findings remain valid.

### RF-17: SPANN Behavior — Immutable, Hybrid Architecture, Potentially More Robust (ChromaDB Research)

**Source**: ChromaDB official docs, Cookbook, Rust source analysis, Robustness paper cross-reference

SPANN defaults: ef_search=200, search_nprobe=64. **Params immutable after creation** — modify() silently ignored. SPANN is IVF+HNSW hybrid (centroid layer is HNSW, posting lists hold embeddings). Partition-based indexes have 3.4-7.6x fewer zero-recall than pure HNSW. Cloud mode may be more robust than local for tail failures. Small collections (<1K docs) run near-brute-force regardless. Existing collections at server defaults with no introspection API.

### RF-21: Post-Fix Baseline — Per-Corpus Thresholds Validated (v3.2.5 Measurement)

**Source**: 10 queries × 20 results, all corpora, post-RDR-059

Code: mean 0.34 (was 0.89), 0% >0.7 (was 100%). Now best corpus. Cross-corpus: mean 0.43 (was 0.62), 0% >0.7 (was 20%). Knowledge/docs: modest improvement, still bimodal — top ~5 relevant (0.41-0.59), rest noise (0.67+). Universal threshold does NOT work. Per-corpus: code 0.45, knowledge/docs 0.65, cross-corpus 0.55. Alternative two-tier: <0.50 high confidence, 0.50-0.65 relevant, >0.65 noise.

### RF-19: SPANN Defaults Are Adequate — Tuning Not Worth the Constraint

**Source**: Robustness paper §5.4.2 + RF-17 SPANN defaults cross-analysis

SPANN defaults (`ef_search=200`, `search_nprobe=64`) are already 2x the HNSW default. The Robustness paper shows partition-based indexes reach Robustness-0.3@10=0.998 at nprobe=100, with diminishing returns after 50-100. SPANN's nprobe=64 is in the sweet spot. Partition-based failures are graceful (vectors still in top-100) vs HNSW catastrophic (91% outside top-100). **Recommendation**: Do not tune SPANN. Focus HNSW tuning on local mode only.

### RF-20: Agent Benefit from Clustered Output Is Assumed, Not Measured (Speculative)

**Source**: HoldUp/Memory paper extrapolation

HoldUp proves 20-30% accuracy improvement from clustering for LLM classification of tabular records. Transfer to "agent consuming search result chunks" is plausible but unproven. Key difference: HoldUp processes ALL records; agents typically stop at top-5. **Recommendation**: Ship clustering as optional (`cluster_by` parameter). A/B test on 3-5 research-synthesizer tasks before making default. Phase 1 (HNSW tuning, multi-probe, thresholds) is independently justified — don't block on clustering validation.

## Proposed Design

### Phase 1: Quick Wins (hours)

**1a. Explicit HNSW ef on all collections**

**Dual-path** (RF-7, RF-11, RF-12, RF-17) — local and cloud have fundamentally different tuning models:

**Local mode** (PersistentClient, HNSW index):
- **New collections**: Pass `metadata={"hnsw:search_ef": 256}` in `get_or_create_collection()`.
- **Existing collections**: `col.modify(configuration={"hnsw": {"ef_search": 256}})` — instant, non-destructive (RF-11 confirmed mutable).
- Default search_ef=100 is the single highest-leverage parameter for tail robustness.

**Cloud mode** (CloudClient, SPANN index — RF-12, RF-17, RF-19):
- **No tuning needed** (RF-19). SPANN defaults (`ef_search=200`, `search_nprobe=64`) are already in the robust operating range — 2x the HNSW default, within the Robustness paper's optimal nprobe zone. Partition-based failures are graceful (vectors still in top-100) vs HNSW catastrophic (91% outside top-100).
- SPANN params are immutable after creation. Tuning would require collection recreation for marginal gain over already-good defaults. Not recommended.
- **Latent bug**: `t3.py:841` reads `meta.get("hnsw:space", "l2")` — HNSW legacy key that SPANN collections don't populate. Falls back to "l2" which is coincidentally correct but fragile. Fix to detect index type.

**Detection and `nx doctor --fix` path**: `T3Database._local_mode` distinguishes modes. `nx doctor --fix` iterates `t3.list_collections()`, and for each: if `_local_mode`, calls `col.modify(configuration={"hnsw": {"ef_search": 256}})`. In cloud mode, skip HNSW tuning (SPANN defaults adequate per RF-19). The `hnsw:space` latent bug fix: in cloud mode, return the known SPANN default (`"l2"`) rather than reading the absent HNSW metadata key.

**1b. Multi-probe verify_collection_deep()**

Use `col.peek(limit=5)` instead of 1 document. Query each, report fraction recovered as `probe_hit_rate` in VerifyResult. Gives crude Robustness-δ@K proxy at δ=1.0.

**1c. Per-corpus distance thresholds (RF-21 — post-fix baseline validated)**

Post-fix baseline (v3.2.5) confirms a **single universal threshold does NOT work**. Knowledge/docs show bimodal distributions (relevant at 0.41-0.59, noise at 0.67+) while code is tight (all <0.43). Per-corpus thresholds:

| Corpus | Threshold | Rationale |
|--------|-----------|-----------|
| `code__*` | 0.45 | All relevant hits <0.43, clear separation |
| `knowledge__*`, `docs__*`, `rdr__*` | 0.65 | Captures relevant cluster, avoids noise tail starting at ~0.67 |
| Cross-corpus default | 0.55 | 93% of results below in post-fix measurement |

Alternative: two-tier confidence (`<0.50` = high, `0.50-0.65` = relevant, `>0.65` = noise).

Apply thresholds per-collection by examining `result.collection` prefix post-merge in `search_cross_corpus()`. Configurable via `.nexus.yml` (`search.distance_threshold.<prefix>`). Log dropped results count at debug level.

**Note**: The code threshold (0.45) is functionally inert under current distributions (all code results <0.43) — its value is guarding against future model changes.

**RDR-055 interaction**: When E2 quality-weighted reranking (RDR-055) is active for a collection, these thresholds apply *post-rerank*, not pre-rerank. A chunk with high distance but high citation count should survive threshold filtering if E2 elevates it. If E2 is not yet implemented, thresholds apply to raw distance. Re-validate thresholds after RDR-055 E1 re-indexing (section-type filtering may shift distance distributions).

### Phase 2: Cluster-Aware Search Results (days)

**Pipeline ordering** (resolves RF-8 cost concern):
1. Over-fetch 4x candidates per corpus
2. Apply Phase 1c distance thresholds (per-corpus, pre-rerank)
3. Send top-2x surviving candidates to Voyage reranker (not full 4x — RF-8 cost mitigation)
4. Cluster reranked output (post-rerank, so clusters contain quality-sorted results)
5. Return clusters to agent, each sorted by reranked score

**2a. Cluster pre-pass in search_engine.py**

When reranked results exceed 15 chunks, cluster via Ward linkage. Clustering is **optional and disabled by default** (RF-20 — benefit is speculative). Enabled per-call via `cluster_by="semantic"` parameter or globally via `.nexus.yml` (`search.cluster_by: semantic`). Agents that want clustering opt in; others get the existing flat ranked list.

**Note (RF-6, RF-10)**: Embeddings are not returned by `t3.search()`. Post-fetch via `col.get(ids=..., include=["embeddings"])` is the recommended approach — one extra ~50-100ms call, no impact on normal search path. Query-to-result distances CANNOT reconstruct inter-result distances (RF-10) — embeddings are required.

`scipy.cluster.hierarchy` (Ward linkage) is already available transitively via docling (RF-10). Ward is deterministic, produces compact clusters, and the dendrogram can be cut at any k post-hoc. numpy-only k-means fallback for environments without scipy.

```python
# Sketch — src/nexus/search_clusterer.py
import math
import numpy as np

def cluster_results(results: list[dict], embeddings: np.ndarray, k: int | None = None):
    n = len(results)
    if n <= 2:
        return [[r] for r in results]
    k = k or max(2, math.ceil(n / 5))  # default heuristic
    try:
        from scipy.cluster.hierarchy import linkage, fcluster
        Z = linkage(embeddings, method='ward')  # O(N^2 D), <2ms for N=50
        labels = fcluster(Z, k, criterion='maxclust') - 1
    except ImportError:
        labels = _kmeans_numpy(embeddings, k)  # fallback, <1ms
    # Group by label, sort each cluster by distance, label = title of best chunk
    ...
```

**2b. Increase over-fetch ratio for knowledge/docs**

In `search_cross_corpus()`, differentiate by corpus type:
- code corpora: current 2x (frecency handles it)
- knowledge/docs/rdr: 4-6x over-fetch, then apply `rerank_results()` from scoring.py

**Note (RF-8)**: Reranker is Voyage AI API (external, billed per call). Consider distance-based pruning first — send only top-2x candidates to reranker, not the full 4-6x over-fetch set.

### Phase 3: Catalog-Scoped Pre-Filtering (weeks)

When `where=` contains high-selectivity predicates (bib_year, specific tags), pre-fetch matching IDs from catalog SQLite and convert to ChromaDB `{"$and": [{"id": {"$in": ids}}, ...]}` filter. Avoids HNSW stalling in predicate-sparse regions.

### Phase 4: FTS5 Shadow Index (medium-term — consider separate RDR)

Add lightweight SQLite FTS5 table in a T3-adjacent database (`~/.config/nexus/shadow_fts.db`) indexing chunk titles, tags, and first 200 chars from all CCE collections (`knowledge__*`, `docs__*`, `rdr__*`). Trigger: when vector search returns no results above the per-corpus threshold (Phase 1c), fall back to FTS5. Scope: all CCE collections (not code — code search works well post-fix). This phase may warrant its own RDR given its medium-term timeline and separate storage concerns.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Clustering dependency | scipy available via docling (RF-10); numpy-only fallback for safety. No new deps needed |
| Cloud SPANN is immutable | Confirmed (RF-17). modify() silently ignored. Tuning only at collection-creation. Existing collections stuck at server defaults |
| ~~Code search broken~~ | RESOLVED (RDR-059, v3.2.5). Distances dropped from 0.86→0.29. RF-13 code baseline invalidated. |
| hnsw:space fallback fragile | t3.py:841 reads HNSW key for cloud SPANN collections. Works by coincidence (both default L2). Fix to detect index type |
| Cluster labels require LLM call | Optional — can return clusters without labels initially; agents can infer from chunk content |
| hnsw:search_ef=256 reduces throughput | Benchmark before/after; tunable via .nexus.yml |
| Over-fetch increases Voyage API cost | Only for rerank path; quantify cost per query at 4x vs 2x |

## Success Criteria

**Phase 1:**
- [ ] verify_collection_deep() reports multi-probe hit rate (5-probe)
- [ ] Per-corpus distance thresholds applied; measurable reduction in >0.65 results for knowledge/docs
- [ ] nx doctor reports per-collection robustness proxy and applies HNSW ef fix for local collections
- [ ] Local HNSW collections run at search_ef=256

**Phase 2:**
- [ ] Clustering available via `cluster_by="semantic"` parameter (disabled by default, per RF-20)
- [ ] A/B comparison on 3-5 research-synthesizer tasks before making clustering default

**Phase 3:**
- [ ] Pre-filtered catalog queries return results for selectivity <5% without HNSW stalling

**Phase 4:**
- [ ] FTS5 fallback returns ≥1 result for queries where vector search returns 0 results above threshold
