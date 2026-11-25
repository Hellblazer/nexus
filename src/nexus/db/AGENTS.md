# `nexus.db` — AGENTS.md

T1, T2, and T3 implementations. The interesting policy lives in the Liquibase/upgrade-ladder split (T2's client-side migration registry is deleted — see § Migration policy below) and the vector-store routing.

**ChromaDB is GONE (RDR-155 P4b, 2026-07-25).** The dependency is dropped, not
merely unused: it is absent from `uv.lock` and not importable. Serving is the
pgvector nexus-service in every mode. `chroma://` URI literals survive
only as residue (RDR-231 renamed the note identity to `nx-note://`) — a
persisted data format (RDR-169 G3), not a dependency. (`ChromaSchemeHandler.java`, the engine's resolver for them, was
removed with `POST /v1/vectors/resolve`, nexus-3t0jv.) Pinned by
`tests/test_rdr155_p4b_deletion_gate.py`.

## Key modules

| File | Purpose |
|---|---|
| `t1.py` | `T1Database` — session scratch. PG-backed `HttpScratchStore` by default (RDR-152); session-id lease discovery via its own `t1_session_lease.<session_id>` flat file (`publish_t1_session_lease` / `read_t1_session_lease` / `end_owned_t1_session_lease`), published by the MCP lifespan and refreshed periodically. `daemon/t1_lease.py` (the RDR-149 P4 `ServiceRegistry(tier="t1")` lease this replaced) is retired (nexus-8zfwv, 2026-08-07) — T1 no longer rides the daemon-lifecycle primitive at all. |
| `t2/` | Package: nine domain stores + `T2Database` facade. See **T2 domain stores** below. |
| `t3.py` | `T3Database` — a facade retained for INJECTED clients (tests, `--dry-run`). Production `make_t3()` returns `HttpVectorClient` unconditionally and constructs no vector client of its own (RDR-155 P4a.2). |
| `http_vector_client.py` | `HttpVectorClient` — the production T3: every vector op over `/v1/vectors`, pgvector storage, server-side embedding and rerank (RDR-188). |
| `inmemory_vector_store.py` | `InMemoryVectorClient` — the in-process substitute for tests, the plan-match session cache, and `nx index --dry-run`. Chroma-parity semantics (cosine, upsert/dedup, dimension pinning) are verified by `tests/test_vector_substrate_contract.py`; its where grammar is the engine's (fields ANDed, one operator per field, `$and`/`$or` refused before any row is read), compared with the engine on seeded filters by `tests/test_where_filter_differential.py` (nexus-3ceda.11). |
| `local_ef.py` | `LocalEmbeddingFunction` — client-side EF for local-Python paths only; T3 embeds server-side. Retirement is tracked on `nexus-sghyo` (the client does no embedding). |
| `limits.py` | Load-bearing chunking/paging caps (`SAFE_CHUNK_BYTES`, `MAX_QUERY_RESULTS`), rehomed here from the deleted `chroma_quotas.py` at `nexus-rn3wo.2`. |

## T2 domain stores

| Store | Purpose |
|---|---|
| `HttpMemoryStore` | Persistent notes + full-text search (`nx memory`). |
| `HttpPlanLibrary` | Plan templates with TTL auto-expiry. Builtin templates (`conexus/plans/builtin/*.yml`) are seeded by `nx init` (`_seed_builtin_plans_best_effort`); `nx catalog setup` is retired. |
| `HttpTaxonomyStore` | HDBSCAN topic discovery, assignments, taxonomy meta, topic links (RDR-070). Pure-compute half lives in `taxonomy_compute.py`. |
| `HttpTelemetryStore` | Relevance log + search/hook telemetry + tier writes. |
| `HttpChashIndex` | Content-hash chunk index (RDR-086; table retired by RDR-187 — shim until the 410 flip). |
| `HttpDocumentAspectsStore` | Structured aspect rows (RDR-089). |
| `HttpAspectQueue` | Producer side of the engine's `aspects` tuple queue (RDR-235): one task per document; the aspect worker drains it. The `aspect_extraction_queue` table and its routes are retired. |
| `HttpDocumentHighlightsStore` | Per-document DEVONthink highlight/mention notes, keyed by catalog tumbler (RDR-139 Layer E). Dedicated table, not `document_aspects`. |
| `HttpTupleStore` | Linda tuple space client over `/v1/tuples` (RDR-205). Service-only from birth. |

All nine are HTTP clients over the engine's PG tables. The SQLite store
classes are DELETED (RDR-158 P4, nexus-i711w), and the
`NX_STORAGE_BACKEND[_<store>]=sqlite` opt-out that selected them
hard-errors with the stranded-install redirect (P3, nexus-7bomn).

`T2Database` is the only thing other modules should hold. Stores are accessed via `t2.memory`, `t2.plans`, etc.

## Migration policy — the client-side chain is DELETED (RDR-158 P4 Stage 4)

`migrations.py` — the `Migration` registry, `apply_pending()`, `T3UpgradeStep`,
`bootstrap_schema`, the RDR-170 registry gating and the RDR-142 dry-run step
resolver — was deleted in nexus-i711w Stage 4. There are NO client-side T2
migrations in any mode: schema is engine-owned via Liquibase, and any local
`.db` file left from the pre-PG era is a relic this version never reads or
writes (RDR-176 Gap 2's downgrade rationale was retired 2026-08-29 — there is
no path back to the Chroma/SQLite era). Do not add a migration here; a schema change is a Liquibase
changeset in the engine, and a new DATA-convergence axis is an upgrade-ladder
rung (`src/nexus/upgrade_ladder/rungs/`, registered in `registry.py`).
Installs still carrying pre-PG local data migrate via the pinned last
migration-capable 6.x release (the stranded-install two-hop redirect).

## Collection registration precedes chunk writes (RDR-156 P0.2, RDR-204, RDR-229)

A collection must have a `nexus.catalog_collections` row before any chunk, taxonomy, aspect or
highlight row names it. The engine never creates that row as a side effect of a write:

1. **A write to an unregistered collection is refused**: HTTP 422, reason `unregistered_collection`
   (`UnregisteredCollectionException`; `nexus.db.engine_reasons`). `PgVectorRepository.upsertChunks`'s
   old auto-registration of a blank "stub" row is retired (RDR-204 Phase 1), and the three NOT-blank
   CHECKs on `content_type`, `owner_id` and `embedding_model` (`hygiene-002-1`) make a stub row
   impossible. The client registers first: `nx index`, `store_put` and the other writers call the
   catalog's collection registration and use the row it returns.

2. **Referential integrity** is a foreign key from each referencing table to the registry.
   `chunks_collection_fk` (`fk-004`) and `topic_assignments_collection_fk` (`fk-002`) are
   name keys (`(tenant_id, name)`): they, and the five other name keys (`canary_query_ring`,
   `document_aspects`, `topics`, `taxonomy_meta`, `document_highlights`), are dropped by the
   RDR-229 name-key drop move (`NameKeyDropMove.NAME_KEYS`) once gate 3 of the completion check
   holds, because a one-row rename cannot update the registry's `name` while they exist. The
   `collection_id` twin keys (`(tenant_id, collection_id)`) carry the integrity from then on.
   See `docs/architecture.md` § Collection identity. The per-dimension `chunks_384/768/1024_collection_fk`
   died with their tables (`vectors-004`, RDR-191); `chash_index_collection_fk` with `chash_index`
   (RDR-187).

3. **Import never overwrites a live row.** `CatalogRepository.importCollection` is `ON CONFLICT
   DO NOTHING`; a recovery bundle cannot clobber a registered collection.

**Rule (Java service surface)**: Never add a chunk write path in the Java service that bypasses
`PgVectorRepository.upsertChunks`, and never add one that registers a collection implicitly. This
governs every mode: RDR-155 P4b (2026-07-25) removed the Chroma dependency, so there is no local-mode
client write path outside it.

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
| chunk → collection referential integrity | **declarative FK** (`chunks_collection_fk` on `nexus.chunks`, and `topic_assignments_collection_fk`, → `catalog_collections`; both VALIDATEd; RDR-229 replaces these name keys with `collection_id` twins and drops them after gate 3) | A FK is declarative and authoritative-by-construction; no function/trigger needed. Cost is one index probe per upsert (negligible vs the embedding call). |
| manifest → chunk integrity (orphan detection) | **declarative FK** `catalog_document_chunks (tenant_id, collection, chash) -> nexus.chunks`, `ON UPDATE CASCADE`, deferrable on delete (RDR-191 Phase 5, `catalog-029-manifest-chunk-fk.xml`, VALIDATEd) — **SUPERSEDES this row's original 2026 entry** | RDR-156's original "A FK is impossible" reasoning held only while the chunk store was split across three dim tables (`chunks_384/768/1024`); RDR-191 Phase 4 unified them into one `nexus.chunks` table first, which made the FK expressible. `nexus.manifest_orphans(dim)` and the P2.1 read backstop this row used to name are RETIRED (RDR-191 Phase 6, nexus-o8dil.33, catalog-030) — the FK rejects the dangling state at write time, making the detection function unreachable by construction. This row is the canonical "declarative FK beats a stored function" case the capability ladder above was written to produce; it took two more RDRs to actually reach it. |
| manifest backfill / document reconstruction | **stored function** `document_text(doc_id)` (`manifest_backfill()` RETIRED, RDR-191 Phase 6) | `document_text` replaces a generated-SQL-string artifact with a first-class DB object callable by doctor / migration validation; no triggers, no app round-trips. `manifest_backfill()`'s only reason to exist — pre-stamping `collection` before the (now-retired) orphan/verify functions' `collection IS NOT NULL` filter — died with those functions, and catalog-025's later `NOT NULL` promotion means no future row can need the stamp either. |
| per-collection stats | **`security_invoker` view** `collection_vector_stats` | Read-only aggregate; a view under the caller's RLS is exactly right — replaces remote `count()` calls. No function needed. |
| combined-query read shapes | **set-returning `LANGUAGE sql` functions** (`search_metadata_scoped` / `search_topic_scoped` / `search_graph_hop` / `search_aspect_scoped`) | Must take the query vector as a plan-time argument (a view can't), and stay inlinable so HNSW survives the join. Functions, not views, not triggers. |
| soft delete (tombstone) | **plain column + partial indexes + view filters** (`deleted_at`, `live_chunks`) | **Adds ZERO triggers.** Tombstoning is an `UPDATE`, so the `ON DELETE CASCADE` chains do not fire; restore clears one column. Cascade semantics are declarative. |
| collection registration-before-write | **a 422 refusal in `upsertChunks` + the FK** (see the section above) | The FK makes the ordering load-bearing; the registration is done in the same transaction. No trigger to maintain the invariant. |

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
- **Queue state-machine guards** — the tuple queues (`/v1/tuples`, RDR-205/RDR-235) own lease and attempt state engine-side; no trigger is needed.
- **Register-as-function** — catalog register is already one atomic `FOR UPDATE` transaction.
- **ETL upserts** — single-statement `ON CONFLICT`; no trigger needed. (A column a trigger/generated mechanism maintains must NOT be an ETL `ON CONFLICT` merge participant — see `doc_count`, dropped from the taxonomy ETL.)

**Matview deferral (RDR-154 Decision 3):** `top_topics`/ICF projection aggregates and
`telemetry_collection_stats` are deferred — plain `security_invoker` views suffice today.
Promote to a materialized view ONLY when a read-hot signal (measurable latency on that read
path) justifies the refresh machinery. A matview over a tenant table MUST carry `tenant_id`
and be fronted by a thin `security_invoker` wrapper view re-applying the tenant filter;
consumers query the wrapper, never the matview.

## Hot rules

- **No ORM, and NO new SQLite** (root `AGENTS.md` hot rule). New persistent state is a Liquibase changeset in `service/src/main/resources/db/changelog/`, never client-side DDL and never a `sqlite3.connect`; the SQLite substrate this module once carried is DELETED (RDR-158 P4, see above).
- **Never edit a shipped Liquibase changeset.** Schema evolution is a NEW changeset (and, for data migrations, an upgrade-ladder rung in `src/nexus/upgrade_ladder/`). Editing a shipped changeset breaks checksum validation on every installed engine past that version.
- **Pagination must respect `_PAGE = 300`, and writes `MAX_RECORDS_PER_WRITE`** (limits table: `.claude/rules/external-service-limits.md`).

## Other modules

Modules the key-modules table does not describe, one line each (the module docstring has the contract). Most of the client-side plumbing here exists to reach the engine reliably; none of it holds state the engine does not.

| Group | Files |
|---|---|
| Endpoint and credentials | `service_endpoint.py` (endpoint discovery and the production-write guard), `data_token.py` (`DataTokenManager`: self-minting short-TTL data tokens), `managed_endpoint.py` (cloud endpoint config and capability probe), `client_identity.py` (the client version the engine reads), `serving_engine_version.py` (the version of the engine this process talks to), `storage_mode.py` (backend env validation) |
| HTTP transport | `pooled_http.py` (persistent-connection transport), `edge_refusal.py` (edge/WAF refusal rendering), `gateway_backoff.py`, `gateway_hold.py` (a gateway 503 holds every client of one config dir), `request_gate.py` (per-process request interval and count) |
| Engine clients | `http_scratch_store.py` (T1), `http_pipeline_client.py` and `inmemory_pipeline.py` (streaming-PDF buffer and its test twin), `http_engine_status.py` (`GET /v1/status` embed counters), `t2_reads.py` (narrow T2 reads for callers outside the facade) |
| Collections and chunks | `collection_purge.py` (the canonical collection-delete cascade), `collection_state.py` (three-state existence probe), `chash_tables.py` (the chash-bearing tables), `reconcile.py` (model/dimension routing, chunk pagers) |
| Local Postgres | `pg_provision.py` (local cluster provisioner), `pg_bundle.py` (first-run bundle extraction), `pg_auth.py` (`pg_hba.conf`), `pg_cluster_upgrade.py` (`pg_upgrade` of older clusters), `admin_sql.py` (the local admin SQL runner), `diag_connection.py` (the only product path to a `nexus_diag` session), `svc_monitor.py` (`pg_monitor` queries), `note_uri_pairs.py` (the RDR-231 pair query) |
| Local models | `service_bge_model.py`, `service_crossencoder_model.py`, `onnx_model_root.py` (provisioning of the engine's ONNX models), `minilm_direct.py` (MiniLM embedding function for tests and `--dry-run`) |
