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
- **Not LLM-based topic naming.** Topic labels come from c-TF-IDF (CountVectorizer + TfidfTransformer), not from LLM summarization. LLM enrichment is deferred.
- **Not a replacement for search.** Taxonomy augments search (clustered results, topic-scoped queries), it doesn't replace the vector similarity pipeline.

## Proposed Approach

### Tooling: sklearn HDBSCAN + ChromaDB Centroids (RF-070-9, RF-070-11)

Replace the existing word-frequency Ward clustering in `catalog_taxonomy.py` with **`sklearn.cluster.HDBSCAN`** for batch topic discovery and **ChromaDB ANN centroid lookup** for incremental single-document assignment.

Why: HDBSCAN's density-based clustering eliminates the need for manual distance thresholds — which RF-070-7 proved were empirically invalid at our scale. `CountVectorizer` + `TfidfTransformer` produce c-TF-IDF topic labels. ChromaDB's existing HNSW (local) / SPANN (cloud) indices handle nearest-centroid lookup for incremental assignment.

**Dependency**: `scikit-learn>=1.3` (core dep, ~30MB). `sklearn.cluster.HDBSCAN` provides the same algorithm as the standalone `hdbscan` package. No optional extras needed.

**Why not BERTopic**: BERTopic eagerly imports `sentence-transformers` at module load time, which transitively pulls PyTorch (~500MB total). This cannot be avoided even when pre-computed embeddings are used. sklearn provides the same HDBSCAN algorithm at 1/17th the install weight. The only BERTopic-specific features (`merge_models`, `hierarchical_topics`) are straightforward to implement directly (~10-20 LOC each) with sklearn's `AgglomerativeClustering` (RF-070-11, nexus-86v).

### Existing Code: Replace, Don't Migrate

`catalog_taxonomy.py` (520 LOC) and `taxonomy.py` (90 LOC) are replaced entirely. The word-frequency `cluster_and_persist()` pipeline is deleted. The T2 schema (`topics`, `topic_assignments` tables) is retained as the durable persistence layer — sklearn HDBSCAN is the clustering engine, T2 is the source of truth for operator decisions.

`search_clusterer.py` (91 LOC) is retained for Phase 3 fallback clustering of search results when no topic assignments exist.

### Phase 1: Batch Discovery + Incremental Assignment

**Batch discovery via `nx taxonomy discover`**: runs `sklearn.cluster.HDBSCAN(min_cluster_size=M, store_centers="centroid").fit_predict(embeddings)` over all documents in a collection. Accepts MiniLM 384d vectors (RF-070-4). `CountVectorizer` + `TfidfTransformer` produce per-cluster c-TF-IDF topic labels. Centroids are upserted into a `taxonomy__centroids` ChromaDB collection for incremental assignment. Expect 30-60 topics globally, 5-15 per major collection (RF-070-8).

**`min_cluster_size` adaptive formula**: `M = max(5, len(embeddings) // 15)`. This is a starting heuristic, not a partition count — HDBSCAN's `min_cluster_size` is a minimum density threshold, and actual topic count depends on embedding geometry. Examples of M values: `code__ART` (4,168 docs) → M=277. `knowledge__knowledge` (173 docs) → M=11. Floor of 5 prevents degenerate single-document clusters on small collections. Actual topic count must be measured empirically — see validation task nexus-7m8. Operator can override via `--min-cluster-size` CLI flag.

**All-noise handling**: if HDBSCAN assigns every document to noise (-1, i.e. `store_centers` returns empty centroids), `discover` logs a warning, skips centroid upsert, and returns 0 topics. This can happen on very small or very homogeneous collections where no density structure exists. The operator can retry with a lower `--min-cluster-size`.

**c-TF-IDF text retrieval**: `CountVectorizer` needs document texts per cluster, not just embeddings. The `discover` pipeline retrieves texts from T3 via `collection.get(ids=chunk_ids, include=["documents"])` after `fit_predict` assigns cluster labels. Texts are held in-memory for the duration of label generation only — not persisted. For 9,361 documents at ~500 bytes average, this is ~5MB — well within memory budget. Label generation operates on full stored document text (the `documents` field in ChromaDB), not on chunk metadata.

**Auto-trigger after indexing**: `nx index repo` calls `nx taxonomy discover` automatically after indexing completes. This is a post-index hook, not a manual step. The operator can also run `nx taxonomy discover` standalone. This satisfies SC-1 ("non-empty topic tree after `nx index repo` without manual intervention").

**Incremental assignment via `post_store_hook`**: on `store_put` (MCP tool), re-embed the document content with local MiniLM (~1ms, no API call per RF-070-4), then query `taxonomy__centroids` ChromaDB collection via `collection.query(query_embeddings=[emb], n_results=1)` — ChromaDB's HNSW (local) / SPANN (cloud) ANN index finds the nearest centroid. Assign unconditionally to the nearest topic — no distance threshold. This is consistent with HDBSCAN eliminating manual thresholds (RF-070-7): the batch `discover` run defines cluster boundaries via density; incremental assignment simply maps new documents to the closest existing cluster. Misassignments are corrected at the next re-discovery (2x corpus growth trigger). No HDBSCAN model deserialization needed.

**Trigger architecture (RF-070-6)**: add a `post_store_hook` callback in `mcp_infra.py`. Do NOT extend `auto_linker.py` — it is single-responsibility for links. Batch indexing via `nx index repo` is unaffected — it triggers `discover` at the end of the pipeline.

**Hook execution model**: synchronous, blocking `store_put` return. Latency budget: MiniLM re-embed (~1ms) + ChromaDB ANN query (~1ms) + T2 write (~1ms) = ~3ms per document. At 100 documents/session, total overhead is ~300ms — acceptable. Synchronous execution avoids shutdown races (async hook might not complete before process exit) and guarantees topic assignment is visible immediately after `store_put` returns. Follows the existing `catalog_auto_link` pattern which is also synchronous. Exceptions are caught per-hook and logged — never fail the `store_put`.

**MiniLM on code chunks (gate finding S5)**: tree-sitter AST chunks are identifier-heavy. MiniLM topic vectors for code will be syntactically driven, not semantically rich (RF-070-8). This is acceptable — code topics will reflect structural patterns (e.g., "HTTP handlers", "database queries", "test fixtures") rather than domain concepts. Phase 1 includes a validation step: cluster a sample of `code__ART` chunks and verify topic coherence before committing.

**Batch noise handling**: HDBSCAN typically assigns 10-30% of points as noise (label -1) in real-world data. At 9,361 documents, ~1,000-2,800 may be unassigned after `discover`. These documents are not lost — they appear in search results normally, they just lack a topic assignment. `nx taxonomy list` shows an **"Uncategorized"** count alongside real topics so the operator knows the noise fraction. No pseudo-topic row is created in T2 for noise — the absence of a `topic_assignments` row IS the noise signal. Noise documents are candidates for assignment at the next re-discovery when the corpus grows.

### Phase 2: Operator Review + Rebalance

**Full re-discovery**: on `nx taxonomy discover --force`, or when the corpus has grown 2x since the last discovery (RF-070-8), re-run sklearn HDBSCAN on all embeddings. Full `fit_predict` rebuild.

**Operator decision merge strategy**: HDBSCAN topic IDs are ordinal and unstable across runs — topic 2 in run 1 may become topic 5 in run 2. Operator-edited labels must survive re-discovery:

1. Before `fit_predict`: read old centroids + labels from `taxonomy__centroids` ChromaDB collection.
2. Run `fit_predict` → new clusters with new ordinal IDs.
3. For each new cluster centroid, find nearest old centroid (cosine similarity in `taxonomy__centroids`). Each old centroid ID may be claimed at most once; if two new centroids both match the same old centroid above 0.8, the higher-similarity claimant wins and the other falls through to step 5.
4. If similarity > 0.8 (high match) and old centroid unclaimed: transfer old operator label to new topic row. Mark `centroid_hash = "auto-matched:{old_topic_id}"` for audit trail.
5. If similarity ≤ 0.8 (ambiguous): use c-TF-IDF generated label. Mark `centroid_hash = "new"`. Flag for operator review in next `nx taxonomy review` session.
6. Clear old centroids, upsert new centroids to `taxonomy__centroids`.

The 0.8 similarity threshold is deliberately high — it is better to lose a label (operator re-reviews it) than to silently transfer a label to the wrong cluster. This is a centroid-to-centroid match, not a document-to-centroid match, so the "no manual thresholds" principle (RF-070-7) does not apply — this is a merge heuristic for label transfer, not a clustering decision.

Manual topic assignments (`nx taxonomy assign`, `assigned_by='manual'`) receive special treatment during re-discovery: they are preferentially transferred to the highest-similarity new centroid regardless of the 0.8 merge threshold. If no new centroid exceeds 0.5 similarity, the manual assignment is flagged for operator review in the next `nx taxonomy review` session rather than silently rerouted. This preserves the mental model: "the operator decides."

**T2 mutation sequence for re-discovery** (mirrors centroid lifecycle):

1. Read old T2 topic rows (id, label, collection, assigned_by) for the target collection.
2. Read old centroids + labels from `taxonomy__centroids` ChromaDB collection.
3. Run `fit_predict` on new embeddings → new cluster labels + centroids.
4. Build new topic rows using merge strategy (steps 3-6 above — match old centroids, transfer labels).
5. For `assigned_by='manual'` rows: find highest-similarity new centroid to old topic, transfer if >0.5.
6. DELETE old `topic_assignments` for collection.
7. DELETE old `topics` for collection.
8. INSERT new topic rows (with merged labels where matched).
9. INSERT new `topic_assignments` from fit_predict output. Manual assignments get `assigned_by='manual'` preserved; auto-matched get `assigned_by='auto-matched'`; rest get `assigned_by='hdbscan'`.
10. Clear old centroids from ChromaDB, upsert new centroids.

Steps 6-9 run inside a single T2 transaction under `self._lock`.

**Hierarchy**: start flat (depth 1). Hierarchy only helps above ~2K docs per topic level (RF-070-8). Only `code__ART` (4,168 docs) may need sub-topics. `sklearn.cluster.AgglomerativeClustering` on centroids handles this when needed.

**Operator review via `nx taxonomy review`**: presents 10-15 topics per session (RF-070-8 cognitive load limit). Each topic shows: label, doc count, top 5 terms, 3 representative doc titles. Operator actions: accept / rename / merge / split / delete / skip.

**Known defect — T3-origin title resolution**: `get_topic_docs()` has a documented defect (RDR-063): for T3-origin topics (`code__*`, `knowledge__*`, `rdr__*`), the JOIN against `memory.title` finds no match, so titles fall back to raw chunk IDs (e.g., `code__ART-8c2e74c0::src/nexus/catalog.py:42`). Since RDR-070 taxonomy targets T3 collections exclusively, the `nx taxonomy review` UX will show chunk IDs instead of human-readable titles until this defect is fixed. **Phase 2 fix**: resolve titles via the catalog (`CatalogEntry.title`) for T3-origin topics, bypassing the T2 memory JOIN. This is the path described in `get_topic_docs()` docstring (line 270-275 of catalog_taxonomy.py) for the eventual Phase 3 fix.

```
Topic "schema-evolution" (47 docs):
  Terms: schema, evolution, migration, mapping, transform
  Docs:  curino-2008-prism | rdr-053-xanadu-fidelity | catalog.py

  [a]ccept  [r]ename  [m]erge with...  [s]plit  [d]elete  [S]kip
```

Operator decisions are stored in T2 `topics` table (the `label` and `parent_id` columns). ChromaDB centroids are rebuildable; T2 decisions are durable.

### Phase 3: Clustered Search (the visible part)

**Default-on clustering**: `search_cross_corpus` enables `cluster_by="semantic"` by default when results span multiple collections. **Precedence**: topic-based grouping takes priority when topic assignments exist for the result set — results are grouped by their T2 topic label. Ward clustering (`search_clusterer.py`) is the fallback when no topic assignments exist (e.g., before first `discover` run, or for collections where `discover` produced all noise). The two mechanisms are mutually exclusive per search: if >50% of results have topic assignments, use topic grouping; otherwise use Ward. No blending — mixed grouping would be confusing.

**Topic-scoped search**: new search parameter `topic=<label>` pre-filters to documents assigned to that topic before running vector search. Implementation path:

1. `search_cross_corpus()` gains a `topic: str | None` parameter.
2. If set, look up `doc_id`s assigned to the topic via `CatalogTaxonomy.get_topic_docs()` (T2 query). Requires T2 injection into the search path — add `t2` parameter to `search_cross_corpus()` or use `t2_ctx()` inline (per-call, not singleton).
3. Use ChromaDB `where={"id": {"$in": doc_ids}}` to pre-filter. If topic has >500 docs (exceeds `_MAX_PREFILTER_IDS`), use post-filter instead: run normal search, then filter results to topic-assigned docs. Post-filter returns fewer results but avoids the pre-filter cap.
4. Unknown topic label → empty result set with warning, not error.

**Console integration**: the Health panel shows topic counts. The Activity panel shows which topics received new documents. The Campaigns panel shows which topics each campaign contributed to.

### Phase 4: Link Graph Integration (the enrichment part)

**Topic-aware links**: when a link is created between two documents, if both are assigned to topics, the link implicitly connects those topics. `nx taxonomy links` shows inter-topic relationships.

**Topic as search boost**: documents in the same topic as a search result get a small relevance boost (0.1). Documents in a linked topic get a smaller boost (0.05).

## Data Model

### Source of Truth: T2 SQLite (durable) + ChromaDB centroids (rebuildable)

T2 `topics` and `topic_assignments` tables are the **source of truth** for topic labels, assignments, and operator decisions. The `taxonomy__centroids` ChromaDB collection stores per-topic centroid embeddings for incremental assignment — rebuildable from T3 embeddings at any time via `nx taxonomy discover --force`.

### T2 Schema (minor extension — add `assigned_by`)

```sql
-- topics: id, label, parent_id, collection, centroid_hash, doc_count, created_at
--   centroid_hash: used to track which discover run produced this topic
--   Actual schema per catalog_taxonomy.py:85-101. No id/collection/assigned_at
--   columns on topic_assignments (audit M1 — earlier RDR text was wrong).

-- topic_assignments: doc_id (TEXT), topic_id (INTEGER), PRIMARY KEY (doc_id, topic_id)
--   sklearn HDBSCAN assigns, T2 persists.

-- New column (RDR-070 Phase 1):
ALTER TABLE topic_assignments ADD COLUMN assigned_by TEXT NOT NULL DEFAULT 'hdbscan';
--   Values: 'hdbscan' (batch discover), 'centroid' (incremental nearest-centroid),
--           'manual' (operator via nx taxonomy assign), 'auto-matched' (re-discovery
--           merge transferred this assignment from an old topic to a new one)
--   Note: topics.centroid_hash carries "auto-matched:{old_id}" for label-transfer audit;
--   topic_assignments.assigned_by carries 'auto-matched' for assignment-transfer audit.
--   These are independent — a topic row can be auto-matched (label transferred) while its
--   assignments are 'hdbscan' (re-assigned by fit_predict, not transferred).
```

The `assigned_by` column enables durable manual assignments: on re-discovery, `manual` assignments are preferentially matched — find the highest-similarity new centroid to the old topic and transfer the manual assignment regardless of the 0.8 merge threshold. If no new centroid is above 0.5 similarity, flag the manual assignment for explicit operator review rather than silently rerouting.

**Optional Phase 2 enhancement**: add `distance_to_centroid REAL` to `topic_assignments` (populated from ChromaDB ANN query distance). Enables `nx taxonomy show --sort-by-distance` and allows the review command to surface low-confidence assignments first. Not required for Phase 1 — re-discovery is the correction loop.

### Centroid Persistence (ChromaDB collection, not pickle files)

Topic centroids are stored in a `taxonomy__centroids` ChromaDB collection:

- **Embeddings**: HDBSCAN centroid vectors (MiniLM 384d) from `store_centers="centroid"`
- **Metadata per centroid**: `{topic_id, label, collection, doc_count}`
- **Index**: HNSW (local, `hnsw:space=cosine`) / SPANN (cloud, cosine by default)
- **IDs**: `topic-{topic_id}` — upserted after each `discover` run
- **Creation**: must set `hnsw:space=cosine` at creation time (RF-070-11). Local HNSW defaults to L2; SPANN params are immutable after creation.
- **Embedding function**: `None` — always pass pre-computed MiniLM vectors via `embeddings=` / `query_embeddings=` parameters. Do NOT use VoyageAI EF (wrong vector space).
- **Creation path**: Do NOT use `t3.get_or_create_collection()` — it always injects a VoyageAI EF and sets L2 distance. Instead, call the ChromaDB client directly: `client.get_or_create_collection("taxonomy__centroids", embedding_function=None, metadata={"hnsw:space": "cosine"})`. Add a dedicated helper `create_centroid_collection(client)` in the discover pipeline to encapsulate this. Collection name `taxonomy__centroids` follows the double-underscore naming convention.

No pickle files. No `~/.config/nexus/taxonomy/` directory. Centroids share the same persistence and replication story as all other ChromaDB data.

**Centroid lifecycle on re-discovery**: `discover --force` must clear all existing centroids before upserting new ones. Sequence: (1) read old centroids + labels for merge strategy (see Phase 2), (2) delete all entries from `taxonomy__centroids` collection, (3) upsert new centroids. This prevents stale centroid accumulation — old topic IDs would otherwise remain in ChromaDB and pollute ANN lookups with dangling references to deleted T2 topics.

**Recovery**: if centroid collection is missing, incremental assignment falls back to no-op (document stored without topic assignment). `nx taxonomy discover` rebuilds centroids from T3 embeddings. Operator decisions in T2 are preserved across rebuilds via the merge strategy (Phase 2).

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

The existing `nx taxonomy rebuild` becomes an alias for `nx taxonomy discover --force`. **Semantic change**: the old `rebuild` ran fast Ward clustering on T2 memory entries (~100ms). The new `discover --force` runs HDBSCAN on T3 embeddings with MiniLM re-embedding (~seconds for large collections). Print a one-line notice on `rebuild`: "Running full re-discovery (this replaces the old Ward clustering)."

## MCP Tool Changes

```
search(..., topic="label")   # new parameter: pre-filter by topic
search(..., cluster_by="semantic")  # already exists, becomes default
```

## Phasing

| Phase | Scope | Depends on | Effort |
|-------|-------|------------|--------|
| P1 | sklearn HDBSCAN integration + discover CLI + post-index auto-trigger + post_store_hook | — | 3-4 days |
| P2 | `nx taxonomy review` interactive CLI + rebalance trigger | P1 | 3-4 days |
| P3 | Clustered search default-on + topic-scoped search | P1 (HDBSCAN engine: nexus-9k5) | 2-3 days |
| P4 | Link graph integration + topic-aware boost | P1, P3 | 2-3 days |

P3 can start after the HDBSCAN engine bead (nexus-9k5) merges — it does not require all P1 beads to complete. P3 needs topics to exist in T2 and centroids in ChromaDB; it does not need the post_store_hook, CLI discover command, or validation gate.

## Success Criteria

1. `nx taxonomy list` shows a non-empty topic tree after `nx index repo .` on a corpus with ≥ `min_cluster_size` documents — discovery triggers automatically post-index, no manual command needed. (Below `min_cluster_size`, HDBSCAN correctly produces 0 topics; `discover` logs a warning.)
2. `nx search "schema evolution"` returns results grouped by topic, not flat.
3. `nx taxonomy review` after a rebalance presents ≤60 topics, reviewable in 2-4 sessions of 10-15 topics each.
4. An agent running `search(topic="schema-evolution", query="mapping composition")` gets results scoped to that topic.
5. The operator can answer "what topics does nexus know about?" in under 5 seconds via `nx taxonomy list`.

## Open Questions (All Resolved)

1. **Embedding source**: **RESOLVED → Local MiniLM 384d (RF-070-4).** Cross-model Voyage cosine similarity is ~0.05 (documented noise). MiniLM serves as the unified topic-assignment space. ~1ms per doc, no API calls. MiniLM ceiling: ~50 reliable topics (RF-070-8); upgrade to bge-base 768d if >60 topics needed.
2. **Cross-collection topics**: **RESOLVED → MiniLM unified space (RF-070-7).** Per-collection Voyage embeddings are incompatible (cross-model mean distance 1.005 ≈ random). All topic math uses MiniLM re-embedding.
3. **Topic hierarchy**: **RESOLVED → Flat, split on demand (RF-070-8).** At 10K docs / 30-60 topics, average topic size is 170-330 — well below the ~2K threshold where hierarchy helps.
4. **Threshold approach**: **RESOLVED → HDBSCAN density-based clustering (RF-070-7, RF-070-9).** Manual thresholds were empirically invalid (real means: code 0.56, prose 0.52). HDBSCAN discovers clusters from natural data structure.
5. **Tool choice**: **RESOLVED → sklearn HDBSCAN + ChromaDB centroid ANN (RF-070-9, RF-070-11).** BERTopic rejected: eagerly imports sentence-transformers + PyTorch (~500MB). sklearn provides the same HDBSCAN at 1/17th install weight. c-TF-IDF labels via CountVectorizer/TfidfTransformer. Incremental via ChromaDB HNSW/SPANN centroid lookup.
6. **Existing code**: **RESOLVED → Replace.** `catalog_taxonomy.py` word-frequency pipeline is deleted. T2 schema retained. sklearn HDBSCAN is the engine, T2 is the durable store, ChromaDB holds centroids.

## Research Findings

### RF-070-1: Current Data Shape
- 9,361 documents across 763 collections, 5 content types
- 981 real links (after `implements-heuristic` noise removal)
- Largest collection: `code__ART-8c2e74c0` (4,168 docs) — likely needs multiple topics
- 173 entries in `knowledge__knowledge` — cross-project knowledge base, most in need of topic structure

### RF-070-2: Existing Infrastructure
- `search_clusterer.py`: 91 LOC, Ward + k-means. Retained for Phase 3 fallback.
- `catalog_taxonomy.py`: 520 LOC, word-frequency clustering. **Replaced by sklearn HDBSCAN** — T2 schema retained, clustering engine swapped.
- `auto_linker.py`: 106 LOC, single-responsibility for link creation. **Not extended** — separate `post_store_hook` for topic assignment (RF-070-6).
- `scoring.py`: `_LINK_BOOST_WEIGHTS` dict. Adding topic boost is mechanical (Phase 4).

### RF-070-3: Word-Frequency vs. Embedding Clustering
The existing `cluster_and_persist` uses word-frequency vectors. This works for memory entries but not for code chunks. sklearn HDBSCAN operating on MiniLM 384d embeddings replaces this entirely.

### RF-070-4: Cross-Model Embedding Incompatibility (HIGH confidence)
Cross-model cosine similarity between `voyage-code-3` and `voyage-context-3` is ~0.05 — documented noise. `EmbeddingModelMismatch` error class enforces this. **Use local MiniLM 384d** as the unified topic embedding space. ~1ms per doc, no API calls, deterministic.

### RF-070-5: Original Manual Thresholds (SUPERSEDED by RF-070-7 + RF-070-9)
~~Calibrated from search noise-floor data: ≤0.35 prose / ≤0.25 code.~~ These were empirically invalid (RF-070-7). HDBSCAN's density-based approach eliminates the need for manual thresholds entirely.

### RF-070-6: Hook Point for Incremental Assignment (HIGH confidence)
Add `post_store_hook` callback in `mcp_infra.py`. Do NOT extend `auto_linker.py`. Hook re-embeds content with local MiniLM (~1ms) independently — `t3.put()` return value is unused because Voyage embeddings are in a different vector space than the MiniLM topic space (audit S1). Batch indexing unaffected.

### RF-070-7: Empirical Distance Distributions (HIGH confidence — 7,350 pairwise measurements)
Measured intra-collection mean pairwise cosine distances across 6 production collections (132,691 total docs):

| Content type | P10 | Median | Mean | P90 |
|---|---|---|---|---|
| Code (3 collections) | 0.40-0.43 | 0.53-0.62 | 0.53-0.60 | 0.65-0.72 |
| Prose (2 collections) | 0.12-0.24 | 0.54-0.55 | 0.51-0.53 | 0.72-0.86 |
| Knowledge | 0.52 | 0.74 | 0.71 | 0.85 |

Cross-model (voyage-code-3 vs voyage-context-3) averages **1.005** — random. MiniLM same-project cross-type delta: +0.228. **Manual thresholds are fragile; HDBSCAN is the right abstraction.**

### RF-070-8: Topic Modeling Literature (MEDIUM-HIGH confidence, PARTIALLY UPDATED)
- **Expected topics**: 30-60 globally, 5-15 per major collection
- **Hierarchy**: flat sufficient at 10K; hierarchy helps above ~2K docs per topic level
- **MiniLM ceiling**: ~50 reliable topics; conflation exceeds 20% above that
- **Incremental quality**: ~~85-90% agreement with batch~~ — RETRACTED. This figure was researched for BERTopic `approximate_predict`, which uses HDBSCAN's learned density topology. The sklearn pivot uses nearest-centroid ANN lookup (1-NN), which is mathematically different: centroid-based assignment can misassign for non-spherical clusters. **P1 validation task added (nexus-7m8)**: measure nearest-centroid vs. batch agreement on `code__ART` 10% holdout before Phase 2. Rebalance trigger remains at 2x corpus growth — adjust if measured agreement is below 75%.
- **Operator review**: 10-15 topics per session, 2-4 sessions total for 30-60 topics
- **Code topics**: syntactically driven in MiniLM — label from structure, not identifiers

### RF-070-9: Taxonomy Tool Survey (HIGH confidence, UPDATED)
**Winner: sklearn HDBSCAN + ChromaDB centroid ANN.** Original survey selected BERTopic, but implementation (nexus-86v) found BERTopic eagerly imports sentence-transformers + PyTorch (~500MB). Pivoted to `sklearn.cluster.HDBSCAN` (core dep since scikit-learn 1.3, 30MB) — same density-based clustering algorithm. c-TF-IDF labels via `CountVectorizer`/`TfidfTransformer`. Incremental assignment via ChromaDB `taxonomy__centroids` collection (HNSW local / SPANN cloud ANN index) instead of `approximate_predict`.

Rejected: BERTopic (500MB torch dep chain, eagerly loaded), Top2Vec (no pre-computed embedding API), Gensim LDA (bag-of-words only), Owlready2/SKOS (ontology, not discovery), Lilac (dead), Nomic Atlas (cloud-only), Argilla (overkill), Neo4j/SPARQL (overkill at 10K).

### RF-070-11: Centroid Indexing via ChromaDB HNSW/SPANN (HIGH confidence)
Store HDBSCAN centroids in `taxonomy__centroids` ChromaDB collection instead of pickle files. Key requirements:

1. **`hnsw:space=cosine` at creation** — local HNSW defaults to L2; MiniLM embeddings are normalized. SPANN always cosine. SPANN params immutable after creation.
2. **No VoyageAI EF** — always pass `embeddings=` / `query_embeddings=` explicitly. MiniLM 384d vectors, not Voyage.
3. **Auto-indexing on upsert** — no separate build step. HNSW builds graph incrementally. <1K centroids → near brute-force, <1ms.

Source: t3.py:316-334 (get_or_create_collection), t3.py:949-970 (apply_hnsw_ef), RDR-056 RF-17 (SPANN immutability).
