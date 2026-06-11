# `nexus.db` — AGENTS.md

T1, T2, and T3 implementations. The interesting policy lives in T2's migration registry and the ChromaDB quota wall.

## Modules

| File | Purpose |
|---|---|
| `t1.py` | `T1Database` — ephemeral or per-session HTTP `chromadb` client. Session-id lease discovery via `daemon/t1_lease.py` (RDR-149 P4); the MCP lifespan publishes the lease. |
| `t2/` | Package: seven domain stores + `T2Database` facade. See **T2 domain stores** below. |
| `t3.py` | `T3Database` — persistent local (`PersistentClient` + ONNX) or cloud (`CloudClient` + Voyage) routing keyed on `is_local_mode()`. |
| `local_ef.py` | `LocalEmbeddingFunction` — bundled ONNX MiniLM. Used by T1 always and by T3 in local mode. |
| `chroma_quotas.py` | **Single source of truth** for ChromaDB Cloud caps. Constants + `QuotaValidator`. Imported wherever a ChromaDB call is constructed. |
| `migrations.py` | Centralised T2 migration registry. `Migration` dataclass, `apply_pending()`, `T3UpgradeStep`, version tracking (RDR-076). |

## T2 domain stores

| Store | Purpose |
|---|---|
| `MemoryStore` | Persistent notes + FTS5 (`nx memory`). |
| `PlanLibrary` | Plan templates with TTL auto-expiry. 12 builtin templates seeded at `nx catalog setup`. |
| `CatalogTaxonomy` | HDBSCAN topic discovery, assignments, taxonomy meta, topic links (RDR-070). |
| `Telemetry` | Relevance log. |
| `ChashIndex` | Content-hash chunk index (RDR-086). Dual-write hook ensures rows exist before topic assignment. |
| `DocumentAspects` | Structured aspect rows (RDR-089). |
| `AspectExtractionQueue` | WAL queue drained by `aspect_worker.py` daemon thread. |

`T2Database` is the only thing other modules should hold. Stores are accessed via `t2.memory`, `t2.plans`, etc.

## Migration policy (RDR-076)

Migrations are **version-gated** and live in `migrations.py` as a registry. Each `Migration` carries:

- `version` — monotonic integer
- `description` — human-readable
- `apply_fn` — `(conn) -> None`, idempotent

`apply_pending(conn)` reads `schema_version` from the meta table and runs every newer migration in order. New migrations land as **additional** rows; **never edit a migration that has shipped** — write a follow-up.

`T3UpgradeStep` is the parallel mechanism for T3-side upgrades that aren't SQL — collection re-creates, embedder swaps, etc. Same registry pattern, separate version counter.

`nx doctor --check-schema` validates that the on-disk schema matches the version the registry claims. `nx upgrade --dry-run` shows what `apply_pending` would do.

## ChromaDB quota wall

Every code path that constructs a ChromaDB call **must** consult `chroma_quotas.py` constants. The quotas are not aspirational — exceeding them produces `ChromaError: Quota exceeded` at runtime. See the table in the project root [`AGENTS.md`](../../../AGENTS.md#external-service-limits--check-before-every-call).

The chunk size cap is the load-bearing one: `MAX_DOCUMENT_BYTES = 16384`, but writers should target `SAFE_CHUNK_BYTES = 12288` to leave headroom for context-prefix padding.

## Adding a new T2 migration

1. Pick the next version number (current max + 1).
2. Add a `Migration(version=N, description="...", apply_fn=_migrate_N)` entry to the registry list at the top of `migrations.py`.
3. Implement `_migrate_N(conn)` below. **Idempotent** — re-running on an already-migrated DB is a no-op (use `IF NOT EXISTS` etc.).
4. Add a test in `tests/test_db_migrations.py`. At minimum: blank-DB-runs-clean and replay-is-noop.
5. Run `./tests/e2e/release-sandbox.sh smoke` — schema migrations are sandbox-required.
6. Run `nx doctor --check-schema` against the editable install.

## Collection registration precedes chunk writes (RDR-156 P0.2)

Collection registration is enforced server-side at two layers:

1. **`PgVectorRepository.upsertChunks` (Java service)** auto-stubs the collection into
   `nexus.catalog_collections` within the same transaction, before any chunk row is inserted.
   For a conformant name (`<content_type>__<owner_id>__<embedding_model>__v<n>`) the parsed
   segments are stored; for a non-conformant name a name-only stub with empty metadata fields
   is stored.  Either way the FK is satisfied before the chunk row lands.

2. **FK constraints** `chunks_384_collection_fk` / `chunks_768_collection_fk` /
   `chunks_1024_collection_fk` / `chash_index_collection_fk` /
   `topic_assignments_collection_fk` (all `NOT VALID` until RDR-153 data migration lands;
   `VALIDATE CONSTRAINT` is bead nexus-70r3c.3).  `NOT VALID` still enforces ALL new writes.

3. **Stub upgradability**: stub rows (all metadata fields `= ''`) are upgraded in-place by
   `CatalogRepository.importCollection` via `DO UPDATE ... WHERE embedding_model='' AND
   content_type='' AND owner_id=''`.  A re-run never clobbers a genuinely-newer live row.

**Rule**: Never add a chunk write path that bypasses `PgVectorRepository.upsertChunks`.
All chunk writers (Python Chroma clients in local mode, the vector ETL, future writers) must
pass through this method so registration-before-write is structural.

## Hot rules

- **No ORM.** SQLAlchemy etc. is banned. Direct `sqlite3` only.
- **WAL mode on open.** Every connection opens with `PRAGMA journal_mode=WAL`. Already centralised — don't override.
- **Never edit a shipped migration.** If you need to change earlier behaviour, add a corrective migration. Editing breaks every user past that version.
- **Pagination must respect `_PAGE = 300`.** When walking a large collection, `offset += 300` in a loop. Same cap on writes (`MAX_RECORDS_PER_WRITE`).
