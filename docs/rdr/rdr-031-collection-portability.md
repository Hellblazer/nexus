---
title: "Collection Portability — Export/Import for T3 Backup and Migration"
id: RDR-031
type: Feature
status: accepted
accepted_date: 2026-03-09
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
- Arcaneum's design at `/Users/hal.hildebrand/git/arcaneum/docs/rdr/RDR-017-collection-export-import.md`

## Research Findings

### F1: ChromaDB Collection Data Access (Verified — API)

Collections expose `col.get(include=["documents", "metadatas", "embeddings"])` which returns all stored data. The `QUOTAS.MAX_RECORDS_PER_WRITE` limit (300 records, defined in `chroma_quotas.py`) applies to batched retrieval — large collections require paginated `col.get()` with offset/limit, same pattern used by `expire()` and `purge_deleted_files()` in `t3.py`.

### F2: Arcaneum Format Design (Verified — RDR-017)

Arcaneum's `.arcexp` format:
- Header: format version, collection name, record count, embedding dimension
- Body: msgpack-serialized records with numpy arrays for embeddings
- Compression: gzip (10x reduction)
- Features: `--detach`/`--attach` for path remapping, `--include`/`--exclude` glob filters

### F3: Embedding Portability (Verified — stored vectors)

Exported embeddings are the actual stored float arrays retrieved from ChromaDB via `col.get(include=["embeddings"])`. Importing preserves the original vector space without re-embedding cost. Since export captures pre-computed vectors (not re-computed ones), embedding determinism is not a concern — the exact vectors that produced the original search quality are preserved byte-for-byte.

### F4: Embedding Space Incompatibility (CRITICAL — t3.py:164)

Nexus uses **two incompatible embedding model families** across its T3 databases:

| Collection prefix | Index model | Query model | Vector space |
|---|---|---|---|
| `code__` | `voyage-code-3` | `voyage-4` | voyage-4 space |
| `docs__` | `voyage-context-3` (CCE) | `voyage-context-3` | CCE space |
| `knowledge__` | `voyage-context-3` (CCE) | `voyage-context-3` | CCE space |
| `rdr__` | `voyage-context-3` (CCE) | `voyage-context-3` | CCE space |

As documented in `t3.py:164`: "voyage-4 is **not** compatible with CCE vector spaces (cross-model cosine similarity ≈ 0.05, i.e. random noise)."

Importing a `code__` export (voyage-4 embeddings) into a `docs__` collection (CCE space) — or vice versa — would silently corrupt the target collection's search quality. This is not a "warning" scenario; it is a hard failure.

## Proposed Solution

### `nx store export`
```bash
nx store export code__myrepo -o myrepo-backup.nxexp
nx store export code__myrepo --include "*.py" -o python-only.nxexp
nx store export --all
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

### Format Versioning Policy

The `format_version` field in the export header enables forward compatibility. On import:
- **Same version**: import proceeds normally.
- **Older format version**: import proceeds — newer code must remain backward-compatible with older exports. Breaking changes require a major version bump.
- **Newer format version than importer understands**: import MUST ABORT with an error instructing the user to upgrade Nexus. The importer maintains a `MAX_SUPPORTED_FORMAT_VERSION` constant.

### Embedding Space Validation

On import, the importer MUST validate embedding model compatibility:

1. Read `embedding_model` from the `.nxexp` header.
2. Determine the target collection's expected model via `corpus.index_model_for_collection(target_collection_name)`.
3. If the models differ, import MUST ABORT with an `EmbeddingModelMismatch` error. This is not a warning — mixed embedding spaces produce random-noise cosine similarity (~0.05) and silently destroy search quality.

Example abort message:
```
ERROR: Embedding model mismatch — export uses 'voyage-4' but target
collection 'docs__corpus' requires 'voyage-context-3'. Import aborted.
Re-index from source or export to a compatible collection prefix.
```

### `--include`/`--exclude` Filter Semantics

`--include` and `--exclude` glob patterns match against the `source_path` metadata field (the field used by `indexer.py` for all indexed content — see lines 624, 731, 750, 798).

Entries without a `source_path` metadata field (e.g., `knowledge__` entries created via `nx store put`) pass through filters unconditionally — they are always included in the export regardless of `--include`/`--exclude` patterns.

### `--all` Semantics

`--all` exports every collection across all 4 ChromaDB databases, producing **one `.nxexp` file per collection** with the naming convention `{collection_name}-{date}.nxexp` (e.g., `code__myrepo-2026-03-08.nxexp`). This keeps the format identical to single-collection exports — no combined-archive format needed. Output directory defaults to the current working directory and can be overridden with `-o <dir>/`.

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
- `QUOTAS.MAX_RECORDS_PER_WRITE` pagination (300 records) requires careful batching for both export and import
- Large exports consume significant disk (167K chunks x ~4KB avg = ~650MB uncompressed, ~65MB compressed)
- Embedding model validation is mandatory to prevent silent corruption of mixed vector spaces

## Implementation Plan

1. Create `src/nexus/exporter.py` with `export_collection()` and `import_collection()`
2. Implement batched `col.get()` with `QUOTAS.MAX_RECORDS_PER_WRITE` pagination
3. Implement `.nxexp` format (JSON header + msgpack body + gzip)
4. Add `nx store export` CLI command
5. Add `nx store import` CLI command with `--remap` and `--collection` options
6. Add `--all` flag producing one `.nxexp` per collection
7. Implement embedding model validation on import — call `corpus.index_model_for_collection()` and compare against export header; abort on mismatch with `EmbeddingModelMismatch` error
8. Implement format version check — abort if export `format_version` exceeds `MAX_SUPPORTED_FORMAT_VERSION`
9. Import implementation should target `T3Database.upsert_chunks_with_embeddings()` (`t3.py:373`) — this method accepts pre-computed embeddings and bypasses ChromaDB's embedding function, which is exactly the import use case

## Test Plan

- Unit: export/import round-trip preserves all data (documents, metadata, embeddings)
- Unit: pagination correctly handles collections > 300 records using `QUOTAS.MAX_RECORDS_PER_WRITE`
- Unit: gzip compression/decompression
- Unit: path remapping transforms `source_path` metadata
- Unit: **embedding model mismatch — import `code__` export (voyage-4) into `docs__` target MUST fail with `EmbeddingModelMismatch`**
- Unit: **embedding model match — import `code__` export into `code__` target succeeds**
- Unit: **format version mismatch — import with future `format_version` MUST abort**
- Unit: `--include`/`--exclude` filters match against `source_path` metadata
- Unit: entries without `source_path` (e.g., `nx store put` entries) pass through filters unconditionally
- Unit: `--all` produces one `.nxexp` per collection with correct naming convention
- Integration: export from cloud, import to ephemeral, verify search results match

## Finalization Gate

### Contradiction Check
No contradictions found. The design is consistent with existing Nexus conventions:
- Uses `QUOTAS.MAX_RECORDS_PER_WRITE` for pagination (same as `expire()`, `purge_deleted_files()`)
- Uses `upsert_chunks_with_embeddings()` for import (same pre-computed embedding path used by CCE indexing)
- Embedding model validation aligns with the strict separation already enforced by `corpus.index_model_for_collection()`

### Assumption Verification
Three key assumptions validated:
1. **CCE/voyage-4 split**: Confirmed at `t3.py:164` — cross-model cosine similarity is ~0.05 (random noise). Import MUST enforce model match, not warn. The `corpus.index_model_for_collection()` function (`corpus.py:62`) provides the authoritative model mapping.
2. **300-record pagination**: Confirmed via `QUOTAS.MAX_RECORDS_PER_WRITE = 300` in `chroma_quotas.py:47`. Both `col.get()` for export and `upsert_chunks_with_embeddings()` for import use this limit. Export pagination follows the same offset/limit pattern as `expire()` in `t3.py`.
3. **Embedding determinism**: Moot for this design — export captures stored vectors from ChromaDB, not re-computed ones. No Voyage AI API calls during export or import.

### Scope Verification
Scope is appropriate for a P3 feature:
- Single new module (`exporter.py`) plus two CLI commands
- No changes to existing indexing, search, or storage paths
- Format is intentionally simple (JSON header + msgpack body + gzip) — no need for a database or complex archive format
- `--all` uses per-collection files to avoid introducing a new combined-archive format

### Cross-Cutting Concerns
- **Error handling**: `EmbeddingModelMismatch` and `FormatVersionError` are new error types in `errors.py`
- **Logging**: Export/import progress via `structlog` (chunk count, compression ratio, elapsed time)
- **Config**: No new config — export/import are stateless CLI operations
- **TTL**: Exported metadata includes `expires_at` and `ttl_days`; import preserves them as-is (expiry is evaluated at query time by `expire()`)

### Proportionality
Design complexity matches the problem:
- Format is msgpack + gzip (proven by Arcaneum RDR-017, not novel)
- Embedding validation adds ~10 lines but prevents catastrophic silent corruption
- `--all` is one-file-per-collection (no new archive format)
- Implementation targets an existing API (`upsert_chunks_with_embeddings`) rather than creating new storage paths
- Estimated implementation: ~300 lines of code + ~200 lines of tests

## References

- Arcaneum RDR-017: Collection export/import design (`RDR-017-collection-export-import.md`)
- ChromaDB `get()` API with pagination
- `T3Database.upsert_chunks_with_embeddings()` at `t3.py:373`
- `corpus.index_model_for_collection()` at `corpus.py:62`
- `QUOTAS.MAX_RECORDS_PER_WRITE` at `chroma_quotas.py:47`
- msgpack-python: efficient binary serialization
