---
title: "RDR-070: Incremental Taxonomy & Clustered Search — Making the Catalog Navigable"
status: accepted
type: feature
priority: P1
created: 2026-04-12
accepted_date: 2026-04-12
reviewed-by: self
---

# RDR-070: Incremental Taxonomy & Clustered Search — Making the Catalog Navigable

## Problem Statement

Nexus has 9,361 cataloged documents across 763 collections, 981 real links (after noise removal), and 5 content types (code, prose, rdr, knowledge, paper). All of this is accessed through flat ranked search — a single vector query returns a scored list, and the operator or agent must mentally group results by topic, distinguish cross-domain matches, and decide what's related to what.

This worked when the corpus was small. It does not work now. The operator's experience:

- **Search returns too much**: 10 results from 5 collections with no grouping. Result 3 and result 8 are about the same topic but the operator can't see that without reading both.
- **No topic structure**: there is no way to ask "what topics does nexus know about?" or "what clusters exist in the knowledge base?" The data is flat.
- **Links exist but are invisible**: 853 `implements` links and 88 `cites` links encode real relationships, but search doesn't surface them. The link graph is write-only from the operator's perspective.
- **Agent campaigns are opaque**: 7 named campaigns produced links and documents, but there's no way to see what topics each campaign contributed to — only raw event counts.

The existing code has the right primitives but they're unused:
- `search_clusterer.py` (91 LOC): Ward hierarchical clustering. Never enabled by default.
- `catalog_taxonomy.py` (520 LOC): Topic storage in T2 SQLite. Never triggered automatically.
- `auto_linker.py` (106 LOC): Fires on every `store_put`. Could trigger topic assignment.

The problem is not missing features — it's that the features exist behind config flags nobody sets and CLI commands nobody runs. They need to be automatic, incremental, and visible.

## Mental Model

**Topic taxonomy as emergent structure, not imposed hierarchy.** The operator does not define topics upfront. Topics emerge from the data as documents are indexed, and the system proposes them. The operator can accept, merge, split, rename, or reject topics — but the system does the discovery.

The right precedents:
- **Gmail labels + auto-categorization**: system proposes categories from content, user can override.
- **Spotify Discover Weekly**: algorithm finds clusters, surfaces them as named playlists the user can act on.
- **GitHub Topics**: emergent tags that become navigable once enough repos use them.

The wrong precedents:
- **Enterprise taxonomy management**: top-down ontology design before any data exists.
- **Automatic folder organization**: silent sorting the user can't see or override.
- **Tag clouds**: visual noise without actionable structure.

Key principle: **the system discovers, the operator decides.** Every topic proposal is a question ("does this grouping make sense?"), not a decree. The CLI is the interaction surface for decisions.

## Non-Goals

- **Not a knowledge graph visualization tool.** Topic structure is navigable via CLI and search, not rendered as a graph diagram.
- **Not real-time streaming taxonomy.** Incremental means "updated when documents arrive," not "continuously recomputing."
- **Not multi-user taxonomy governance.** Single operator, single taxonomy.
- **Not LLM-based topic naming.** Topic labels come from BERTopic's c-TF-IDF, not from LLM summarization. LLM enrichment is deferred.
- **Not a replacement for search.** Taxonomy augments search (clustered results, topic-scoped queries), it doesn't replace the vector similarity pipeline.

## Proposed Approach

### Tooling: BERTopic + HDBSCAN (RF-070-9)

Replace the existing word-frequency Ward clustering in `catalog_taxonomy.py` with **BERTopic** for batch topic discovery and **HDBSCAN `approximate_predict`** for incremental single-document assignment.

Why: BERTopic accepts pre-computed numpy embeddings, produces automatic topic labels via c-TF-IDF, has built-in hierarchy support, and HDBSCAN's density-based clustering eliminates the need for manual distance thresholds — which RF-070-7 proved were empirically invalid at our scale.

**Dependency**: `bertopic` (pip install, ~15MB wheel). Core deps: `hdbscan`, `umap-learn`, `scikit-learn`. BERTopic can operate **without** `sentence-transformers` or PyTorch when pre-computed embeddings are provided — we pass MiniLM vectors directly. Verify minimal install path in Phase 1 implementation (observation from gate review).

### Existing Code: Replace, Don't Migrate

`catalog_taxonomy.py` (520 LOC) and `taxonomy.py` (90 LOC) are replaced entirely. The word-frequency `cluster_and_persist()` pipeline is deleted. The T2 schema (`topics`, `topic_assignments` tables) is retained as the durable persistence layer — BERTopic is the clustering engine, T2 is the source of truth for operator decisions.

`search_clusterer.py` (91 LOC) is retained for Phase 3 fallback clustering of search results when no topic assignments exist.

### Phase 1: Batch Discovery + Incremental Assignment

**Batch discovery via `nx taxonomy discover`**: runs BERTopic `fit_transform(docs, embeddings)` over all documents in a collection. Accepts MiniLM 384d vectors (RF-070-4). BERTopic's c-TF-IDF produces topic labels automatically. Expect 30-60 topics globally, 5-15 per major collection (RF-070-8).

**Auto-trigger after indexing**: `nx index repo` calls `nx taxonomy discover` automatically after indexing completes. This is a post-index hook, not a manual step. The operator can also run `nx taxonomy discover` standalone. This satisfies SC-1 ("non-empty topic tree after `nx index repo` without manual intervention").

**Incremental assignment via `post_store_hook`**: on `store_put` (MCP tool), re-embed the document content with local MiniLM (~1ms, no API call per RF-070-4), then use `HDBSCAN.approximate_predict(clusterer, [new_embedding])` to assign to an existing topic. Outliers get label -1 (unassigned) — HDBSCAN handles this natively, no buffer table needed.

**Trigger architecture (RF-070-6)**: add a `post_store_hook` callback in `mcp_infra.py`. Do NOT extend `auto_linker.py` — it is single-responsibility for links. Batch indexing via `nx index repo` is unaffected — it triggers `discover` at the end of the pipeline.

**MiniLM on code chunks (gate finding S5)**: tree-sitter AST chunks are identifier-heavy. MiniLM topic vectors for code will be syntactically driven, not semantically rich (RF-070-8). This is acceptable — code topics will reflect structural patterns (e.g., "HTTP handlers", "database queries", "test fixtures") rather than domain concepts. Phase 1 includes a validation step: cluster a sample of `code__ART` chunks and verify topic coherence before committing.

### Phase 2: Operator Review + Rebalance

**Full re-discovery**: on `nx taxonomy discover --force`, or when the corpus has grown 2x since the last discovery (RF-070-8), re-run BERTopic. Use `merge_models()` to incorporate new documents while preserving operator decisions, or full `fit_transform` for a clean rebuild.

**Hierarchy**: start flat (depth 1). Hierarchy only helps above ~2K docs per topic level (RF-070-8). Only `code__ART` (4,168 docs) may need sub-topics. BERTopic's `hierarchical_topics()` handles this when needed.

**Operator review via `nx taxonomy review`**: presents 10-15 topics per session (RF-070-8 cognitive load limit). Each topic shows: label, doc count, top 5 terms, 3 representative doc titles. Operator actions: accept / rename / merge / split / delete / skip.

```
Topic "schema-evolution" (47 docs):
  Terms: schema, evolution, migration, mapping, transform
  Docs:  curino-2008-prism | rdr-053-xanadu-fidelity | catalog.py

  [a]ccept  [r]ename  [m]erge with...  [s]plit  [d]elete  [S]kip
```

Operator decisions are stored in T2 `topics` table (the `label` and `parent_id` columns). BERTopic model files are rebuildable; T2 decisions are durable.

### Phase 3: Clustered Search (the visible part)

**Default-on clustering**: `search_cross_corpus` enables `cluster_by="semantic"` by default when results span multiple collections. Results are grouped by topic when topic assignments exist, falling back to Ward clustering (existing `search_clusterer.py`) when they don't.

**Topic-scoped search**: new search parameter `topic=<label>` pre-filters to documents assigned to that topic before running vector search.

**Console integration**: the Health panel shows topic counts. The Activity panel shows which topics received new documents. The Campaigns panel shows which topics each campaign contributed to.

### Phase 4: Link Graph Integration (the enrichment part)

**Topic-aware links**: when a link is created between two documents, if both are assigned to topics, the link implicitly connects those topics. `nx taxonomy links` shows inter-topic relationships.

**Topic as search boost**: documents in the same topic as a search result get a small relevance boost (0.1). Documents in a linked topic get a smaller boost (0.05).

## Data Model

### Source of Truth: T2 SQLite (durable) + BERTopic model files (rebuildable)

T2 `topics` and `topic_assignments` tables are the **source of truth** for topic labels, assignments, and operator decisions. BERTopic model files are the **clustering engine state** — they can be rebuilt from T3 embeddings at any time via `nx taxonomy discover --force`.

### T2 Schema (existing, minor extensions)

```sql
-- topics: id, label, parent_id, collection, centroid_hash, doc_count, created_at
--   centroid_hash: SHA256 of the BERTopic model version that produced this topic
--   No centroid BLOB — BERTopic manages its own topic representations internally

-- topic_assignments: id, topic_id, doc_id, collection, assigned_at
--   Unchanged. BERTopic assigns, T2 persists.

-- New column for model tracking
ALTER TABLE topics ADD COLUMN model_version TEXT;  -- BERTopic model ID
```

### BERTopic Model Persistence

```
~/.config/nexus/taxonomy/
  {collection}.bertopic       # BERTopic serialized model (~5-50MB per collection)
  {collection}.hdbscan.pkl    # HDBSCAN clusterer for approximate_predict (~1-5MB)
```

Expected sizes: 5-50MB per collection depending on document count. Total for 9 active collections: ~50-200MB.

**Recovery**: if model files are missing or corrupted, `approximate_predict` falls back to labeling new documents as unassigned (-1). `nx taxonomy discover` rebuilds the model from T3 embeddings. Operator decisions in T2 are preserved across rebuilds — the `label` column is authoritative, not the BERTopic-generated label.

## CLI Commands

```
nx taxonomy discover [--collection C] [--force]  # batch topic discovery (Phase 1)
nx taxonomy list [--collection C]                 # show topic tree
nx taxonomy show <label>                          # show documents in a topic
nx taxonomy review                                # interactive accept/rename/merge/split (Phase 2)
nx taxonomy assign <doc-id> <topic>               # manual assignment
nx taxonomy merge <topic1> <topic2>               # merge two topics
nx taxonomy split <topic> --k N                   # split a topic into N sub-topics
nx taxonomy rename <topic> <new-label>            # rename
```

The existing `nx taxonomy rebuild` becomes an alias for `nx taxonomy discover --force`.

## MCP Tool Changes

```
search(..., topic="label")   # new parameter: pre-filter by topic
search(..., cluster_by="semantic")  # already exists, becomes default
```

## Phasing

| Phase | Scope | Depends on | Effort |
|-------|-------|------------|--------|
| P1 | BERTopic integration + discover CLI + post-index auto-trigger + post_store_hook | — | 3-4 days |
| P2 | `nx taxonomy review` interactive CLI + rebalance trigger | P1 | 3-4 days |
| P3 | Clustered search default-on + topic-scoped search | P1 | 2-3 days |
| P4 | Link graph integration + topic-aware boost | P1, P3 | 2-3 days |

P1 and P3 can run in parallel after BERTopic integration is in place.

## Success Criteria

1. `nx taxonomy list` shows a non-empty topic tree after `nx index repo .` — discovery triggers automatically post-index, no manual command needed.
2. `nx search "schema evolution"` returns results grouped by topic, not flat.
3. `nx taxonomy review` after a rebalance presents ≤60 topics, reviewable in 2-4 sessions of 10-15 topics each.
4. An agent running `search(topic="schema-evolution", query="mapping composition")` gets results scoped to that topic.
5. The operator can answer "what topics does nexus know about?" in under 5 seconds via `nx taxonomy list`.

## Open Questions (All Resolved)

1. **Embedding source**: **RESOLVED → Local MiniLM 384d (RF-070-4).** Cross-model Voyage cosine similarity is ~0.05 (documented noise). MiniLM serves as the unified topic-assignment space. ~1ms per doc, no API calls. MiniLM ceiling: ~50 reliable topics (RF-070-8); upgrade to bge-base 768d if >60 topics needed.
2. **Cross-collection topics**: **RESOLVED → MiniLM unified space (RF-070-7).** Per-collection Voyage embeddings are incompatible (cross-model mean distance 1.005 ≈ random). All topic math uses MiniLM re-embedding.
3. **Topic hierarchy**: **RESOLVED → Flat, split on demand (RF-070-8).** At 10K docs / 30-60 topics, average topic size is 170-330 — well below the ~2K threshold where hierarchy helps.
4. **Threshold approach**: **RESOLVED → HDBSCAN density-based clustering (RF-070-7, RF-070-9).** Manual thresholds were empirically invalid (real means: code 0.56, prose 0.52). HDBSCAN discovers clusters from natural data structure.
5. **Tool choice**: **RESOLVED → BERTopic + HDBSCAN `approximate_predict` (RF-070-9).** ~300 LOC integration, accepts pre-computed embeddings, automatic labels, built-in hierarchy, incremental without refit.
6. **Existing code**: **RESOLVED → Replace.** `catalog_taxonomy.py` word-frequency pipeline is deleted. T2 schema retained. BERTopic is the engine, T2 is the durable store.

## Research Findings

### RF-070-1: Current Data Shape
- 9,361 documents across 763 collections, 5 content types
- 981 real links (after `implements-heuristic` noise removal)
- Largest collection: `code__ART-8c2e74c0` (4,168 docs) — likely needs multiple topics
- 173 entries in `knowledge__knowledge` — cross-project knowledge base, most in need of topic structure

### RF-070-2: Existing Infrastructure
- `search_clusterer.py`: 91 LOC, Ward + k-means. Retained for Phase 3 fallback.
- `catalog_taxonomy.py`: 520 LOC, word-frequency clustering. **Replaced by BERTopic** — T2 schema retained, clustering engine swapped.
- `auto_linker.py`: 106 LOC, single-responsibility for link creation. **Not extended** — separate `post_store_hook` for topic assignment (RF-070-6).
- `scoring.py`: `_LINK_BOOST_WEIGHTS` dict. Adding topic boost is mechanical (Phase 4).

### RF-070-3: Word-Frequency vs. Embedding Clustering
The existing `cluster_and_persist` uses word-frequency vectors. This works for memory entries but not for code chunks. BERTopic operating on MiniLM 384d embeddings replaces this entirely.

### RF-070-4: Cross-Model Embedding Incompatibility (HIGH confidence)
Cross-model cosine similarity between `voyage-code-3` and `voyage-context-3` is ~0.05 — documented noise. `EmbeddingModelMismatch` error class enforces this. **Use local MiniLM 384d** as the unified topic embedding space. ~1ms per doc, no API calls, deterministic.

### RF-070-5: Original Manual Thresholds (SUPERSEDED by RF-070-7 + RF-070-9)
~~Calibrated from search noise-floor data: ≤0.35 prose / ≤0.25 code.~~ These were empirically invalid (RF-070-7). HDBSCAN's density-based approach eliminates the need for manual thresholds entirely.

### RF-070-6: Hook Point for Incremental Assignment (HIGH confidence)
Add `post_store_hook` callback in `mcp_infra.py`. Do NOT extend `auto_linker.py`. Modify `t3.put()` to return the embedding vector (currently discarded). Batch indexing unaffected.

### RF-070-7: Empirical Distance Distributions (HIGH confidence — 7,350 pairwise measurements)
Measured intra-collection mean pairwise cosine distances across 6 production collections (132,691 total docs):

| Content type | P10 | Median | Mean | P90 |
|---|---|---|---|---|
| Code (3 collections) | 0.40-0.43 | 0.53-0.62 | 0.53-0.60 | 0.65-0.72 |
| Prose (2 collections) | 0.12-0.24 | 0.54-0.55 | 0.51-0.53 | 0.72-0.86 |
| Knowledge | 0.52 | 0.74 | 0.71 | 0.85 |

Cross-model (voyage-code-3 vs voyage-context-3) averages **1.005** — random. MiniLM same-project cross-type delta: +0.228. **Manual thresholds are fragile; HDBSCAN is the right abstraction.**

### RF-070-8: Topic Modeling Literature (MEDIUM-HIGH confidence)
- **Expected topics**: 30-60 globally, 5-15 per major collection
- **Hierarchy**: flat sufficient at 10K; hierarchy helps above ~2K docs per topic level
- **MiniLM ceiling**: ~50 reliable topics; conflation exceeds 20% above that
- **Incremental quality**: 85-90% agreement with batch; rebalance every 2x corpus growth
- **Operator review**: 10-15 topics per session, 2-4 sessions total for 30-60 topics
- **Code topics**: syntactically driven in MiniLM — label from structure, not identifiers

### RF-070-9: Taxonomy Tool Survey (HIGH confidence)
**Winner: BERTopic + HDBSCAN `approximate_predict`.** Evaluated 12 tools across 5 categories. BERTopic: accepts numpy embeddings via `fit_transform(docs, embeddings)`, automatic c-TF-IDF labels, `hierarchical_topics()`, `merge_models()` for production incremental updates. HDBSCAN (scikit-learn 1.3+): `approximate_predict` assigns new documents without refitting.

Rejected: Top2Vec (no pre-computed embedding API), Gensim LDA (bag-of-words only), Owlready2/SKOS (ontology, not discovery), Lilac (dead), Nomic Atlas (cloud-only), Argilla (overkill), Neo4j/SPARQL (overkill at 10K).
