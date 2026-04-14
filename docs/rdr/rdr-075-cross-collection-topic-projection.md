---
title: "RDR-075: Cross-Collection Topic Projection"
status: closed
close_reason: implemented
type: feature
priority: P2
created: 2026-04-13
accepted_date: 2026-04-14
closed_date: 2026-04-14
github_issue: 154
reviewed-by: self
---

# RDR-075: Cross-Collection Topic Projection

`taxonomy discover` clusters chunks within a single collection using HDBSCAN, then labels the clusters. Collections never see each other's topics. The taxonomy has 2100+ topics across 238K chunks, but a new collection's chunks are never compared against existing topic centroids.

## Problem

Every collection is a semantic island. When `docs__art-architecture` (76 chunks, 10 architecture reference docs) runs `discover`, it finds 6 internal topics. But those 76 chunks are semantically connected to **80+ existing topics** across 5 ART collections — and the system has no idea.

| Internal Topic (6) | Related External Topics | Ext. Chunks |
|---|---|---|
| Implemented architecture phase documentation | Cortical Adaptive Resonance Learning, Adaptive Resonance Theory | 538, 465 |
| Binder 65D vector perception | Vocabulary Grounding in Dialogue, Visual Language Grounding Models | 674, 656 |
| Chatsome language production pipeline | Phoneme Analysis Processing Pipeline, Chat Emotion and Drive | 830, 412 |
| Closed Grossberg RDR work | Neural Network Dynamics, Grossberg Adaptive Neural Cortex, 60+ topics | 789, 638, ... |
| Ashby variety resonance dynamics | Speech Rate and Silence, Semantic Word Resonance Generation | 171, 37 |
| Vocabulary scaling latency analysis | (novel — minimal overlap) | — |

This is the difference between a knowledge **graph** and a knowledge **filing cabinet**.

### Root Cause

`discover()` runs HDBSCAN on a single collection's embeddings. Centroids are stored in the `taxonomy__centroids` ChromaDB collection (1024d voyage vectors, cosine HNSW index) with per-topic metadata. The `centroid_hash` column in the SQLite `topics` table exists but is unused. `topic_assignments` maps chunk→topic within a collection only — `assign_single()` and `assign_batch()` enforce collection isolation via `where={"collection": ...}` filters.

## Proposed Design

### Phase 1: `taxonomy list --collection` filter — ALREADY IMPLEMENTED

```bash
nx taxonomy list --collection docs__art-architecture
```

Already implemented across all taxonomy CLI commands (`list`, `discover`, `rebuild`, `review`, `assign`, `rename`, `merge`, `split`, `links`, `label`). See `taxonomy_cmd.py:228–242`. **No work needed.**

### Phase 2: `taxonomy project` — cross-collection similarity

```bash
nx taxonomy project docs__art-architecture \
    --against knowledge__art,rdr__ART-8c2e74c0,code__ART-8c2e74c0
```

For each chunk in the source collection, compute nearest-centroid distance against all topics in `--against` collections.

**Algorithm**:
1. Load source chunk embeddings from ChromaDB (76 × 1024d)
2. Load target topic centroids from ChromaDB (219 × 1024d for ART collections)
3. Cosine similarity matrix: 76 × 219
4. For each chunk, top-k nearest centroids
5. Aggregate: which topics appear, how many chunks, average distance

**Complexity**: One matrix multiply. 76 × 219 × 1024d ≈ 17M FLOPs. Milliseconds.

### Phase 3: Automatic projection in existing hooks (zero-friction)

Three integration points, all modifying existing code rather than adding commands:

1. **`taxonomy_assign_hook`** (`mcp_infra.py:246–317`): After assigning to same-collection topic, drop the `where={"collection": ...}` filter and query all centroids. Store matches above 0.85 similarity with `assigned_by='projection'`. Fires on every `store_put` — zero user friction.

2. **`discover_topics()` post-pass** (`catalog_taxonomy.py:943–1057`): After creating centroids for collection A, query centroids from sibling collections. Store topic-to-topic matches in `topic_links`.

3. **`index repo` pipeline** (`index.py:167–206`): Add projection step between label and links in the existing chain.

### Phase 4: `taxonomy links` from projection co-occurrence

When chunks from collection A project onto topic T1 in collection B and topic T2 in collection C, create `topic_links` between T1 and T2 weighted by co-occurrence. Cross-collection knowledge graph emerges automatically.

### Phase 5: Corpus-level scoping (convenience)

Add `list_sibling_collections(collection_name) -> list[str]` to `registry.py` — parse the `{basename}-{hash8}` suffix shared across `code__`, `docs__`, `rdr__` for the same repo. This enables `--against` defaulting to siblings and `taxonomy discover --all` chaining the full pipeline (discover → label → project → links).

## Research Findings

### RF-1: Distance threshold for topic matching vs novel content — ANSWERED

**Status**: Answered (codebase evidence)

The centroid collection uses `hnsw:space=cosine` (`catalog_taxonomy.py:893`), so distances are cosine distance (0 = identical, 2 = opposite). Existing `assign_single()` uses ChromaDB's `n_results=1` ANN query and assigns unconditionally — there is **no existing threshold**. The `_merge_labels()` function in `rebuild_taxonomy()` uses cosine similarity ≥ 0.85 as a merge threshold (`catalog_taxonomy.py:835`).

**Recommendation**: Use 0.85 cosine similarity (0.15 cosine distance) as the "match" threshold, consistent with the merge threshold. Chunks below this are "novel content". Empirical calibration on ART collections can refine this.

### RF-2: `topic_assignments.assigned_by` column — CONFIRMED

**Status**: Confirmed (schema + code)

Column exists with `NOT NULL DEFAULT 'hdbscan'` (`catalog_taxonomy.py:84`). Migration added in RDR-070 via `_migrate_assigned_by_if_needed()` (lines 193–208). Current values in use:

| Value | Source | Lines |
|---|---|---|
| `'hdbscan'` | Initial HDBSCAN clustering | 1034 |
| `'centroid'` | Incremental nearest-centroid ANN | 1137 |
| `'manual'` | CLI `taxonomy assign` | taxonomy_cmd.py:541 |
| `'split'` | KMeans sub-clustering | 753 |
| `'auto-matched'` | Rebuild merge strategy | 1370 |

Adding `'projection'` for cross-collection assignments requires **no schema change** — the column is free-text `TEXT`.

### RF-3: `--against` default scope — ANSWERED

**Status**: Design recommendation (codebase analysis)

Current `assign_single()` and `assign_batch()` enforce collection isolation via `where={"collection": collection_name}` filter on centroid queries (`catalog_taxonomy.py:1083–1087, 1125–1129`). Test `test_assign_single_cross_collection_isolation()` (`test_taxonomy.py:290–312`) explicitly validates this boundary.

**Recommendation**: Default to **all collections in the same repo scope** (e.g., all `*__ART-*` collections). The centroid collection already stores `collection` metadata per centroid, so filtering is just a `$nin` / `$in` ChromaDB where clause. Require `--against` for explicit targeting; omission means "all other collections".

### RF-4: Interaction with `apply_topic_boost()` — ANSWERED

**Status**: Compatible (no conflicts)

`apply_topic_boost()` (`scoring.py:274–333`) operates on `dict[doc_id → topic_id]` and optional `topic_links: dict[tuple[int,int], int]`. It modifies result `distance` (not `hybrid_score`):
- Same-topic boost: `−0.1` distance when 2+ results share a topic (lines 318–320)
- Linked-topic boost: `−0.05` distance for related topics (lines 322–331)

Projection-based assignments stored with `assigned_by='projection'` will be **automatically picked up** by the existing search pipeline — `search_engine.py` loads topic assignments by `doc_id` without filtering on `assigned_by`. Phase 4 topic links feed directly into the `topic_links` parameter. **No search code changes needed.**

### RF-5: Performance at scale — ANSWERED

**Status**: Feasible (arithmetic + architecture analysis)

The proposed approach uses ChromaDB's `get()` to retrieve embeddings and numpy for the similarity matrix.

| Scale | Matrix size | FLOPs | Estimate |
|---|---|---|---|
| 76 chunks × 219 centroids × 1024d | 76 × 219 | ~17M | < 1ms |
| 1K chunks × 2100 centroids × 1024d | 1K × 2.1K | ~2.1B | ~50ms |
| 10K chunks × 2100 centroids × 1024d | 10K × 2.1K | ~21B | ~500ms |
| 238K chunks × 2100 centroids × 1024d | 238K × 2.1K | ~512B | ~10s |

The bottleneck is **embedding retrieval from ChromaDB**, not the matmul. `get_embeddings()` (`t3.py:336–349`) fetches by ID list. For 238K chunks this requires batched `get()` calls. The `_LARGE_COLLECTION_THRESHOLD = 5000` (`catalog_taxonomy.py:902`) is the existing switch point from HDBSCAN to MiniBatchKMeans, suggesting 5K is the practical single-call limit.

**Recommendation**: Phase 2 (interactive `project` command) should cap at collection-level projection (typically <10K chunks). Phase 3 (automatic during discover) can batch. Full 238K projection is a background job, not interactive.

### RF-6: Automation touchpoints — existing hooks already fire at the right moments — ANSWERED

**Status**: Answered (codebase analysis)

Three hooks already fire at natural projection boundaries:

| Hook | File | Lines | When it fires | Current scope |
|---|---|---|---|---|
| `taxonomy_assign_hook` | `mcp_infra.py` | 246–317 | After every `store_put` | Single-collection centroid query |
| `discover_for_collection()` chain | `index.py` | 167–206 | After `nx index repo` | discover → label → links (single collection) |
| `auto_linker` | `catalog/auto_linker.py` | 1–107 | After `store_put` catalog registration | Document-level links, not topics |

**Key insight**: `taxonomy_assign_hook` already queries `taxonomy__centroids` for each stored doc. The only change needed for cross-collection projection is **removing the `where={"collection": collection_name}` filter** (or adding a second query without it). This makes every `store_put` automatically project against all existing topics — zero new commands, zero user friction.

Similarly, `discover_topics()` could add a post-discovery projection pass: after creating centroids for collection A, query centroids from all other collections and store matches in `topic_links`.

### RF-7: Current friction — 3 disconnected manual steps collapse to 0 — ANSWERED

**Status**: Answered (workflow analysis)

**Current workflow for cross-collection awareness** (manual, doesn't exist today):
1. `nx taxonomy discover --collection A` — create topics for A
2. (manually inspect topics and guess which collections might be related)
3. (no command exists to compare A's topics against B's topics)
4. `nx taxonomy links --collection A` — compute links from catalog only

**Proposed zero-friction workflow**:
1. `nx index repo /path` or `store_put` via MCP — **everything else is automatic**:
   - discover → label → project against all sibling collections → store projection assignments → compute topic links

The `index repo` pipeline already chains discover → label → links (`index.py:167–206`). Adding projection as step 3 of 4 requires one function call insertion.

### RF-8: No corpus-level grouping exists — registry hash is the implicit key — ANSWERED

**Status**: Answered (codebase analysis)

Collections are named `{type}__{basename}-{hash8}` (`registry.py:73–91`). The `{basename}-{hash8}` suffix is shared across `code__`, `docs__`, `rdr__` for the same repo. But **no code exploits this** — there's no "give me all collections for repo X" function.

**Recommendation**: Add `list_sibling_collections(collection_name) -> list[str]` to `registry.py`. Parse the basename-hash suffix, match against all known collections. This enables `--against` defaulting to "all siblings" without user needing to know collection names.

Example: given `docs__art-architecture`, siblings would be `code__ART-8c2e74c0`, `rdr__ART-8c2e74c0`, `docs__ART-8c2e74c0`, `knowledge__art`.

### RF-9: `taxonomy discover --all` doesn't chain like `index repo` — ANSWERED

**Status**: Answered (code comparison)

`index repo` automatically chains: discover → label → links (`index.py:167–206`). But `taxonomy discover --all` does **only** discovery + optional labeling (`taxonomy_cmd.py:285–366`) — it does **not** compute links afterward. This is an existing inconsistency.

**Recommendation**: Make `taxonomy discover --all` chain the full pipeline (discover → label → project → links) like `index repo` does. Add `--skip-label`, `--skip-project`, `--skip-links` flags for selective execution.

### RF-10: Idempotent upgrade mechanism — RESOLVED by RDR-076

**Status**: Resolved (RDR-076 implemented, PR #155, closed 2026-04-14)

RDR-076 delivered the upgrade infrastructure this finding called for:

- `nx upgrade` CLI with `--dry-run`, `--force`, `--auto` flags
- `T3UpgradeStep` typed interface: `Callable[[T3Database, CatalogTaxonomy], None]`
- `T3_UPGRADES` registry list in `src/nexus/db/migrations.py` (currently empty — ready for RDR-075 backfill)
- `hooks.json` SessionStart: `nx upgrade --auto` runs T2 migrations automatically
- Version tracking via `_nexus_version` table in T2

**For RDR-075**: the projection backfill should be registered as a `T3UpgradeStep` in the `T3_UPGRADES` list. This is the designed integration point — one line addition per T3 upgrade operation. The `--auto` mode skips T3 steps (they require ChromaDB and can exceed hook timeout), so backfill runs only via explicit `nx upgrade`.

### RF-11: T3UpgradeStep integration for projection backfill — ANSWERED

**Status**: Answered (codebase investigation, 2026-04-14)

RDR-076 provides `T3UpgradeStep(introduced, name, fn)` where `fn: Callable[[T3Database, CatalogTaxonomy], None]` (`migrations.py:298–310`). The `T3_UPGRADES` registry is empty with a commented-out template for this exact use case. `upgrade.py:138` has a TODO for T3 step execution.

**Design (resolved)**:
1. Define `backfill_projection(t3_db, taxonomy)` using existing `assign_batch()` (`catalog_taxonomy.py:1053`) — it already handles centroid ANN query, topic assignment, and idempotency via `INSERT OR IGNORE`
2. Enumerate collections via `SELECT DISTINCT collection FROM topics` — only collections with prior discovery need backfill
3. For each collection, fetch embeddings from T3 via `coll.get(include=["embeddings"])`, filter to unassigned doc_ids, call `assign_batch()`
4. Register as `T3UpgradeStep("X.Y.Z", "Backfill cross-collection projection", backfill_projection)`

**Answers to open questions**:
- **One-shot vs re-runnable**: Both. The `T3UpgradeStep` runs once (version-gated). `nx taxonomy project --backfill` provides the same function as a manual re-runnable command.
- **Post-backfill new collections**: Phase 3 hooks (`taxonomy_assign_hook`) handle incremental. The backfill is for the pre-hook install base only.
- **Progress callback**: Not needed — `assign_batch()` returns count of assigned docs; structured logging provides per-collection progress. The `T3UpgradeStep` fn signature stays `Callable[[T3Database, CatalogTaxonomy], None]`.

**Implementation note**: `upgrade.py:138` TODO must be closed — implement T3 step execution by instantiating `T3Database` + `CatalogTaxonomy` and calling `step.fn(t3_db, taxonomy)` for each pending step.

### RF-12: topic_assignments PK is NOT a cross-collection collision risk — ANSWERED

**Status**: Answered (codebase investigation, 2026-04-14)

The gate critic flagged `topic_assignments PRIMARY KEY (doc_id, topic_id)` as lacking collection scope. Investigation shows **this is not a problem for repo-indexing paths**:

- **Repo indexing** (`code_indexer.py:340`, `prose_indexer.py:71`): `doc_id` = SHA-256 of `{corpus}:{title}:chunk{i}` — collection name baked in, cross-collection doc_ids never collide
- **MCP `store_put`** (`t3.py:380`): `doc_id` = SHA-256 of `{collection}:{title}` — also collection-scoped (16-char hex)
- **PDF/doc pipeline** (`doc_indexer.py:546`): `chunk_id` = `{content_hash[:16]}_{chunk_index}` — **NOT collection-scoped**. Two collections indexing identical content share chunk IDs. `INSERT OR IGNORE` prevents crashes but the second collection's projection assignment is silently dropped
- `topic_id` is globally auto-incremented — each topic records its source collection via `topics.collection` column
- `INSERT OR IGNORE` prevents duplicate `(doc_id, topic_id)` pairs — idempotent re-projection

**No schema change needed for repo-indexing and MCP paths.** The `doc_indexer` path has a known limitation: identical content in two collections shares chunk IDs, so only one collection's projection assignment persists per topic. This is acceptable because the `doc_indexer` path is primarily used for single-collection PDF corpora.

### RF-13: assign_single and assign_batch BOTH filter by collection — CORRECTED

**Status**: Corrected (codebase investigation, 2026-04-14)

The gate critic claimed `assign_single()` queries all centroids unscoped. **This is wrong.** Verified:
- `assign_single()` (`catalog_taxonomy.py:1039–1046`) uses `where={"collection": collection_name}` — scoped to same collection
- `assign_batch()` (`catalog_taxonomy.py:1082–1086`) also uses `where={"collection": collection_name}` — scoped

**Both methods enforce collection isolation.** SC-6 is NOT partially live. Phase 3 must add unscoped centroid queries (remove or bypass the `where` filter) for cross-collection projection. Two options:
1. Add `cross_collection: bool = False` parameter that omits the `where` filter
2. Add a separate `project_single()`/`project_batch()` method pair that queries all centroids

### RF-15: Embedding dimension mismatch — real but mitigated by preferred path — ANSWERED

**Status**: Answered (codebase investigation, 2026-04-14)

The gate critic flagged that `taxonomy__centroids` is dimension-locked on first write (1024d Voyage or 384d MiniLM). Investigation confirms:

- `_create_centroid_collection()` (`catalog_taxonomy.py:842–854`) uses `embedding_function=None` — dimension set by first upsert
- `discover_topics()` writes centroids from input embeddings — 1024d if Voyage, 384d if MiniLM
- `taxonomy_assign_hook` preferred path (`mcp_infra.py:298–304`) fetches the T3 embedding (same dimension as stored)
- MiniLM fallback (`mcp_infra.py:308–312`) fires only when T3 embedding unavailable (race condition) — NOT mode-specific

**The dimension mismatch is real but narrow**: it only fires when the preferred T3 embedding fetch fails AND the centroids were created with a different dimension. In practice: (1) cloud-mode discover → 1024d centroids, (2) hook fires on a doc whose T3 embedding isn't available yet → MiniLM 384d → dimension error → silently swallowed by `except Exception: return None`.

**Mitigation**: Add a dimension check before centroid `query()` calls. If `embedding.shape[0] != centroid_coll_dimension`, log a warning and skip instead of silently swallowing. This is a one-line check: `if len(embedding) != centroid_coll.peek(1)["embeddings"][0].__len__()`.

**Design constraint for Phase 3**: All projection queries must use embeddings of the same dimension as the stored centroids. The preferred path (fetch T3 embedding) already guarantees this when the T3 embedding is available. Document: "projection requires mode-consistent centroids — all centroids in `taxonomy__centroids` share one dimension."

### RF-14: 0.85 threshold needs empirical calibration — ANSWERED

**Status**: Answered (design recommendation, 2026-04-14)

RF-1 borrowed the 0.85 cosine similarity threshold from `_merge_labels()` (centroid-to-centroid). The gate critic correctly noted this is a different operation — chunk-to-centroid distances have different characteristics than centroid-to-centroid distances.

**Recommendation**: Accept 0.85 as the initial threshold with two mitigations:
1. Add a `--threshold` parameter to `nx taxonomy project` (default 0.85, configurable)
2. Add SC-9: "Validate 0.85 threshold empirically on ART collections before Phase 3 goes to production — adjust if precision/recall is unacceptable"
3. The threshold is a runtime parameter, not a schema decision — can be tuned without migration

## Success Criteria

- SC-1: ~~`nx taxonomy list --collection <name>` returns only topics for that collection~~ Already implemented
- SC-2: `nx taxonomy project A --against B,C` reports matched topics with chunk counts and distances
- SC-3: Novel chunks (no close centroid ≥ 0.85 similarity) are identified as gaps
- SC-4: Cross-collection assignments stored with `assigned_by='projection'` and automatically benefit search via `apply_topic_boost()`
- SC-5: Topic links generated from projection co-occurrence and persisted in `topic_links` table
- SC-6: `taxonomy_assign_hook` automatically projects new docs against all collections' centroids on every `store_put`. Requires adding unscoped centroid query (both `assign_single` and `assign_batch` currently filter by collection). Dimension must match stored centroids
- SC-7: `nx index repo` and `taxonomy discover --all` chain the full pipeline: discover → label → project → links
- SC-8: `list_sibling_collections()` auto-detects related collections from registry naming convention. Limitation: `knowledge__*` collections without `{hash8}` suffix require explicit `--against` specification
- SC-9: Validate 0.85 cosine similarity threshold on ART collections before Phase 3 production: manual review of 50 random projection assignments, ≥80% judged correct. Threshold configurable via `--threshold` parameter; adjust and re-validate if acceptance threshold not met
- SC-10: `assign_single()` and `assign_batch()` add a dimension check before centroid `query()`: if query embedding dimension does not match `taxonomy__centroids`, log a structured warning and skip. Silent `except Exception: return None` patterns must not mask dimension errors

## Impact

- **Discovery**: "What topics does this new document cover?" — answered instantly
- **Coverage analysis**: "Which topics have no architecture documentation?"
- **Gap detection**: Chunks with no centroid match = genuinely novel content
- **Knowledge graph**: Topic links emerge from projections, not just internal clustering

## Technical Notes

- Centroids stored in `taxonomy__centroids` ChromaDB collection with `hnsw:space=cosine`. **Dimension is fixed on first write** — 1024d for Voyage (cloud mode) or 384d for MiniLM (local mode). A single `taxonomy__centroids` collection cannot contain both dimensions. Mode consistency required: all centroids and all projection queries must use the same embedding model
- **Embedding dimension safety**: `taxonomy_assign_hook` (`mcp_infra.py:308–312`) falls back to MiniLM when Voyage is unavailable. If centroids were created with Voyage (1024d), a MiniLM query (384d) raises a dimension mismatch. The hook's `except Exception: pass` silently swallows this. Phase 3 must add a dimension check before centroid queries and log a warning on mismatch instead of silent swallow
- `centroid_hash` in SQLite `topics` table is unused — actual vectors are in ChromaDB
- `doc_id` is collection-scoped for repo-indexing and MCP paths (hash includes corpus/collection). Exception: `doc_indexer` PDF pipeline uses content-hash chunk IDs without collection scope — identical content in two collections shares chunk IDs (RF-12)
- `topic_assignments.assigned_by` is free-text `TEXT NOT NULL DEFAULT 'hdbscan'` — no schema change needed for `'projection'`
- `topic_links` table already exists (`catalog_taxonomy.py:88–94`) with `upsert_topic_links()` method
- `apply_topic_boost()` reads topic assignments by `doc_id` without filtering `assigned_by` — projection assignments automatically benefit search
- `assign_single()` and `assign_batch()` both enforce collection isolation via `where={"collection": ...}` filter (RF-13). SC-6 requires adding an unscoped query path — e.g., `cross_collection: bool = False` parameter on both methods
- `_LARGE_COLLECTION_THRESHOLD = 5000` — existing switch point from HDBSCAN to MiniBatchKMeans
- `list_sibling_collections()` must handle `knowledge__*` collections that lack `{hash8}` suffix — either exclude from auto-detection or enumerate all T3 collections via `T3Database.list_collections()`
- Phase 1 already implemented. Phase 2 is one matmul + aggregation. Phase 3 is Phase 2 + writes. Phase 4 is Phase 3 + link generation
- T3 backfill registered as `T3UpgradeStep` in `migrations.py` — runs via `nx upgrade` (not `--auto`). Also available as `nx taxonomy project --backfill`
- Related: RDR-070 (taxonomy topic clustering, `apply_topic_boost`), RDR-076 (idempotent upgrade mechanism, T3UpgradeStep)
