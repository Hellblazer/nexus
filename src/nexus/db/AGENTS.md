# `nexus.db` — AGENTS.md

T1, T2, and T3 implementations. The interesting policy lives in the Liquibase/upgrade-ladder split (T2's client-side migration registry is deleted — see § Migration policy below) and the vector-store routing.

**ChromaDB is GONE (RDR-155 P4b, 2026-07-25).** The dependency is dropped, not
merely unused: it is absent from `uv.lock` and not importable. Serving is the
pgvector nexus-service in every mode. `chroma://` URI literals and
`ChromaSchemeHandler.java` survive deliberately — they are a persisted data
format (RDR-169 G3), not a dependency. Pinned by
`tests/test_rdr155_p4b_deletion_gate.py`.

## Modules

| File | Purpose |
|---|---|
| `t1.py` | `T1Database` — session scratch. PG-backed `HttpScratchStore` by default (RDR-152); session-id lease discovery via its own `t1_session_lease.<session_id>` flat file (`publish_t1_session_lease` / `read_t1_session_lease` / `clear_t1_session_lease`), published by the MCP lifespan and refreshed periodically. `daemon/t1_lease.py` (the RDR-149 P4 `ServiceRegistry(tier="t1")` lease this replaced) is retired (nexus-8zfwv, 2026-08-07) — T1 no longer rides the daemon-lifecycle primitive at all. |
| `t2/` | Package: eight domain stores + `T2Database` facade. See **T2 domain stores** below. |
| `t3.py` | `T3Database` — a facade retained for INJECTED clients (tests, `--dry-run`). Production `make_t3()` returns `HttpVectorClient` unconditionally and constructs no vector client of its own (RDR-155 P4a.2). |
| `http_vector_client.py` | `HttpVectorClient` — the production T3: every vector op over `/v1/vectors`, pgvector storage, server-side embedding and rerank (RDR-188). |
| `inmemory_vector_store.py` | `InMemoryVectorClient` — the in-process substitute for tests, the plan-match session cache, and `nx index --dry-run`. Chroma-parity semantics (cosine, `$eq`/`$in`/`$and` where-grammar, upsert/dedup, dimension pinning) are differentially verified, not assumed. |
| `local_ef.py` | `LocalEmbeddingFunction` — client-side EF for local-Python paths only; T3 embeds server-side. Retirement is tracked on `nexus-sghyo` (the client does no embedding). |
| `limits.py` | Load-bearing chunking/paging caps (`SAFE_CHUNK_BYTES`, `MAX_QUERY_RESULTS`), rehomed here from the deleted `chroma_quotas.py` at `nexus-rn3wo.2`. |

## T2 domain stores

| Store | Purpose |
|---|---|
| `HttpMemoryStore` | Persistent notes + full-text search (`nx memory`). |
| `HttpPlanLibrary` | Plan templates with TTL auto-expiry. 17 builtin templates seeded at `nx catalog setup`. |
| `HttpTaxonomyStore` | HDBSCAN topic discovery, assignments, taxonomy meta, topic links (RDR-070). Pure-compute half lives in `taxonomy_compute.py`. |
| `HttpTelemetryStore` | Relevance log + search/hook telemetry + tier writes. |
| `HttpChashIndex` | Content-hash chunk index (RDR-086; table retired by RDR-187 — shim until the 410 flip). |
| `HttpDocumentAspectsStore` | Structured aspect rows (RDR-089). |
| `HttpAspectQueue` | Queue drained by the aspect-worker daemon (PG `FOR UPDATE SKIP LOCKED`). |
| `HttpDocumentHighlightsStore` | Per-document DEVONthink highlight/mention notes, keyed by catalog tumbler (RDR-139 Layer E). Dedicated table, not `document_aspects`. |

All eight are HTTP clients over the engine's PG tables. The SQLite store
classes are DELETED (RDR-158 P4, nexus-i711w), and the
`NX_STORAGE_BACKEND[_<store>]=sqlite` opt-out that selected them
hard-errors with the stranded-install redirect (P3, nexus-7bomn).

`T2Database` is the only thing other modules should hold. Stores are accessed via `t2.memory`, `t2.plans`, etc.

## Migration policy — the client-side chain is DELETED (RDR-158 P4 Stage 4)

`migrations.py` — the `Migration` registry, `apply_pending()`, `T3UpgradeStep`,
`bootstrap_schema`, the RDR-170 registry gating and the RDR-142 dry-run step
resolver — was deleted in nexus-i711w Stage 4. There are NO client-side T2
migrations in any mode: schema is engine-owned via Liquibase, and the local
`.db` files are a frozen migration source (RDR-176 Gap 2) that this version
never writes. Do not add a migration here; a schema change is a Liquibase
changeset in the engine, and a new DATA-convergence axis is an upgrade-ladder
rung (`src/nexus/upgrade_ladder/rungs/`, registered in `registry.py`).
Installs still carrying pre-PG local data migrate via the pinned last
migration-capable 6.x release (the stranded-install two-hop redirect).

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
`PgVectorRepository.upsertChunks`.  This rule now governs every mode — RDR-155 P4b (2026-07-25)
removed the Chroma dependency entirely, so there is no local-mode client write path outside it.

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
| manifest → chunk integrity (orphan detection) | **declarative FK** `catalog_document_chunks (tenant_id, collection, chash) -> nexus.chunks`, `ON UPDATE CASCADE`, deferrable on delete (RDR-191 Phase 5, `catalog-029-manifest-chunk-fk.xml`, VALIDATEd) — **SUPERSEDES this row's original 2026 entry** | RDR-156's original "A FK is impossible" reasoning held only while the chunk store was split across three dim tables (`chunks_384/768/1024`); RDR-191 Phase 4 unified them into one `nexus.chunks` table first, which made the FK expressible. `nexus.manifest_orphans(dim)` and the P2.1 read backstop this row used to name are RETIRED (RDR-191 Phase 6, nexus-o8dil.33, catalog-030) — the FK rejects the dangling state at write time, making the detection function unreachable by construction. This row is the canonical "declarative FK beats a stored function" case the capability ladder above was written to produce; it took two more RDRs to actually reach it. |
| manifest backfill / document reconstruction | **stored function** `document_text(doc_id)` (`manifest_backfill()` RETIRED, RDR-191 Phase 6) | `document_text` replaces a generated-SQL-string artifact with a first-class DB object callable by doctor / migration validation; no triggers, no app round-trips. `manifest_backfill()`'s only reason to exist — pre-stamping `collection` before the (now-retired) orphan/verify functions' `collection IS NOT NULL` filter — died with those functions, and catalog-025's later `NOT NULL` promotion means no future row can need the stamp either. |
| per-collection stats | **`security_invoker` view** `collection_vector_stats` | Read-only aggregate; a view under the caller's RLS is exactly right — replaces remote `count()` calls. No function needed. |
| combined-query read shapes | **set-returning `LANGUAGE sql` functions** (`search_metadata_scoped` / `search_topic_scoped` / `search_graph_hop`) | Must take the query vector as a plan-time argument (a view can't), and stay inlinable so HNSW survives the join. Functions, not views, not triggers. |
| soft delete (tombstone) | **plain column + partial indexes + view filters** (`deleted_at`, `live_chunks`) | **Adds ZERO triggers.** Tombstoning is an `UPDATE`, so the `ON DELETE CASCADE` chains do not fire; restore clears one column. Cascade semantics are declarative. |
| collection registration-before-write | **app-side ordering in `upsertChunks` + the FK** (see the section above) | The FK makes the ordering load-bearing; the registration is done in the same transaction. No trigger to maintain the invariant. |

### The `chunks_registry` trigger — recorded NOT-worth-it (this RDR's entry)

A trigger-maintained `chunks_registry` parent table was considered as a real FK anchor for
`catalog_document_chunks.chash`, back when the chunk store was split across three dim
tables and a direct FK was not expressible. **Rejected at the time**: it would have added a
trigger on the hottest write path (chunk upsert) and coupled write-ordering (chunk row
before manifest row) onto the hot indexing path, sitting outside the "app-unfixable only"
bar — the orphan class it would guard was, at the time, adequately served by
`manifest_orphans()` + the fail-loud read backstop. **RDR-191 (Phases 4-6) mooted this
entirely**: unifying the dim-sharded tables into one `nexus.chunks` made a real declarative
FK expressible (see the ladder table above), which now enforces the SAME invariant the
rejected trigger targeted — with neither a trigger nor the stored function this row
originally named (both retired). No trigger was ever needed; the FK was just blocked on an
earlier decision, not on triggers being the only alternative. (Other rejected anchors: a
single partitioned `chunks` table — impossible, `vector(n)` is fixed-dimension per column;
`manifest.chash → chash_index` — wrong lifecycle, and `chash_index` itself is retired,
RDR-187.)

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

- **No ORM, and NO new SQLite.** Raw SQL through the engine only. The SQLite substrate this module once carried is DELETED (RDR-158 P4 — see the retirement notes above); new persistent state is a Liquibase changeset in `service/src/main/resources/db/changelog/`, never client-side DDL and never a `sqlite3.connect`. A diff adding either is a review **Critical** (project AGENTS.md hot rule, Hal directive 2026-07-18).
- **Never edit a shipped Liquibase changeset.** Schema evolution is a NEW changeset (and, for data migrations, an upgrade-ladder rung in `src/nexus/upgrade_ladder/`). Editing a shipped changeset breaks checksum validation on every installed engine past that version.
- **Pagination must respect `_PAGE = 300`.** When walking a large collection, `offset += 300` in a loop. Same cap on writes (`MAX_RECORDS_PER_WRITE`).
