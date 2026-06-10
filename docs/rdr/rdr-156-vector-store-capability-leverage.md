---
title: "RDR-156: Vector-Store Capability Leverage — Unify the Retrieval Substrate: Combined Queries, Schema-Enforced Integrity, and Specialized Functions over the RDR-155 pgvector Chunk Tables"
id: RDR-156
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-10
accepted_date: 2026-06-10
related_issues: [nexus-rn3wo, nexus-dfwh0, nexus-4ft44]
related_tests: []
related: [RDR-152, RDR-153, RDR-154, RDR-155, RDR-106, RDR-108]
---

# RDR-156: Vector-Store Capability Leverage

## Problem Statement

RDR-155 landed the permanent vector store (`nexus.chunks_384` / `chunks_768` / `chunks_1024`) inside the same Postgres that already holds the RDR-152 catalog and T2 schema. For the first time, the vectors and the metadata that governs them (collection registry, document manifests, topic assignments, chash index) are co-resident in one transactional database. None of the referential relationships between them is schema-enforced: every integrity guarantee is application code, generated SQL strings, or prose in a locked RDR contract.

RDR-154 made the deliberate capability-selection pass over the RDR-152 schema, but its audit and gate (2026-06-08, 0C/0S) predate `vectors-001` entirely. The vector-store capability surface is unaudited new scope. This RDR is the same exercise, scoped to the vector tables, applying the capability-selection discipline RDR-154 P3 records: declarative first, views for read-shapes (always `security_invoker`), triggers only where an invariant is structurally app-unfixable.

### Four root classes (gaps)

#### Gap 1: No schema-enforced referential integrity between vectors and their governing metadata

`chunks_<dim>` has zero FKs in or out; `catalog_collections` (the RDR-103 name authority) is referenced by nothing; the RDR-155 verbatim-collection-name lock-step invariant is prose in a locked contract, not schema. Collection registration is best-effort application code (exception-swallowed), so a vector row can exist for a collection the catalog has never heard of, and nothing notices.

#### Gap 2: Retrieval shapes are assembled by multi-store stitching that co-residence has made unnecessary

The `query` MCP tool's catalog-aware routing is the canonical cost: catalog metadata lookup, collection resolution, per-collection vector query, then app-side re-join, interleave, and re-rank — duplicated logic, multiple round-trips, cross-store consistency windows, and retrieval cost that cannot be EXPLAINed. Topic-scoped, aspect-filtered, graph-hop, and frecency-boosted retrieval all pay the same stitching tax. The stores are now one database; the stitching is pure legacy.

#### Gap 3: Physical delete is still the only delete

RDR-106 (draft) documented the near-miss: `prune-stale` about to drop 11,766 valid catalog entries on a classification bug, averted by operator intuition, band-aided with backup files + an undelete verb. The structural gap stands: there is no in-database trashed state, a delete fires FK cascades through the manifest/aspects/highlights chains, and recovery is file restore or full re-index. The SQLite/Chroma split made tombstones awkward (RDR-107 was superseded rather than shipped); Postgres makes them nearly declarative.

#### Gap 4: Integrity checks live outside the schema and degrade silently

The RDR-155 cutover validation shipped as generated SQL strings (`manifest_orphan_sql`) executed by hand via psql — and the first production run executed them **vacuously against empty catalog tables**, with nothing but a mid-run human audit to notice. Checks that are not first-class DB objects cannot be called by doctor, CI, or migration validation, and their preconditions are not self-evident.

### The prize: combined queries (unify and simplify)

The deeper goal is not integrity hygiene. It is that co-residence makes **combined queries** possible: retrieval shapes that today require multi-store round-trips with application-side stitching collapse into single SQL statements the planner can optimize. The current `query` MCP tool's catalog-aware routing is the canonical example of the cost being paid today: catalog metadata lookup (SQLite), collection resolution, per-collection vector query (Chroma), then app-side re-join, interleave, and re-rank. Each of these becomes one statement:

- **Metadata-scoped semantic search**: `author` / `content_type` / `year` / subtree filters from `catalog_documents` joined directly into the HNSW scan, instead of pre-resolving collections and post-filtering.
- **Topic-scoped vector search**: `topic_assignments` ⋈ `chunks_<dim>` in one query, instead of T2 pre-filter then Chroma query.
- **Aspect-filtered retrieval**: `document_aspects` predicates (the RDR-089 SQL fast path) composed *with* semantic rank in the same statement, instead of choosing between the SQL path and the vector path.
- **Graph-hop + rank**: a `catalog_links` traversal CTE feeding an HNSW scan (the `follow_links` dance, server-side).
- **Hybrid fusion**: vector + tsvector + trigram RRF in one round-trip (the dearlord case — three index families on the *same table*).
- **Frecency/recency-boosted ranking**: `frecency` ⋈ similarity, one expression.

Every one of these deletes an application-side stitching layer (less code, fewer round-trips, no cross-store consistency window) and replaces it with a plannable, EXPLAIN-able, index-backed query. Reliability, correctness, and maintainability all move in the same direction: the database enforces what the app used to promise.

### Evidence (live-schema audit, 2026-06-10, during the first production migration run)

- `chunks_<dim>`: PK `(tenant_id, collection, chash)`, HNSW cosine index, GIN `tsvector`, GIN trigram, FORCE RLS — and **zero foreign keys in or out**.
- `catalog_collections` (the RDR-103 collection-name authority, with `embedding_model`) is **referenced by nothing**: not by `chunks_<dim>.collection`, not by `chash_index.physical_collection`, not by `topic_assignments.source_collection`.
- `catalog_document_chunks.chash` (the document manifest) is application-enforced only: the P2.1 fail-loud read backstop (`fetchDocumentChunks` ISE) plus the RDR-155 ETL's `manifest_orphan_sql(dim)` **generated SQL strings executed by hand via psql**.
- The RDR-155 locked contract "collection names verbatim; any future normalization must update `topic_assignments.source_collection` in lock-step" is **prose, not schema**. Postgres can mechanize exactly this with `ON UPDATE CASCADE`.
- The production migration's cutover validation ran against **empty catalog tables** (the RDR-153 SQLite→Postgres data migration has not run), making the manifest-orphan check vacuous — evidence that integrity checks living outside the schema degrade silently.

## Decision (draft)

1. **Schema-enforce the collection registry.** Add FKs, contingent on a unique key over `catalog_collections(tenant_id, name)`:
   - `chunks_384/768/1024 (tenant_id, collection)` → `catalog_collections (tenant_id, name)` — no vector row may exist for an unregistered collection. `ON DELETE` action is an open question (RESTRICT favored: dropping a registered collection should be an explicit, audited act, not a cascade surprise over up-to-100k chunk rows).
   - `chash_index (tenant_id, physical_collection)` → `catalog_collections (tenant_id, name)`.
   - `topic_assignments (tenant_id, source_collection)` → `catalog_collections (tenant_id, name)` **`ON UPDATE CASCADE`** — the verbatim-name lock-step invariant becomes authoritative-by-construction, retiring the prose contract.

2. **Do NOT FK the manifest to the chunk tables.** `catalog_document_chunks.chash` cannot directly reference chunks split across three dim tables. The trigger-maintained `chunks_registry` parent-table alternative is rejected today (see Alternatives): it adds a trigger outside RDR-154's "app-unfixable only" bar and imposes chunk-before-manifest write ordering on the hot indexing path. Instead, promote the orphan check to the database as a stored function (below) and keep the P2.1 fail-loud read backstop.

3. **Specialized functions** (replacing generated-SQL-string artifacts and hand-assembled reconstruction):
   - `nexus.manifest_orphans(dim int)` — the RDR-155 `manifest_orphan_sql` as a stored function; on-demand integrity check, callable by doctor/status surfaces and the RDR-153 migration validation.
   - `nexus.manifest_backfill()` — the idempotent `collection`-stamping backfill as a function.
   - `nexus.document_text(doc_id text)` — ordered manifest⋈chunks reconstruction (the `fetchDocumentChunks` read-shape, queryable server-side without the Java service).
   - `nexus.hybrid_search(...)` — server-side RRF fusion over HNSW + tsvector (+ trigram) in one round-trip. **Deferred/interlocked**: lands only with (or after) the conexus xr7.8.9 production-scale recall + hybrid-parity gate, so parity is benchmarked against the service-side fusion it would replace, not assumed.

4. **Views, under the RDR-154 `security_invoker` standing rule:** `collection_vector_stats` (per-collection chunk count, dim, last write — replaces remote count calls in doctor/status and the migration runbook's hand psql).

5. **Combined-query read shapes as the unification deliverable.** Define the canonical cross-store retrieval shapes (metadata-scoped search, topic-scoped search, aspect-filtered search, graph-hop + rank, frecency-boosted rank) as `security_invoker` views or set-returning functions, and progressively repoint the Python `query`/search composition layers at them, deleting the app-side stitching they replace. Each shape lands with an EXPLAIN-verified plan (the HNSW scan must survive the join — a filter that defeats the index is a regression, not a simplification) and a parity test against the stitched path it retires. **The parity fixture MUST include a narrow-collection scenario (collection size smaller than `hnsw.ef_search`, semantically distant query vector) with an exact-recall assertion (`== N`, not `>= threshold`)** — Research Finding 5b shows medium-selectivity fixtures pass while narrow-collection inputs silently under-return at the `max_scan_tuples` ceiling; the exact-count gate proves the selectivity-strategy switch actually fires (mirroring the RDR-155 P3.E recall@10 == 1.0 pattern). Stitching deletion happens in a **separate commit from the repoint**, so `git revert` is the rollback mechanism if a regression surfaces post-deletion.

6. **Soft delete, finally done right (absorbs RDR-106's catalog-projection scope onto the PG substrate).** RDR-106 (draft, 2026-05-08) was motivated by a near-miss mass deletion (`prune-stale` about to drop 11,766 valid entries, nexus-6ims); its SQLite-era answer was backup files + an undelete verb — physical delete remained the only delete. On Postgres this becomes almost declarative:
   - `deleted_at timestamptz NULL` on `catalog_documents` (and `catalog_links`). Tombstoning a document is an `UPDATE`, so the `ON DELETE CASCADE` chains to manifest/aspects/highlights **do not fire** — children stay intact and restore is clearing one column. Physical delete still exists, but only inside the explicit purge ceremony.
   - **Partial indexes** (`WHERE deleted_at IS NULL`) keep every hot path exactly as fast as today; live-row queries never pay for tombstones.
   - **Single enforcement point**: the RDR-156 view/function set (Decision 3-5) filters `deleted_at IS NULL` from day one — consumers cannot forget the filter because they never see the column. The destructive verbs (`nx catalog delete`, `gc`, `prune-stale`) repoint to tombstoning; `nexus.document_trash(doc)` / `document_restore(doc)` / `purge_trash(older_than interval)` are the ceremony, with purge doing the physical cascade plus a chunk-orphan sweep (a chunk row is shared by content-hash; it is removable only when no live manifest row references it — an anti-join that is itself a one-statement combined query now).
   - **Search-layer visibility, default committed**: chunk-level search excludes tombstoned documents via a `live_chunks` anti-join view — the same single-enforcement-point logic as the rest of the view set (consumers never see `deleted_at`, so they cannot forget it). P1's EXPLAIN run against live data volumes either confirms the join cost is acceptable or surfaces a measured reason to shift filtering to doc-result assembly (which would re-introduce a documented consumer obligation). The default is the view; deviation requires evidence.
   - **Scope boundary**: the event-sourced catalog model (events.jsonl, RDR-101 projector verbs) was retired by RDR-152's Postgres migration. `DocumentSoftDeleted`/`DocumentPurged` event types and events.jsonl back-compat are NOT in P1 scope — soft delete is a direct Postgres schema feature. RDR-106's SQLite-era backup/undelete mechanism remains shipped history.
   - **`purge_trash` invocation context**: the function is `SECURITY INVOKER` under FORCE RLS; called with no tenant GUC set (e.g. a maintenance cron), RLS filters nothing for a BYPASSRLS role and a purge could cross tenants. The function body MUST check `current_setting('nexus.tenant', true)` is non-empty and raise otherwise; cross-tenant maintenance purges are an explicit per-tenant loop, never an unscoped call.
   - Collections already carry `superseded_by`/`superseded_at` — collection-level soft delete exists; this aligns documents/links with the same lifecycle philosophy.
   - RDR-107's Chroma-metadata tombstones stay dead (superseded by RDR-108's content-hash identity); nothing from it is revived.
7. **Declarative hygiene now cheap because the tables are empty** (do before RDR-153 lands data — these are free on empty tables and expensive after):
   - Fix SQLite-heritage typing in `catalog_collections`: `created_at`/`superseded_at` are `text ''`-default — make them `timestamptz NULL` (coordinate with the RDR-153 ETL column mapping).
   - CHECK constraints: `length(chash) = 32` on `chunks_<dim>` and `catalog_document_chunks` (live data confirms 32 uniformly), `position >= 0` on the manifest.
   - Candidate, audit-gated: UNIQUE on `catalog_documents (tenant_id, source_uri)` where non-empty (the RDR-096 identity) — requires a ghost/duplicate audit against live SQLite data first; record as P0 audit item, not a blind add.
8. **Capability-selection discipline:** every choice above is recorded against RDR-154 P3's boundary (declarative FK > function > view > trigger-only-if-app-unfixable). The chunks_registry trigger is this RDR's recorded "NOT worth it" entry; soft delete adds zero triggers (tombstone is a plain column + partial indexes + view filters).

## Approach (phased, draft)

1. **P0 — FK set + empty-table hygiene.** The three FK groups as **two separate Liquibase changesets**: (a) `ADD CONSTRAINT ... NOT VALID` ships in RDR-156's own changeset sequence and deploys immediately (new writes validated, existing rows untouched); (b) `VALIDATE CONSTRAINT` ships as a distinct named follow-on changeset run only after the RDR-153 data migration completes with `total_failed == 0` — a single combined changeset would fail on any pre-RDR-153 deployment and block the migration entirely. Make collection registration mandatory-and-first on every new-collection write path (Finding 2). The Decision-7 hygiene in the same changeset window while tables are empty: `catalog_collections` temporal typing fix, chash/position CHECKs, and the `source_uri` uniqueness *audit* (constraint only if the audit is clean). Cross-tenant + ON UPDATE CASCADE rename tests.
2. **P1 — soft delete.** `deleted_at` columns + partial indexes + tombstone-aware purge/restore functions (`document_trash`, `document_restore`, `purge_trash` with the chunk-orphan anti-join sweep); repoint the destructive catalog verbs to tombstoning. Lands BEFORE the view set so every view ships tombstone-filtered from day one. Tests: tombstone leaves children intact (cascades don't fire), restore round-trip, purge removes only orphaned chunks, RLS isolation on trash/restore.
3. **P2 — manifest functions.** `manifest_orphans(dim)`, `manifest_backfill()`, `document_text(doc_id)` (all tombstone-aware); re-run the RDR-155 cutover validation non-vacuously through them once RDR-153 data lands; retire the generated-SQL-string artifacts from `vector_etl.py` (or its successor doc, given P4b deletes the module).
4. **P3 — `collection_vector_stats` view** under the security_invoker rule + repoint doctor/status consumers.
5. **P4 — combined-query shapes, one at a time, evidence-led.** Start with the two highest-traffic stitches: metadata-scoped search (the `query` MCP tool's catalog dance) and topic-scoped search. Each: view/function + EXPLAIN plan check (HNSW survives the join, query vector as argument per Finding 5) + parity test vs the stitched path + repoint + delete the stitching. Aspect-filtered and graph-hop shapes follow on the same template once the first two prove the pattern.
6. **P5 — `hybrid_search` function**, gated on conexus xr7.8.9 parity benchmarking. Explicitly a separate go/no-go: server-side fusion is only worth it if parity holds and the round-trip win is measurable. P5 also records the per-decision capability-selection rationale (Decision 8) in `src/nexus/db/AGENTS.md` (or the service schema AGENTS.md), matching RDR-154 P3's pattern — the discipline entry is a deliverable, not an aspiration.

## Alternatives considered

- **`chunks_registry` parent table (trigger-maintained) as FK anchor for the manifest.** Gives a real `manifest.chash` FK, but: a new trigger on the hottest write path (chunk upsert), write-ordering coupling (chunk before manifest row), and RDR-154's discipline says triggers only for app-unfixable invariants — the orphan class is adequately served by a stored function + fail-loud read. Recorded as NOT worth it today; revisit if orphan incidents recur post-RDR-153.
- **Single partitioned `chunks` table (dim as partition key) to enable direct FKs.** Rejected: `vector(n)` is a fixed-dimension column type; a single embedding column cannot vary dimension across partitions. Three tables is the correct pgvector shape.
- **FK manifest.chash → chash_index.** Wrong lifecycle: `chash_index` rows are per `physical_collection` with their own churn; a manifest row's chash legitimately outlives or precedes a given chash_index row.
- **Status quo (application-enforced everything).** Rejected by direct evidence: the first production cutover validation ran vacuously against empty tables and nothing noticed except a human audit mid-run.

## Consequences

- Referential integrity between vectors and their governing metadata becomes **authoritative-by-construction**; the verbatim-collection-name contract moves from prose to schema.
- The multi-store stitching layers (catalog-aware `query` routing, topic pre-filtering, aspect-vs-vector path selection) **shrink toward single plannable statements** — less Python/Java composition code, fewer round-trips, no cross-store consistency windows, and EXPLAIN replaces guesswork about retrieval cost.
- Integrity checks become **first-class DB objects** (functions), callable by doctor, migration validation, and future CI against live schemas — no more generated-SQL-string artifacts.
- Cost: FK overhead on chunk upsert (one index probe per row against `catalog_collections` — negligible vs the embedding call); `NOT VALID`/`VALIDATE` choreography couples this RDR's P0 to the RDR-153 migration sequencing; one more standing rule (FKs require registration-before-write ordering in the service).

## Research Findings

All verified 2026-06-10 against the live production cluster (PG16 + pgvector 0.8.2, ~98k chunks mid-migration) and the codebase at develop `67ae5b75`.

1. **FK target exists as-is** (VERIFIED, live `\d`): `catalog_collections_pk PRIMARY KEY (tenant_id, name)` — no schema change needed for any of the three FK groups. Side observation for RDR-153/154: `created_at`/`superseded_at`/`model_version` are `text` with `''` defaults (SQLite-heritage typing).
2. **Registration-before-write is NOT guaranteed today; registration is best-effort** (VERIFIED, code): the vectors layer (`PgVectorRepository`, `VectorHandler`) never references `catalog_collections`. Registration flows through `/collections/upsert` (`CatalogRepository`), called from Python — and the indexer call site (`indexer.py:598-611`, RDR-103 rename path) registers *after* data-plane writes inside `except Exception: log.warning` (silently skippable). P0 must therefore: make registration mandatory-and-first on every new-collection write path, backfill existing names, then `VALIDATE CONSTRAINT`. The FK deliberately converts today's silent registration failure into a hard upsert failure.
3. **`ON DELETE` is academic today; RESTRICT is free** (VERIFIED, code): the service has no collection hard-delete route at all — `http_catalog_client.delete_collection_projection` warns and returns `False` (guarded for bead `nexus-gmiaf.24`); only `supersede_collection` exists. Decision: RESTRICT, with `nexus.collection_drop(tenant, name)` as the future explicit ceremony — it would be the *first* real deletion path, not a replacement for one.
4. **Parity benchmarking home** (VERIFIED, T2 `nexus_rdr/155-P3-gate-result`): extend the existing Java `DualRunHarnessIntegrationTest` / `HybridParityIntegrationTest` (RDR-155 P3.E, commit 4c6be055, `-Dnx.dualrun.*` params; baseline recall 1.0 over 20 queries k=10, p95 < 250ms) for function-vs-service-fusion parity. The production-scale go/no-go (incl. `word_similarity` 0.6 calibration cross-check) remains conexus xr7.8.9 — both, with clear ownership split.
5. **Combined-query feasibility CONFIRMED live, with two hard design constraints** (VERIFIED, EXPLAIN ANALYZE on 98,205 chunks):
   - HNSW only engages when the query vector is a plan-time constant/parameter: literal vector → `Index Scan using idx_chunks_1024_embedding`, **2.0 ms**; the same query with a join-sourced vector → Seq Scan + top-N sort, **340 ms**. Constraint: every combined-query function takes the query vector as an *argument*; never produce it via a join in the same statement.
   - `SET hnsw.iterative_scan = relaxed_order` works (pgvector 0.8.2). But the filtered-search hazard is real: a collection-filtered scan with a semantically distant query vector returned **2 of LIMIT 10** rows after 20,207 filtered-out candidates (the `hnsw.max_scan_tuples` ceiling) in 53 ms. Constraint: scoped-search functions must pick strategy by selectivity — exact KNN (seq scan) for small collections, iterative HNSW with tuned `max_scan_tuples` otherwise — and Decision §5's EXPLAIN + parity bar is the enforcement point.

## Open questions

- ~~`ON DELETE` action~~ — RESOLVED (Finding 3): RESTRICT.
- ~~Registration-before-write ordering~~ — RESOLVED (Finding 2): not guaranteed today; P0 scope includes making it mandatory-and-first.
- ~~Where does `hybrid_search` parity benchmarking live~~ — RESOLVED (Finding 4): extend the P3.E Java harness; production go/no-go stays xr7.8.9.
- Should `manifest_orphans` run automatically post-RDR-153-migration (one-shot validation step) in addition to on-demand? (Leaning yes — fold into the RDR-153 migration's validation phase as a named step.)
- ~~Soft-delete visibility at the search layer~~ — RESOLVED in Decision 6: `live_chunks` anti-join view is the committed default; P1's EXPLAIN either confirms or produces measured evidence for shifting to doc-result assembly.
- RDR-106 disposition: on RDR-156 acceptance, mark RDR-106 `superseded-by: RDR-156` (its catalog-projection tombstone scope is absorbed here; its SQLite-era backup/undelete mechanism remains shipped history; its event-sourced projector-verb scope died with events.jsonl in RDR-152 — see Decision 6 scope boundary).
