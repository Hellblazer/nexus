---
title: "RDR-164: Collection- and Document-Lifecycle Cascade Consolidation — Replace SQLite-Era Client-Side Cross-Store Orchestration with Postgres-Native Atomic Cascades"
id: RDR-164
type: Architecture
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-21
accepted_date: 2026-06-21
related_issues: [nexus-tquoj, nexus-cugrk, nexus-5kl1b]
related_tests: []
related: [RDR-152, RDR-154, RDR-155, RDR-156, RDR-144, RDR-138]
---

# RDR-164: Collection- and Document-Lifecycle Cascade Consolidation

## Problem Statement

Deleting or renaming a T3 collection touches state in many stores: the T3 chunks, taxonomy (topics / assignments / links / meta), centroids, `document_aspects`, `aspect_extraction_queue`, `document_highlights`, `chash_index`, the catalog (documents / chunks / projection), search telemetry, and the streaming `pipeline.db`. Today that fan-out is **client-side Python orchestration**: `purge_collection_cascade` (`src/nexus/db/collection_purge.py:47`) and `rename_collection_data_plane` (`src/nexus/collection_rename.py:22`) walk those stores one at a time, each in its own `try/except`, accumulating a `CascadeCounts.failures` list. That `failures` list exists *only because the multi-store operation is non-atomic*: any step can fail and leave orphans, and the next step runs anyway.

This shape is a **SQLite-era artifact**. When T2 was seven separate SQLite files, T3 was Chroma, the catalog was its own SQLite, and `pipeline.db` was a fourth database, there was no way to express "deleting a collection purges all its derived state" as one transaction — cross-database FK `ON DELETE CASCADE` and triggers do not exist across separate SQLite files. Client-side orchestration was the only option.

That constraint is now largely gone. RDR-152/155/156 moved `chunks_*`, `document_aspects`, `aspect_extraction_queue`, `document_highlights`, taxonomy, centroids, and the catalog tables into **one `nexus` Postgres schema**. Postgres can therefore express the cascade atomically — a single transactional `deleteCollection(tenant, name)` / `renameCollection(tenant, old, new)` method, RLS-scoped. (The intuitive "FK `ON DELETE CASCADE` rooted at `catalog_collections`" shape turns out NOT to be available as-is: research CA-3 found the existing registry FKs on `chunks_*`/`chash_index`/`topic_assignments` are `ON DELETE RESTRICT` and `NOT VALID`, so a registry-row delete is *blocked*, not cascaded — the corrected mechanism is an explicit ordered DELETE; see Decision item 1 and Q6.) Keeping the orchestration in the client is now a liability, not a necessity: it is the **root cause of a recurring orphan-row bug class**, of which two instances were found and patched client-side in the same session that motivated this RDR:

- **nexus-tquoj** — `nx collection delete` never purged `aspect_extraction_queue` (or `document_aspects`); the aspect worker then repeatedly claims a row whose collection no longer exists → extract-fail retry churn.
- **nexus-cugrk** — `nx taxonomy review` delete/merge left an orphan centroid in the pgvector centroid store that kept attracting chunks → ghost assignments to a deleted topic. Patched client-side; the local-mode half (nexus-5kl1b) could not even be fixed client-side because `CatalogTaxonomy` holds no centroid handle.

Both are symptoms of the same disease: lifecycle integrity maintained by hand, in the client, across stores, non-atomically. This RDR makes a deliberate decision about moving that integrity into the database — the natural next member of the RDR-154/156 "lean on Postgres" family.

### Three root gaps

#### Gap 1: Non-atomic multi-store lifecycle ops leave orphan rows

`purge_collection_cascade` (`collection_purge.py:47`) walks five stores in five independent `try/except` blocks, accumulating a `CascadeCounts.failures` list (`collection_purge.py:44,75,86,115`). There is no transaction around the fan-out: if step 3 fails, steps 1–2 have already committed and step 4 still runs. The `failures` list is not a feature — it is the visible scar of a non-atomic operation, and the orphan rows it admits are exactly the recurring bug class (nexus-tquoj: orphaned `aspect_extraction_queue` rows the worker then churns on; nexus-cugrk: orphaned centroids that keep attracting chunks). Every store added to a collection's derived state re-opens this gap.

#### Gap 2: The same cross-store fan-out is hand-duplicated across delete, rename, and "caller-responsible" contracts

The collection→stores fan-out is re-implemented three times over: the delete cascade (Gap 1); a rename cascade spread across **8 per-store `rename_collection` methods** (`chash_index`, `document_aspects`, `aspect_extraction_queue`, `telemetry`, `catalog_taxonomy`, `http_taxonomy_store`, `http_document_highlights_store`, catalog) orchestrated by `rename_collection_data_plane` (`collection_rename.py:22`); and **5 "caller is responsible" contracts** plus **4 post-relational-write side-effect cleanups** (the cugrk class: `delete_topic`/`merge_topics`/`rebuild`/`discover` delete a coupled centroid *after* the relational write). Each is a separate hand-maintained copy of the same integrity rule, and each is a place the rule can be forgotten for a newly-added store.

#### Gap 3: No DB-level integrity — `collection` is a denorm `TEXT` cache, and document-level deletes don't cascade either

The dependency that should be enforced by the schema is instead carried as **10 denormalized `collection` TEXT columns** with no (or `NOT VALID`) FK to the `catalog_collections` registry, so the database has no authoritative model of "this row belongs to that collection" and cannot enforce the cascade. The same gap exists one level down: `document_aspects.py:699` explicitly records that when a *document* is deleted, its aspect rows are NOT cascaded ("the catalog and T2 live in separate SQLite files; cross-DB FK CASCADE is not available") — a SQLite-era statement that is no longer true once both live in one Postgres.

### Evidence (the audit)

A full sweep of `src/nexus/` (2026-06-21) enumerated every client-side cross-store cascade/coupling. The pattern is broader than the two known bugs:

- **2 cascade orchestrators.** `purge_collection_cascade` (5 stores: T3 + taxonomy/chash + pipeline + catalog, best-effort, non-atomic, `failures` list at `collection_purge.py:44,75,86,115`). `rename_collection_data_plane` (`collection_rename.py:22`) sequences T2-cascade → T3-rename → catalog-cascade with mixed fail-close / fail-open semantics.
- **8 per-store `rename_collection` methods** all re-homing the same logical collection's denormalized `collection` column: `chash_index`, `document_aspects`, `aspect_extraction_queue`, `telemetry` (`search_telemetry` + `hook_failures`), `catalog_taxonomy` (topics/assignments/meta), `http_taxonomy_store`, `http_document_highlights_store`, catalog document re-home.
- **4 post-relational-write side-effect cleanups** — the cugrk class: `delete_topic`/`merge_topics` delete the centroid *after* the relational delete (`catalog_taxonomy.py:1060,1093`; `http_taxonomy_store.py` post-fix); `rebuild_taxonomy` and `discover_topics` delete-then-upsert centroids as separate calls around the T2 write.
- **5 "caller is responsible" contracts** — docstrings handing centroid/aspect cleanup to the caller (`http_taxonomy_store.py` delete_topic/merge_topics/purge_collection; `catalog_taxonomy.purge_assignments_for_doc`; `document_aspects` "aspect rows are NOT cascaded today … cross-DB FK CASCADE is not available").
- **A document-level cascade gap** (broader than collection-level): `document_aspects.py:699` explicitly records that when a *document* is deleted, its aspect rows are NOT cascaded; `T2Database.delete()` separately calls `memory.delete()` then `taxonomy.purge_assignments_for_doc()` (`memory.py:221`).
- **10 denormalized `collection` TEXT columns** that block a pure FK-cascade today: `chash_index.physical_collection`, `topic_assignments.source_collection`, `topics.collection`, `taxonomy_meta.collection`, `documents.physical_collection`, `relevance_log.collection`, `search_telemetry.collection`, `hook_failures.collection`, `aspect_extraction_queue.collection`, `document_aspects.collection`. Only `chunks_*` and `documents` are on the FK-eligible path so far (RDR-156 / RDR-108 P4 backfill).

### Out-of-Postgres residue (the honest caveat)

Not everything is in PG. `pipeline.db` (streaming buffer) is still local SQLite, and in **local mode** (`NX_STORAGE_BACKEND=sqlite`) the whole stack is SQLite + Chroma again — there is no shared-database cascade to lean on. So this RDR cannot be "delete the client orchestration"; it must be "shrink it to the genuinely-cross-substrate residue and make the in-Postgres core atomic," with the local path either keeping a (smaller) client cascade or being explicitly scoped out.

## Research Findings

Six Critical Assumptions investigated against the Java service schema (Liquibase changelogs) and the Python stores (2026-06-21). All cited.

- **CA-1 — Co-location: VERIFIED.** Every lifecycle table (`document_aspects`, `aspect_extraction_queue`, `document_highlights`, taxonomy `topics`/`topic_assignments`/`topic_links`/`taxonomy_meta`, `taxonomy_centroids_{384,768,1024}`, `chash_index`, `catalog_*`, `chunks_{384,768,1024}`, telemetry) is in the one `nexus` schema, applied by one master changelog over one HikariCP pool (`Main.java:62-70`). A single transaction/FK can span them. `pipeline.db` is the only out-of-PG lifecycle store; T1 is a separate `t1` schema (correctly out of scope).
- **CA-2 — FK spine feasibility: VERIFIED with caveats.** `catalog_collections` exists, PK `(tenant_id, name)` (`catalog-001-baseline.xml:catalog-001-5`). Five FKs already wired but `NOT VALID` and `ON DELETE RESTRICT` (`fk-002-collection-registry.xml`): `chunks_{384,768,1024}`, `chash_index`, `topic_assignments`. 8 of 10 denorm `collection` columns are FK-eligible; **3 must NOT cascade** — `relevance_log.collection`, `search_telemetry.collection`, `hook_failures.collection` are audit/event logs that must outlive their collection (resolves Open Q2 for these three). Risk: the `NOT VALID` constraints mean bulk rows are unvalidated; P1 backfill must pre-register every referenced collection name (mirror `fk-002-0-backfill-stubs`) before adding/validating FKs.
- **CA-3 — RLS-safe atomic cross-table purge: VERIFIED, with a load-bearing correction.** Tenant isolation is `TenantScope.withTenant` → `SET LOCAL nexus.tenant` GUC inside a txn (`TenantScope.java:93-147`); `nexus_svc` is `NOBYPASSRLS`; every lifecycle table is `FORCE ROW LEVEL SECURITY`. Multi-table mutation in one tenant-scoped txn is already proven — `CatalogRepository.renameCollection` (`CatalogRepository.java:1175-1197`). **Correction to the Decision:** the `catalog_collections` child FKs are `ON DELETE RESTRICT`, so deleting a registry row is *blocked* while children exist — a pure "FK ON DELETE CASCADE rooted at catalog_collections" is NOT available today. The document-rooted FKs *are* `ON DELETE CASCADE` (`catalog_documents` → `catalog_document_chunks`, `document_aspects`, `document_highlights`, `aspect_extraction_queue` — `fk-001`). Recommended shape: a Java `deleteCollection(tenant, name)` doing explicit ordered DELETEs (chunks → chash → topic_assignments/topics/centroids → catalog rows) in one `withTenant` transaction. If a PG `FUNCTION` is used instead, **`SECURITY INVOKER` is mandatory** (zero `SECURITY DEFINER` in the schema; it would bypass FORCE RLS — RDR-154 Gap 3). Composite `(tenant_id, col)` FKs keep any cascade tenant-safe (`fk-001-catalog-cross-store.xml:9-18`).
- **CA-6 — Centroid co-location: VERIFIED.** `taxonomy_centroids_*` are in `nexus`, same pool (`taxonomy-002-centroids.xml`), and have **no FK to `topics(id)`** — the function must purge them with an explicit `DELETE WHERE collection=?` (not a topic-cascade), achievable in the same transaction. Fixes the cugrk class atomically.
- **CA-4 — `pipeline.db` residue: VERIFIED.** Standalone local SQLite (`pipeline_buffer.py:25-36,102` "owns its own substrate … not in scope for the T2 daemon"), no service-mode branch in the cascade (`collection_purge.py:79-88`), no migration RDR. In the *current* (pre-P2) cascade, **T3 physical delete + `pipeline.db` are 2 client steps** outside any PG transaction. Refined by Q3: once P2 lands, service-mode T3 chunks fold into the `deleteCollection` transaction (they are pgvector in the same `nexus` schema), so the *post-P2 service-mode* residue is `pipeline.db` alone; the "2 steps" framing is the local-mode (Chroma) / pre-P2 baseline. Either way the service cascade cannot be a single `BEGIN…COMMIT` while `pipeline.db` is local SQLite, so RDR-164 does not promise a fully-atomic single-round-trip unless a separate RDR migrates `pipeline.db` into PG (Open Q3).
- **CA-5 — Local-mode liveness: VERIFIED (resolves Open Q1).** `NX_STORAGE_BACKEND=sqlite` is a live, supported opt-out with no hard-fail guard (`storage_mode.py:122-180`); the entire unit suite runs on it (`_pin_storage_backend_sqlite`). RDR-158 P3 (making `=sqlite` an error) is `accepted` but gated on `nexus-luxe6`, which has four unmet sub-conditions and **no projected timeline**. **Therefore scope-out is not viable** — RDR-164 MUST keep a parallel (smaller, explicit) client cascade for local mode, and that maintenance obligation persists until RDR-158 P3 closes. This is the divergence named in §Consequences; it is a cost, not a free choice.

**Net effect on the design:** the in-PG core *can* be made atomic (CA-1/3/6), but via an **ordered-DELETE transactional method** (not a bare FK-cascade from the registry — CA-3) plus a registry backfill (CA-2); and the client cascade **cannot be deleted**, only shrunk — in service mode to a single explicit `pipeline.db` step (T3 chunks fold into the transaction), in local mode to the Chroma delete + `pipeline.db` + the whole sqlite path (CA-4/5).

## Decision

*(Finalized at the gate, 2026-06-21, after research CA-1..6 and the Q1–Q6 resolutions.)*

Move collection- and document-lifecycle integrity into Postgres for the service path, and reduce the client cascade to a thin coordinator over the genuinely-separate substrates (T3 physical delete where not co-located, `pipeline.db`, and the local-mode SQLite/Chroma path).

Concretely, the proposed direction is:

1. **A transactional `deleteCollection(tenant, name)` / `renameCollection(tenant, old, new)` service method** (Java `CatalogRepository`, one `TenantScope.withTenant` transaction — the proven `renameCollection` pattern, CA-3) doing **explicit ordered DELETEs** across the in-PG lifecycle tables: `chunks_*` → `chash_index` → `topic_assignments`/`topics`/`taxonomy_centroids_*` → `document_aspects`/`document_highlights`/`aspect_extraction_queue` → `catalog_documents`/projection → the `catalog_collections` registry row last. RLS scopes every DELETE to the tenant automatically; no `failures` list. (Research corrected the original "FK ON DELETE CASCADE rooted at the registry" idea: those registry FKs are `ON DELETE RESTRICT`, so a registry-row delete is *blocked* until children are gone — CA-3. If a PG `FUNCTION` is preferred over a Java method, it must be `SECURITY INVOKER`.)
2. **Lean on the FKs that already cascade, and validate the rest.** The `catalog_documents`-rooted FKs are already `ON DELETE CASCADE` (aspects/highlights/queue/chunks-manifest) — document-level deletes get cascade for free (P4). For the collection registry, run the P1 backfill (register every referenced collection, mirror `fk-002-0-backfill-stubs`) then `VALIDATE CONSTRAINT` the five existing `NOT VALID` FKs; decide per-table whether to flip RESTRICT→CASCADE or keep the explicit ordered DELETE. **3 telemetry/audit columns (`relevance_log`, `search_telemetry`, `hook_failures`) get NO cascade FK** — they must outlive their collection (CA-2; resolves Open Q2 for these).
3. **Retire the "caller is responsible" centroid/aspect contracts** — the store/function owns its coupled state (cugrk/tquoj generalized).
4. **Keep a thin client coordinator** for the irreducible residue, explicitly documented (not silent best-effort): in **service mode** that is just `pipeline.db` (one explicit step — T3 chunks fold into the in-PG transaction); in **local mode** it is the Chroma physical delete + `pipeline.db` + the full sqlite cascade (kept because local mode is live with no retirement timeline — CA-5).

The alternative of leaving everything client-side is rejected: it is the proven source of the orphan-row bug class and grows a new orphan with every store added.

## Approach (phased)

*(Phase intent is settled below; the per-phase bead decomposition is finalized post-accept. FK strategy is NOT an open phase decision — Q6 settled it: RESTRICT + explicit ordered DELETE at collection level, CASCADE only for the existing document-level FKs.)*

**Phase 0: Audit lock**
- Ratify the §Evidence inventory (2 cascade orchestrators, 8 per-store renames, 4 post-write centroid cleanups, 5 caller-responsible contracts, 10 denorm `collection` TEXT columns). No FK-strategy or local-mode decision remains open — Q6 (RESTRICT + ordered DELETE for collection level) and CA-5 (local mode keeps a parallel client cascade) are settled.
- Confirm the per-table cascade/no-cascade list (taxonomy/aspect/centroid/chash cascade; `relevance_log`/`search_telemetry`/`hook_failures` do NOT) against the live Liquibase schema, verifying each FK-target table carries a `collection` column.
- Pick the retention/TTL policy for the three no-cascade audit tables.

**Phase 1: Collection-registry backfill + FK validation**
- Register every referenced collection in `catalog_collections` (mirror `fk-002-0-backfill-stubs`); reconcile orphans per the Q5 policy (stub-register live / DELETE-with-count genuinely-orphaned / FAIL-LOUD on ambiguous). Backfill MUST precede validation.
- Add `ON DELETE RESTRICT` `NOT VALID` FKs to the remaining FK-eligible collection-level tables (`document_aspects`, `aspect_extraction_queue`, `topics`, `taxonomy_meta`, `document_highlights`); this half ships on the critical path now.
- `VALIDATE CONSTRAINT` the five existing `NOT VALID` RESTRICT FKs (and the new ones). This half is world-blocked on the RDR-153 data migration / RDR-156 P0.3 and trails as a separate sub-bead — NOT on the P2→P5 path. Migration + rehearsal (RDR-153 discipline).

**Phase 2: Server-side `deleteCollection(tenant, name)` method**
- Extend Java `CatalogRepository.deleteCollection` (registry-only today) into the explicit ordered DELETE (chunks → chash → topic_assignments/topics/centroids → aspects/highlights/queue → catalog rows → registry row last) in one `TenantScope.withTenant` transaction. Re-point `purge_collection_cascade`'s in-PG steps at it; keep the `pipeline.db` (and local-mode) steps client-side.
- Close **nexus-tquoj** (aspect_extraction_queue not purged) — the explicit collection-DELETE catches the doc-less queue rows fk-001 cannot reach. Fold the service-mode cugrk centroid fix atomically (explicit centroid DELETE WHERE collection). The beads close here, not in P5.

**Phase 3: Server-side `renameCollection` method**
- Consolidate the 8 per-store renames into one transactional re-home, preserving the RDR-162 cross-model `targetExists` branch already at `renameCollection`; simplify `rename_collection_data_plane`.
- Re-home (not cascade) the three audit tables (`relevance_log`, `search_telemetry`, `hook_failures`) within the same transaction.

**Phase 4: Verify + retire the document-level path (cascade already exists)**
- Verify the existing `fk-001` `ON DELETE CASCADE` (catalog_documents → catalog_document_chunks/document_aspects/document_highlights/aspect_extraction_queue) fires correctly in the service path — NO new schema (CA-3).
- Retire the stale SQLite-era "caller responsible / not cascaded" comment at `document_aspects.py:699` and the now-redundant service-mode client-side caller cleanup; confirm the assignment orphan-on-document-delete gap is closed (keep `purge_assignments_for_doc` only where it covers a local-mode gap fk-001 does not).

**Phase 5: Retire the now-redundant client orchestration + caller-responsible contracts**
- Delete the dead `failures` accumulation and the per-store "caller removes the centroid" contracts that P2/P3 made atomic (service path only; the parallel local-mode client cascade is preserved per CA-5).
- Land **nexus-5kl1b** (cugrk local-mode leg) — or close it obsolete with a written disposition citing RDR-158-P3 / RDR-155-P4b status by bead+status. (nexus-tquoj closes in P2, not here.) Epic closeout.

## Alternatives considered

- **Status quo (keep client-side, fix each orphan as found).** Rejected: it is the bug class. Every new store re-opens it (tquoj and cugrk in one session).
- **Pure FK `ON DELETE CASCADE`, no functions.** Insufficient alone: rename re-homing and centroid purge are not deletes; `pipeline.db` and local mode are out of the PG transaction; some tables should deliberately *not* cascade.
- **Triggers instead of a function.** Considered for the centroid coupling; a function is more legible for a multi-table lifecycle op and easier to test than scattered triggers. Triggers remain a candidate for narrow invariants (RDR-154 precedent).
- **Do nothing until RDR-155 P4b / full SQLite-T2 retirement (RDR-158).** Partially valid — local mode is shrinking — but the service path is the 6.0 default *now*, and the orphan bugs bite *now*.

## Consequences

- **Atomicity:** lifecycle ops become all-or-nothing; the `failures`-list / partial-orphan class disappears for the in-PG core.
- **Simpler client:** the cascade shrinks to a documented thin coordinator; "caller is responsible" contracts retire.
- **Cost:** a real schema migration (denorm `collection` → FK, backfill, collision resolution) with RDR-153 rehearsal; risk concentrated in the backfill.
- **Local-mode divergence:** the sqlite/Chroma path cannot share the PG cascade; it keeps a (smaller, explicit) client cascade or is scoped out — a divergence that must be named, not hidden.

## Open questions

1. ~~**Local mode:** parallel client cascade vs scope-out?~~ **RESOLVED by CA-5:** local sqlite mode is live and gated-open with no timeline (RDR-158 P3 → `nexus-luxe6`); the unit suite runs on it. A parallel (smaller, explicit) client cascade MUST be maintained — scope-out is not viable. The remaining sub-question is only *how small* the local cascade can get.
2. ~~**Which tables must NOT cascade?**~~ **PARTLY RESOLVED by CA-2:** `relevance_log`, `search_telemetry`, `hook_failures` get no cascade FK (audit retention). Remaining: confirm taxonomy/aspect tables all *should* cascade (expected yes) and pick a retention/TTL policy for the audit tables.
3. ~~**`pipeline.db`:** migrate into PG vs permanent client step?~~ **RESOLVED — keep it a client step, do NOT migrate.** It is a transient local PDF-extraction buffer (`pdf_pages`/`pdf_chunks`/`pdf_pipeline`), per-install working state with no multi-tenant or durability requirement; it does not belong in the shared service PG, and migrating a high-churn buffer adds write load for no correctness gain (its cleanup is defensive and self-limiting, not the orphan-bug class). Correction this surfaces: in **service mode** T3 chunks are pgvector *in the same `nexus` schema*, so the chunk DELETE folds into the `deleteCollection` transaction — the ONLY service-mode residue is `pipeline.db` (a single explicit client step), not "T3 + pipeline.db". (Local mode residue stays larger: Chroma delete + `pipeline.db` + the sqlite cascade.)
4. ~~**RLS in the function:** `SECURITY DEFINER` vs invoker?~~ **RESOLVED by CA-3:** Java method in `TenantScope.withTenant` (preferred), or a `SECURITY INVOKER` PG function. Never `SECURITY DEFINER` (FORCE-RLS bypass, RDR-154 Gap 3).
5. ~~**Backfill collision policy?**~~ **RESOLVED — register-then-reconcile, fail-loud on ambiguity** (reuses the RDR-153 migration-data-quality discipline: structured issue reporting, no silent corruption). For each distinct `(tenant_id, collection)` referenced by a lifecycle table but absent from `catalog_collections`: (a) if it maps to live data (chunks exist / catalog projection exists) → **stub-register** it (mirror `fk-002-0-backfill-stubs`); (b) if genuinely orphaned (no chunks, no projection, no live collection) → **DELETE the rows with a logged count** (these are the exact orphans the RDR exists to remove — no silent drop); (c) if **ambiguous** (a renamed/tombstoned name that could map to >1 registry entry) → **FAIL LOUD** with a structured migration issue, require operator resolution, never guess. The per-store collision-defense DELETE is the precedent for case (b).
6. ~~**RESTRICT→CASCADE vs explicit ordered DELETE?**~~ **RESOLVED — keep RESTRICT + explicit ordered DELETE for the collection-level op; keep CASCADE only for document-level.** Collection delete is a rare, irreversible admin op: `ON DELETE RESTRICT` on the registry child FKs is a *safety net* (a stray `catalog_collections` delete errors instead of silently destroying derived state), the explicit ordered DELETE preserves per-table counts (the `CascadeCounts`/CLI-render + telemetry contract) and is testable with exact-count assertions. The `catalog_documents`-rooted FKs stay `ON DELETE CASCADE` — a document delete is a routine fine-grained op where cascade is correct and already working. Policy: **CASCADE where the parent delete is routine (document); RESTRICT + explicit method where it is destructive-admin (collection).**
