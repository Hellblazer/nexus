---
title: "RDR-070: Incremental Taxonomy & Clustered Search — Making the Catalog Navigable"
status: draft
type: feature
priority: P1
created: 2026-04-12
reviewed-by: pending
---

# RDR-070: Incremental Taxonomy & Clustered Search — Making the Catalog Navigable

## Problem Statement

Nexus has 9,361 cataloged documents across 763 collections, 981 real links (after noise removal), and 5 content types (code, prose, rdr, knowledge, paper). All of this is accessed through flat ranked search — a single vector query returns a scored list, and the operator or agent must mentally group results by topic, distinguish cross-domain matches, and decide what's related to what.

This worked when the corpus was small. It does not work now. The operator's experience:

- **Search returns too much**: 10 results from 5 collections with no grouping. Result 3 and result 8 are about the same topic but the operator can't see that without reading both.
- **No topic structure**: there is no way to ask "what topics does nexus know about?" or "what clusters exist in the knowledge base?" The data is flat.
- **Links exist but are invisible**: 853 `implements` links and 88 `cites` links encode real relationships, but search doesn't surface them. The link graph is write-only from the operator's perspective.
- **Agent campaigns are opaque**: 7 named campaigns produced links and documents, but there's no way to see what topics each campaign contributed to — only raw event counts.

The existing code has the right primitives:
- `search_clusterer.py` (91 LOC): Ward hierarchical clustering with k-means fallback. Clean, tested, never enabled by default.
- `catalog_taxonomy.py` (520 LOC): Topic discovery via word-frequency clustering over memory entries. Stores topics + assignments in T2 SQLite. Never triggered automatically.
- `auto_linker.py` (106 LOC): Fires on every `store_put` MCP call. Could be the trigger for incremental topic assignment.

The problem is not missing features — it's that the features exist behind config flags nobody sets and CLI commands nobody runs. They need to be automatic, incremental, and visible.

## Mental Model

**Topic taxonomy as emergent structure, not imposed hierarchy.** The operator does not define topics upfront. Topics emerge from the data as documents are indexed, and the system proposes them. The operator can accept, merge, split, rename, or reject topics — but the system does the discovery.

The right precedents:
- **Gmail labels + auto-categorization**: system proposes categories from content, user can override. Categories are visible everywhere (inbox, search, sidebar).
- **Spotify Discover Weekly**: algorithm finds clusters in listening data, surfaces them as named playlists the user can act on.
- **GitHub Topics**: emergent tags that become navigable once enough repos use them.

The wrong precedents:
- **Enterprise taxonomy management**: top-down ontology design before any data exists. Too heavy.
- **Automatic folder organization**: silent sorting that the user can't see or override. Too opaque.
- **Tag clouds**: visual noise without actionable structure.

Key principle: **the system discovers, the operator decides.** Every topic proposal is a question ("does this grouping make sense?"), not a decree. The CLI is the interaction surface for decisions — not a web UI, not a config file.

## Non-Goals

- **Not a knowledge graph visualization tool.** Topic structure is navigable via CLI and search, not rendered as a graph diagram. Visualization is a future RDR.
- **Not real-time streaming taxonomy.** Incremental means "updated when documents arrive," not "continuously recomputing."
- **Not multi-user taxonomy governance.** Single operator, single taxonomy. No access control, no approval workflows.
- **Not LLM-based topic naming.** Topic labels come from the highest-ranked term in the cluster centroid, not from an LLM summarization call. LLM enrichment is deferred — it has latency, cost, and prompt-dependency that the base system should not require.
- **Not a replacement for search.** Taxonomy augments search (clustered results, topic-scoped queries), it doesn't replace the vector similarity pipeline.

## Proposed Approach

### Phase 1: Batch Topic Discovery + Incremental Assignment

**Architecture change (RF-070-9)**: use **BERTopic** for batch topic discovery and **HDBSCAN `approximate_predict`** for incremental single-document assignment. This replaces hand-rolled Ward clustering with a production-quality library that solves the exact problem. ~300 LOC integration, ~15MB dependency (bertopic + hdbscan + umap-learn + scikit-learn, no PyTorch).

**Initial discovery**: `nx taxonomy discover [--collection CODE]` runs BERTopic `fit_transform(docs, embeddings)` over all documents in a collection. Accepts our MiniLM 384d vectors directly — no re-embedding needed. BERTopic's c-TF-IDF produces topic labels automatically from document text. Expect 30-60 topics globally, 5-15 per major collection (RF-070-8).

**Incremental assignment**: on `store_put`, re-embed the document content with local MiniLM (~1ms, no API call per RF-070-4), then use `HDBSCAN.approximate_predict(clusterer, [new_embedding])` to assign to an existing topic. No refit, no threshold tuning — HDBSCAN handles density-based assignment natively, including labeling outliers as noise (-1) instead of forcing assignment.

**Trigger**: add a `post_store_hook` callback in `mcp_infra.py` (RF-070-6). Do NOT extend `auto_linker.py`. Batch indexing via `nx index repo` is unaffected — topic assignment for batch runs as a separate `nx taxonomy discover` operation.

**Threshold correction (RF-070-7)**: the original thresholds (≤0.25 code / ≤0.35 prose) were empirically invalid — they would catch <5% of same-collection pairs. Real intra-collection mean pairwise distances: code 0.56, prose 0.52, knowledge 0.71. **HDBSCAN eliminates the need for manual thresholds entirely** — it discovers density-based clusters from the data's natural structure.

**Persistence**: BERTopic model serialized to disk (`.safetensors` format). HDBSCAN clusterer object pickled for `approximate_predict`. Topic labels and assignments stored in existing T2 `topics` / `topic_assignments` schema. Add `model_version` column to track which BERTopic model produced the clustering.

### Phase 2: Periodic Rebalance (the expensive part)

**Full re-discovery**: on `nx taxonomy discover --force`, or when the corpus has grown 2x since the last discovery (RF-070-8), re-run BERTopic over the full collection. BERTopic's `merge_models()` merges a new batch model into the existing master model, preserving operator decisions. Alternatively, a full `fit_transform` with `hierarchical_topics()` produces a topic tree mapping to our `topics.parent_id` column.

**Hierarchy**: start flat (depth 1) per RF-070-8 — hierarchy only helps above ~2K docs per topic. Only `code__ART` (4,168 docs) is likely to need sub-topics. BERTopic's built-in `hierarchical_topics()` handles this when needed.

**User review**: after a rebalance, `nx taxonomy review` presents each new/changed topic as a question:

```
Topic "schema-evolution" (47 docs, 3 collections):
  Top terms: schema, evolution, migration, mapping, transform
  Sample docs: curino-2008-prism, rdr-053-xanadu-fidelity, src/nexus/catalog/catalog.py
  
  [a]ccept  [r]ename  [m]erge with...  [s]plit  [d]elete  [S]kip
```

This is a CLI interaction, not a web form. The operator works through the list and the taxonomy stabilizes. Subsequent rebalances produce fewer changes as the taxonomy converges.

### Phase 3: Clustered Search (the visible part)

**Default-on clustering**: `search_cross_corpus` enables `cluster_by="semantic"` by default when results span multiple collections. Results are grouped by topic when topic assignments exist, falling back to Ward clustering of result embeddings when they don't.

**Topic-scoped search**: new search parameter `topic=<label>` pre-filters to documents assigned to that topic before running vector search. This is the primary navigation mechanism — the operator discovers topics via `nx taxonomy list`, then drills into a topic via `nx search --topic "schema-evolution" "mapping composition"`.

**Console integration**: the Health panel shows topic counts. The Activity panel shows which topics received new documents. The Campaigns panel shows which topics each campaign contributed to.

### Phase 4: Link Graph Integration (the enrichment part)

**Topic-aware links**: when a link is created between two documents, if both are assigned to topics, the link implicitly connects those topics. `nx taxonomy links` shows inter-topic relationships — which topics cite each other, which topics implement each other.

**Topic as search boost**: documents in the same topic as a search result get a small relevance boost (0.1). Documents in a linked topic get a smaller boost (0.05). This makes search aware of the taxonomy without replacing vector similarity.

## Data Model Changes

### T2 Schema Extensions

```sql
-- Extend topics table with centroid storage
ALTER TABLE topics ADD COLUMN centroid BLOB;  -- msgpack float32 vector
ALTER TABLE topics ADD COLUMN term_weights TEXT;  -- JSON {term: weight} for labeling

-- Unassigned document buffer
CREATE TABLE IF NOT EXISTS taxonomy_buffer (
    id INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL,
    collection TEXT NOT NULL,
    embedding BLOB,  -- msgpack float32 vector
    buffered_at TEXT NOT NULL,
    UNIQUE(doc_id, collection)
);
```

### Existing Tables (unchanged)

```sql
-- topics: id, label, parent_id, collection, centroid_hash, doc_count, created_at
-- topic_assignments: id, topic_id, doc_id, collection, assigned_at
```

## CLI Commands

```
nx taxonomy list [--collection CODE]     # show topic tree
nx taxonomy show <label>                  # show documents in a topic
nx taxonomy rebuild [--project NAME]      # full re-cluster
nx taxonomy review                        # interactive accept/rename/merge/split
nx taxonomy assign <doc-id> <topic>       # manual assignment
nx taxonomy merge <topic1> <topic2>       # merge two topics
nx taxonomy split <topic> --k N           # split a topic into N sub-topics
nx taxonomy rename <topic> <new-label>    # rename
nx taxonomy buffer                        # show unassigned documents
```

## MCP Tool Changes

```
search(..., topic="label")   # new parameter: pre-filter by topic
search(..., cluster_by="semantic")  # already exists, becomes default
```

## Phasing

| Phase | Scope | Depends on | Effort |
|-------|-------|------------|--------|
| P1 | Incremental assign-on-ingest + centroid storage | — | 3-4 days |
| P2 | Periodic rebalance + `nx taxonomy review` CLI | P1 | 3-4 days |
| P3 | Clustered search default-on + topic-scoped search | P1 | 2-3 days |
| P4 | Link graph integration + topic-aware boost | P1, P3 | 2-3 days |

P1 and P3 can run in parallel after the centroid storage is in place.

## Success Criteria

1. `nx taxonomy list` shows a non-empty topic tree after `nx index repo .` without manual intervention
2. `nx search "schema evolution"` returns results grouped by topic, not flat
3. `nx taxonomy review` after a rebalance presents fewer than 20 topics for a typical single-project corpus
4. An agent running `search(topic="schema-evolution", query="mapping composition")` gets results scoped to that topic
5. The operator can answer "what topics does nexus know about?" in under 5 seconds via the CLI

## Open Questions (All Resolved)

1. **Embedding source**: **RESOLVED → Local MiniLM 384d (RF-070-4).** Cross-model Voyage cosine similarity is ~0.05 (documented noise). MiniLM serves as the unified topic-assignment space. ~1ms per doc, no API calls, unifies local and cloud mode. MiniLM ceiling: ~50 reliable topics (RF-070-8); upgrade to bge-base 768d if >60 topics needed.
2. **Cross-collection topics**: **RESOLVED → MiniLM unified space.** Per-collection Voyage embeddings are incompatible (RF-070-7: cross-model mean distance 1.005 ≈ random). All topic math uses MiniLM re-embedding. Same-project cross-content-type delta is +0.228 in MiniLM space — detectable but wide.
3. **Topic hierarchy depth**: **RESOLVED → Flat, split on demand (RF-070-8).** At 10K docs / 30-60 topics, average topic size is 170-330 — well below the ~2K threshold where hierarchy helps. Only `code__ART` (4,168 docs) may need sub-topics. BERTopic `hierarchical_topics()` available when needed.
4. **Threshold approach**: **RESOLVED → HDBSCAN replaces manual thresholds (RF-070-7, RF-070-9).** The original thresholds (≤0.25 code / ≤0.35 prose) were empirically invalid — real intra-collection means are 0.56 (code) and 0.52 (prose). HDBSCAN discovers density-based clusters from natural data structure without distance cutoffs. BERTopic wraps HDBSCAN with topic labeling.
5. **Tool choice**: **RESOLVED → BERTopic + HDBSCAN `approximate_predict` (RF-070-9).** ~300 LOC integration, ~15MB deps, accepts pre-computed embeddings, incremental assignment without refit, automatic topic labels via c-TF-IDF, built-in hierarchy support.

## Research Findings

### RF-070-1: Current Data Shape
- 9,361 documents across 763 collections, 5 content types
- 981 real links (after `implements-heuristic` noise removal)
- Largest collection: `code__ART-8c2e74c0` (4,168 docs) — likely needs multiple topics
- 173 entries in `knowledge__knowledge` — the cross-project knowledge base, most in need of topic structure

### RF-070-2: Existing Infrastructure
- `search_clusterer.py`: 91 LOC, Ward + k-means, clean API, tested. Needs no changes for Phase 3.
- `catalog_taxonomy.py`: 520 LOC, full T2 domain store with schema, locking, tree queries. Needs centroid storage + incremental assign for Phase 1.
- `auto_linker.py`: 106 LOC, single-responsibility for link creation. **Do NOT extend for topic assignment** (see RF-070-6). Add a separate `post_store_hook` in `mcp_infra.py` instead.
- `scoring.py`: already has `_LINK_BOOST_WEIGHTS` dict. Adding topic boost is mechanical.

### RF-070-3: Word-Frequency vs. Embedding Clustering
The existing `cluster_and_persist` uses word-frequency vectors (TF-IDF-like). This works for memory entries (short text, English) but not for code chunks (identifiers, mixed languages). Phase 1 should use the document's actual embedding vector for topic assignment. Per RF-070-4, use local MiniLM as the topic embedding space to avoid cross-model incompatibility.

### RF-070-4: Cross-Model Embedding Incompatibility (HIGH confidence)
Cross-model cosine similarity between `voyage-code-3` and `voyage-context-3` is ~0.05 — documented noise. The codebase has `EmbeddingModelMismatch` error class and explicit guard rails against mixing. **Recommendation**: use bundled `LocalEmbeddingFunction` (MiniLM 384d, `src/nexus/db/local_ef.py`) as a dedicated topic-assignment embedding space. Cost: ~1ms per document on CPU, no API calls, deterministic. Naturally unifies local and cloud mode. Topic centroids stored as 384d vectors in T2 `topics.centroid` BLOB column.

### RF-070-5: Incremental Assignment Thresholds (MEDIUM confidence)
Calibrated from existing noise-floor thresholds in `config.py:296` (RDR-056 empirical data):

| Parameter | Prose (knowledge/docs/rdr) | Code | Rationale |
|---|---|---|---|
| Same-topic assignment | cosine distance ≤ 0.35 | ≤ 0.25 | Midpoint of useful range (0 to noise floor) |
| Buffer before mini-cluster | 10 docs | 10 docs | Ward minimum viable k=2 |
| Split (too broad) | mean pairwise > 0.40 | > 0.30 | ~60% of noise floor |

ChromaDB uses cosine distance (1 - cosine_similarity). Only 9 of 763 collections exceed 100 docs. Schema is ready: `centroid_hash` column exists, `topic_assignments` supports `INSERT OR IGNORE`. **Thresholds should be logged and adjusted** after first real deployment — medium confidence because they're derived from search calibration, not topic-specific validation.

### RF-070-6: Hook Point for Incremental Assignment (HIGH confidence)
**Do NOT extend `auto_linker.py`** — it is single-responsibility for catalog link creation with zero embedding awareness. Instead:
1. Add a `post_store_hook` callback list in `mcp_infra.py`, called from `store_put` after the existing auto_link call.
2. Modify `t3.put()` to **return the embedding vector** instead of discarding it — CCE collections already compute it in `_cce_embed()` then throw it away after upsert.
3. For code collections (server-side embedding), use `t3.get_embeddings(collection, [doc_id])` — one HTTP call, acceptable latency.
4. **Batch indexing is unaffected** — `index_repository()` calls `t3.upsert_chunks_with_embeddings()` directly, never touching `store_put`. Topic assignment for batch-indexed repos stays as a separate `nx taxonomy discover` operation.

### RF-070-7: Empirical Distance Distributions (HIGH confidence — 7,350 pairwise measurements)
**The original thresholds were empirically invalid.** Measured intra-collection mean pairwise cosine distances across 6 production collections (132,691 total docs):

| Content type | P10 | Median | Mean | P90 | Std |
|---|---|---|---|---|---|
| Code (3 collections) | 0.40-0.43 | 0.53-0.62 | 0.53-0.60 | 0.65-0.72 | 0.10-0.12 |
| Prose (2 collections) | 0.12-0.24 | 0.54-0.55 | 0.51-0.53 | 0.72-0.86 | 0.17-0.30 |
| Knowledge | 0.52 | 0.74 | 0.71 | 0.85 | — |

A code threshold of 0.25 captures <5% of same-collection pairs. Cross-model distance (voyage-code-3 vs voyage-context-3) averages **1.005** — random. MiniLM re-embedding shows same-project cross-type delta of +0.228. **Conclusion**: manual thresholds are fragile; HDBSCAN's density-based approach is the right abstraction.

### RF-070-8: Topic Modeling Literature (MEDIUM-HIGH confidence)
At ~10K mixed code/prose documents:
- **Expected topics**: 30-60 globally, 5-15 per major collection
- **Hierarchy**: flat sufficient at 10K; hierarchy helps above ~2K docs per topic level
- **MiniLM ceiling**: ~50 reliable topics; above that, conflation exceeds 20%
- **Incremental quality**: 85-90% agreement with batch between rebalances; rebalance every 2x corpus growth
- **Operator review bandwidth**: 10-15 topics per session, merge fastest (2-3s), split slowest (15-30s)
- **Code topics**: syntactically driven in MiniLM; label from file paths/AST, not identifiers

### RF-070-9: Taxonomy Tool Survey (HIGH confidence)
**Winner: BERTopic + HDBSCAN `approximate_predict`.** Evaluated against: Top2Vec (no pre-computed embedding API), Gensim LDA (bag-of-words only), Owlready2/SKOS (ontology, not discovery), Lilac (dead), Nomic Atlas (cloud-only), Argilla (overkill).

BERTopic: accepts numpy embeddings via `fit_transform(docs, embeddings)`, automatic c-TF-IDF topic labels, built-in `hierarchical_topics()`, `merge_models()` for production incremental updates. ~15MB wheel, 8.5K GitHub stars, active development. HDBSCAN (in scikit-learn 1.3+): `approximate_predict(clusterer, new_points)` assigns new documents to existing clusters without refitting — the exact primitive for incremental assignment.
