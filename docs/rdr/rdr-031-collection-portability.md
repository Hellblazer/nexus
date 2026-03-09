---
title: "Collection Portability — Export/Import for T3 Backup and Migration"
id: RDR-031
type: Feature
status: draft
priority: P3
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-08
related_issues: []
related_tests: []
implementation_notes: ""
---

# RDR-031: Collection Portability — Export/Import for T3 Backup and Migration

## Problem Statement

Nexus has no way to:
1. **Export** a T3 collection for backup or sharing
2. **Import** a collection onto a different machine or ChromaDB instance
3. **Migrate** data when changing ChromaDB providers or plans

Rebuilding collections from source requires re-indexing (expensive: Voyage AI API costs ~$0.15 per 1000 chunks) and re-embedding (slow: minutes to hours for large repos). `code__ART` alone has 94,059 chunks.

Arcaneum designed a portable `.arcexp` format (RDR-017) with msgpack + numpy + gzip compression achieving 10x reduction vs JSON, plus path remapping for cross-machine migration. This validates the need and provides a reference implementation.

## Context

- ChromaDB Cloud has no built-in export/import
- 26 collections across 4 databases, ~167K total chunks
- Re-indexing cost: ~$25 for all collections at current Voyage AI rates
- Arcaneum's design at `/Users/hal.hildebrand/git/arcaneum/docs/rdr/rdr-017-collection-portability.md`

## Research Findings

### F1: ChromaDB Collection Data Access (Verified — API)

Collections expose `col.get(include=["documents", "metadatas", "embeddings"])` which returns all stored data. However, the 300-record pagination limit means large collections require batched retrieval.

### F2: Arcaneum Format Design (Verified — RDR-017)

Arcaneum's `.arcexp` format:
- Header: format version, collection name, record count, embedding dimension
- Body: msgpack-serialized records with numpy arrays for embeddings
- Compression: gzip (10x reduction)
- Features: `--detach`/`--attach` for path remapping, `--include`/`--exclude` glob filters

### F3: Embedding Portability (Verified — Voyage AI docs)

Voyage AI embeddings are deterministic for the same input — exported embeddings can be imported without re-embedding. The embedding model name must be stored with the export to prevent model mismatch.

## Proposed Solution

### `nx store export`
```bash
nx store export code__myrepo -o myrepo-backup.nxexp
nx store export code__myrepo --include "*.py" -o python-only.nxexp
nx store export --all -o full-backup.nxexp
```

### `nx store import`
```bash
nx store import myrepo-backup.nxexp
nx store import myrepo-backup.nxexp --remap "/old/path:/new/path"
nx store import myrepo-backup.nxexp --collection code__newname
```

### Format: `.nxexp` (Nexus Export)
- Header: JSON — format_version, collection_name, database_type, embedding_model, record_count, embedding_dim, exported_at, pipeline_version
- Body: msgpack — list of {id, document, metadata, embedding (numpy bytes)}
- Compression: gzip
- Extension: `.nxexp`

## Alternatives Considered

**A. JSON export**: Simple but 10x larger and slow for large collections. No binary embedding support.

**B. SQLite export**: Portable and queryable, but heavier than needed for a transfer format.

**C. ChromaDB persist directory copy**: Only works for local ChromaDB, not cloud. No path remapping.

## Trade-offs

**Benefits**:
- Backup/restore for cloud ChromaDB collections
- Machine migration without re-embedding ($25+ saved per full export)
- Selective export with glob filters
- Path remapping for cross-machine portability

**Risks**:
- 300-record pagination requires careful batching for export
- Large exports consume significant disk (167K chunks × ~4KB avg = ~650MB uncompressed, ~65MB compressed)
- Model version mismatch on import could produce mixed embedding spaces

## Implementation Plan

1. Create `src/nexus/exporter.py` with `export_collection()` and `import_collection()`
2. Implement batched `col.get()` with 300-record pagination
3. Implement `.nxexp` format (JSON header + msgpack body + gzip)
4. Add `nx store export` CLI command
5. Add `nx store import` CLI command with `--remap` and `--collection` options
6. Add `--all` flag for full backup
7. Add model version validation on import

## Test Plan

- Unit: export/import round-trip preserves all data (documents, metadata, embeddings)
- Unit: pagination correctly handles collections > 300 records
- Unit: gzip compression/decompression
- Unit: path remapping transforms source_file metadata
- Unit: model version mismatch warning on import
- Integration: export from cloud, import to ephemeral, verify search results match

## References

- Arcaneum RDR-017: Collection portability design
- ChromaDB `get()` API with pagination
- msgpack-python: efficient binary serialization
