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

**Rule (Java service surface)**: Never add a chunk write path in the Java service that bypasses
`PgVectorRepository.upsertChunks`.  This rule governs service-mode writes only; local-mode
Python clients write directly to Chroma (bypassing PostgreSQL) and are outside this rule's
scope until RDR-155 P4b removes the Chroma path.

## Capability-selection discipline (RDR-156 Decision 8)

When a schema-level invariant or read shape needs enforcing, choose the **least powerful
mechanism that suffices**, in this order (the RDR-154 P3 boundary, carried forward):

> **declarative FK / constraint  >  stored function  >  `security_invoker` view  >  trigger**

A trigger is the last resort — admissible **only for an invariant the application layer
genuinely cannot enforce** ("app-unfixable"). Every RDR-156 choice was recorded against
this ladder; the entries below are the deliverable (not an aspiration), so a future change
that reaches for a heavier mechanism has to argue past them.

| RDR-156 decision | Mechanism chosen | Why not heavier |
|---|---|---|
| chunk → collection referential integrity | **declarative FK** (`chunks_<dim>/chash_index/topic_assignments` → `catalog_collections`, `NOT VALID` until RDR-153, then `VALIDATE`) | A FK is declarative and authoritative-by-construction; no function/trigger needed. Cost is one index probe per upsert (negligible vs the embedding call). |
| manifest → chunk integrity (orphan detection) | **stored function** `nexus.manifest_orphans(dim)` + the P2.1 fail-loud read backstop | A FK is impossible (`catalog_document_chunks.chash` can't reference chunks split across three dim tables); the orphan class is adequately served on-demand by a function — no parent table, no trigger. |
| manifest backfill / document reconstruction | **stored functions** `manifest_backfill()`, `document_text(doc_id)` | Replace generated-SQL-string artifacts with first-class DB objects callable by doctor / migration validation; no triggers, no app round-trips. |
| per-collection stats | **`security_invoker` view** `collection_vector_stats` | Read-only aggregate; a view under the caller's RLS is exactly right — replaces remote `count()` calls. No function needed. |
| combined-query read shapes | **set-returning `LANGUAGE sql` functions** (`search_metadata_scoped` / `search_topic_scoped` / `search_graph_hop`) | Must take the query vector as a plan-time argument (a view can't), and stay inlinable so HNSW survives the join. Functions, not views, not triggers. |
| soft delete (tombstone) | **plain column + partial indexes + view filters** (`deleted_at`, `live_chunks`) | **Adds ZERO triggers.** Tombstoning is an `UPDATE`, so the `ON DELETE CASCADE` chains do not fire; restore clears one column. Cascade semantics are declarative. |
| collection registration-before-write | **app-side ordering in `upsertChunks` + the FK** (see the section above) | The FK makes the ordering load-bearing; the registration is done in the same transaction. No trigger to maintain the invariant. |

### The `chunks_registry` trigger — recorded NOT-worth-it (this RDR's entry)

A trigger-maintained `chunks_registry` parent table was considered as a real FK anchor for
`catalog_document_chunks.chash`. **Rejected today**: it adds a trigger on the hottest write
path (chunk upsert), couples write-ordering (chunk row before manifest row) onto the hot
indexing path, and sits outside the "app-unfixable only" bar — the orphan class it would
guard is already served by `manifest_orphans()` + the fail-loud read backstop. **Revisit only
if orphan incidents recur post-RDR-153.** (Other rejected anchors: a single partitioned
`chunks` table — impossible, `vector(n)` is fixed-dimension per column; `manifest.chash →
chash_index` — wrong lifecycle.)

### RDR-154 entries (the ladder's origin)

The ladder above was first recorded by RDR-154 (Decision 4). RDR-154's own per-decision
choices against it:

| RDR-154 decision | Mechanism chosen | Why not heavier / lighter |
|---|---|---|
| `topics.doc_count` (denormalized count, hot `ORDER BY doc_count DESC`) | **trigger** — statement-level `AFTER INSERT OR DELETE ON topic_assignments`, sole writer, `SECURITY INVOKER` | The cascade-delete hole is genuinely **app-unfixable**: the `topics` row survives when a `catalog_documents` delete cascades away its `topic_assignments`, so the counter strands stale-high and no application write path can see it. A plain computed view was considered and rejected — `ORDER BY doc_count DESC` over a `LEFT JOIN … COUNT` loses the `idx_topics_tenant_collection_count` index on a hot read path. (Contrast `chunk_count`: it lives on `catalog_documents`, which is deleted *with* its counter, so it cannot strand — **no trigger**, HTTP resync suffices.) |
| read-shapes (`catalog_stats`, `collection_doc_counts`, `coverage_by_content_type`, `collection_health_meta`, `topics_with_counts`; plus `links_by_type_counts` added in P1.2 as the `links_by_type` half of the "5+2" `stats()` collapse — §Approach P1 names the first five, Gap 3 names the two group-bys) | **`security_invoker` views** | Derived read-only shapes; a view under the caller's RLS is exactly right and kills the Java↔Python hand-assembly + an N+1. Every view over a tenant (RLS) table MUST be `WITH (security_invoker = true)` — a default `security_definer` view silently bypasses `FORCE ROW LEVEL SECURITY`. Enforced by `tests/db/test_view_security_invoker_guard.py`. |
| `updated_at` on `document_aspects` + `topics` | **trigger** — shared `BEFORE UPDATE FOR EACH ROW` `stamp_updated_at()` (`SECURITY INVOKER`) | Multiple writers and no purpose-built mutation timestamp; a DB-enforced stamp is the only way to guarantee it moves on every partial UPDATE. Added to **exactly these two tables** — NEVER to tables with a fit-for-purpose timestamp, NEVER to the append-only logs. |

**NOT-worth-it list (RDR-154 Alternatives considered) — do not reach for a trigger here:**

- **Dangler-logging triggers** — `allow_dangling` is intentional; danglers belong in RDR-153's batch report, not a write-path trigger.
- **Queue state-machine guards** — already enforced by `WHERE`-guarded `UPDATE`s + `FOR UPDATE SKIP LOCKED`.
- **Register-as-function** — catalog register is already one atomic `FOR UPDATE` transaction.
- **ETL upserts** — single-statement `ON CONFLICT`; no trigger needed. (A column a trigger/generated mechanism maintains must NOT be an ETL `ON CONFLICT` merge participant — see `doc_count`, dropped from the taxonomy ETL.)

**Matview deferral (RDR-154 Decision 3):** `top_topics`/ICF projection aggregates and
`telemetry_collection_stats` are deferred — plain `security_invoker` views suffice today.
Promote to a materialized view ONLY when a read-hot signal (measurable latency on that read
path) justifies the refresh machinery. A matview over a tenant table MUST carry `tenant_id`
and be fronted by a thin `security_invoker` wrapper view re-applying the tenant filter;
consumers query the wrapper, never the matview.

## Hot rules

- **No ORM.** SQLAlchemy etc. is banned. Direct `sqlite3` only.
- **WAL mode on open.** Every connection opens with `PRAGMA journal_mode=WAL`. Already centralised — don't override.
- **Never edit a shipped migration.** If you need to change earlier behaviour, add a corrective migration. Editing breaks every user past that version.
- **Pagination must respect `_PAGE = 300`.** When walking a large collection, `offset += 300` in a loop. Same cap on writes (`MAX_RECORDS_PER_WRITE`).
