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

### Phase 1: Incremental Topic Assignment (the automatic part)

**Assign-on-ingest**: when a new document enters the catalog (via `store_put`, `index repo`, or `index pdf`), compute its similarity to existing topic centroids stored in T2. If the best match exceeds a similarity threshold (default 0.6), assign the document to that topic and update the centroid incrementally. If no match, buffer the document as "unassigned."

**Trigger**: extend `auto_linker.py` to also call `taxonomy.assign_or_buffer(doc_embedding, doc_id, collection)` on every `store_put`. The auto-linker already fires at the right boundary — adding topic assignment is a natural extension.

**Centroid storage**: T2 `topics` table already has a `centroid_hash` column. Extend to store the actual centroid vector (as msgpack blob) so assignment is a single cosine similarity computation, not a full re-cluster.

**Topic spawning**: when the unassigned buffer reaches a threshold (default 10 documents), run a mini-cluster over just the buffered documents. If the clustering produces a coherent group (intra-cluster similarity > 0.5), spawn a new topic. Otherwise, leave them buffered for the next periodic rebalance.

### Phase 2: Periodic Rebalance (the expensive part)

**Full re-cluster**: on `nx catalog setup`, `nx taxonomy rebuild`, or on a configurable schedule (e.g., weekly via the audit loop), run Ward hierarchical clustering over all documents in a collection. This:
- Merges topics that drifted together (centroid similarity > 0.8)
- Splits topics that grew too broad (intra-cluster variance above threshold)
- Reassigns documents that were buffered or misassigned
- Updates all centroid vectors

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

## Open Questions

1. **Embedding source for topic assignment**: use the existing T3 embeddings (voyage-code-3 / voyage-context-3) or compute lightweight local embeddings (MiniLM) for topic math? T3 embeddings are better quality but require API calls; local embeddings are free but lower quality.
2. **Cross-collection topics**: should a topic span collections (e.g., "schema-evolution" includes both `code__nexus` chunks and `docs__chase-papers` chunks)? The current taxonomy is per-collection. Cross-collection topics are more useful but require a shared embedding space.
3. **Topic hierarchy depth**: flat (1 level) vs. hierarchical (2-3 levels)? The existing schema supports hierarchy (`parent_id`). Flat is simpler to start; hierarchy can emerge in rebalance if a topic grows large enough to split meaningfully.
4. **Buffer threshold**: how many unassigned documents before attempting a mini-cluster? Too low (3) creates noise topics. Too high (50) delays topic discovery. Default 10 is a guess.

## Research Findings

### RF-070-1: Current Data Shape
- 9,361 documents across 763 collections, 5 content types
- 981 real links (after `implements-heuristic` noise removal)
- Largest collection: `code__ART-8c2e74c0` (4,168 docs) — likely needs multiple topics
- 173 entries in `knowledge__knowledge` — the cross-project knowledge base, most in need of topic structure

### RF-070-2: Existing Infrastructure
- `search_clusterer.py`: 91 LOC, Ward + k-means, clean API, tested. Needs no changes for Phase 3.
- `catalog_taxonomy.py`: 520 LOC, full T2 domain store with schema, locking, tree queries. Needs centroid storage + incremental assign for Phase 1.
- `auto_linker.py`: 106 LOC, fires on every `store_put`. Natural extension point for Phase 1 assignment.
- `scoring.py`: already has `_LINK_BOOST_WEIGHTS` dict. Adding topic boost is mechanical.

### RF-070-3: Word-Frequency vs. Embedding Clustering
The existing `cluster_and_persist` uses word-frequency vectors (TF-IDF-like). This works for memory entries (short text, English) but not for code chunks (identifiers, mixed languages). Phase 1 should use the document's actual embedding vector (already stored in T3) for topic assignment, falling back to word-frequency when embeddings are unavailable.
