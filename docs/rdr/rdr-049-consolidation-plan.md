# RDR-049 Consolidation Plan: Collection Merge Strategy

**Status**: Design  
**Bead**: nexus-wowv  
**Prerequisite**: Catalog Phases 1-4 complete, backfill run

## Problem

~95 `docs__` collections in T3, of which ~60 are individual paper collections from legacy per-paper indexing (`docs__L*` pattern). This wastes ChromaDB collection slots and forces explicit enumeration for cross-paper search.

**Target**: ~15-20 corpus-level collections via catalog-guided migration.

## 1. Target Collection Naming

| Collection | Corpus Tag | Description |
|---|---|---|
| `docs__schema-evolution` | schema-evolution | Schema mapping, data integration, temporal databases |
| `docs__distributed-systems` | distributed-systems | Consensus, replication, CRDTs, distributed storage |
| `docs__xanadu-theory` | xanadu-theory | Nelson's hypertext, transclusion, link theory |
| `docs__knowledge-graphs` | knowledge-graphs | Graph databases, ontologies, semantic web |
| `docs__ml-foundations` | ml-foundations | Embeddings, transformers, retrieval-augmented generation |
| `docs__nexus-deps` | nexus-deps | Papers behind nexus library dependencies |
| `docs__misc` | misc | Uncategorized remainder |

Corpus assignment is manual: `nx catalog update <tumbler> --corpus <tag>` per entry after backfill. The `corpus` field on catalog entries drives which source collections map to which target.

## 2. Migration Plan

### Pre-flight

```bash
nx catalog backfill          # Populate catalog from existing T3
nx catalog stats             # Verify document counts
nx catalog list --json > pre-migration-snapshot.json
```

### Per-corpus merge (for each target collection)

```
1. Identify sources:
   SELECT physical_collection FROM documents WHERE corpus = '<tag>'
   → list of source collection names

2. Create target collection:
   t3.get_or_create_collection('<target_name>')
   Copy embedding model metadata from first source collection

3. For each source collection:
   a. col.get(include=["documents", "metadatas", "embeddings"])
   b. Verify: len(ids) == catalog entry chunk_count (warn if mismatch)
   c. target_col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embeds)
   d. Verify: target_col.count() increased by expected amount
   e. cat.update(tumbler, physical_collection='<target_name>')
   f. ONLY after e succeeds: t3.delete_collection(source_name)

4. Final verification:
   target_col.count() == sum of all source chunk counts
```

### Post-migration

```bash
nx catalog stats             # Verify totals unchanged
nx catalog list --json > post-migration-snapshot.json
diff <(jq '.[] | .tumbler' pre-migration-snapshot.json) \
     <(jq '.[] | .tumbler' post-migration-snapshot.json)
# Tumbler addresses must be identical — only physical_collection changes
```

## 3. Rollback Strategy

**Backup**: export each source collection before merge via `col.get(include=["documents","metadatas","embeddings"])` → save to local JSONL/pickle.

**If step 3c fails** (partial upsert into target):
- Delete target collection entirely
- Source collections untouched — no data loss
- Revert catalog pointers: `cat.update(tumbler, physical_collection='<original>')`

**If step 3f fails** (source deletion after successful transfer):
- Harmless: source still exists, target has all data
- Catalog pointer already updated — search works via target
- Retry deletion manually

**Rollback window**: 7 days. Keep backup exports for 7 days post-migration. After confirmation, delete backups.

## 4. Safety Requirements

| Requirement | Check | Enforcement |
|---|---|---|
| Chunk count verification | `len(source_ids) == catalog.chunk_count` | Warn on mismatch, abort on >10% delta |
| Embedding model match | Source and target use same `voyage-context-3` | Assert before upsert |
| ChromaDB ID preservation | Use original chunk IDs in upsert | Pass `ids=` directly |
| Catalog pointer atomicity | Update `physical_collection` only after full transfer verified | Step ordering |
| No duplicate chunks | Use `upsert` not `add` — idempotent on ID collision | ChromaDB upsert semantics |
| Source deletion gated | Delete source only after target count verified | Step 3d before 3f |

## 5. Implementation Notes

- `nx catalog consolidate --corpus <tag> --target <collection> [--dry-run]`
- Dry-run shows source→target mapping and chunk counts without writing
- Progress bar per source collection (reuse `tqdm` pattern from indexer)
- Single-threaded: one source at a time to avoid ChromaDB contention
- Estimated time: ~60 collections × ~50 chunks avg × upsert = minutes, not hours

## 6. Why the Catalog Makes This Safe

Without the catalog, consolidation would break every search query and MCP tool call using old collection names. With the catalog:
- Tumbler addresses are stable (never change)
- `catalog_resolve(tumbler=...)` returns the *current* `physical_collection`
- Links, graph traversal, and all catalog queries are collection-name-agnostic
- Only the `physical_collection` metadata field changes — the identity layer is untouched
