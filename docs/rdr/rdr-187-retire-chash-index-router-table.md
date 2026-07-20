---
title: "Retire chash_index: the Chunks Tables Are the Chash-Keyed Store — Drop the Router Remnant of the Split-Store Architecture"
id: RDR-187
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-07-20
related_issues: [nexus-uu4ue, nexus-9zuks, nexus-84tr4, nexus-kmd5b, nexus-19svb]
related: [RDR-108, RDR-152, RDR-155, RDR-156, RDR-158, RDR-180, RDR-186]
---

# RDR-187: Retire chash_index — the Chunks Tables Are the Chash-Keyed Store

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

`nexus.chash_index` is the router half of the retired split-store
architecture. In the SQLite+Chroma world it was load-bearing: Chroma could
not answer "which collection holds this chunk id" without opening every
collection, so a SQLite table mapped `chash → physical_collection`. Its own
baseline changelog says so verbatim (`chash-001-baseline.xml`: "mirrors the
SQLite ChashIndex schema 1:1").

Post-RDR-155/180 that architecture is gone. The chunks tables ARE the
chash-keyed store: PK `(tenant_id, collection, chash)`, one row per
collection membership — exactly the multi-collection semantics the router
exists to provide. `chash_index` is now a denormalized copy of information
the store itself holds, maintained by dual-writes, with no FK tying it to
the truth.

#### Gap 1: The router is a derived copy with no FK, and it drifted 292,230 rows

A derived copy that can drift, does. Measured on the production store,
2026-07-20 (engine-service-v0.1.50, post-kmd5b census): **292,230 of the
copy's rows point at chunks that no longer exist** — the single largest
data-integrity debt in the system, 99.85% of the RDR-180 VALIDATE amnesty,
and the reason `chash_index`'s octet CHECK cannot be VALIDATEd. Nothing
ties the copy to the truth: no FK, no trigger, no reconciliation pass —
only a census leg whose job is measuring how far the copy has diverged.

#### Gap 2: Every per-chunk delete path leaks pointers, and none can be fixed once

The write-path analysis on `nexus-uu4ue` (comment, 2026-07-20) shows the
drift is structural, not incidental: pointer rows are written on every
chunk upsert and deleted on almost no chunk delete. Every per-chunk
deletion path in the system — PDF stale-prune, misclassified prune,
deleted-files GC → quarantine → expiry, upsert-supersede — removes chunk
rows and leaks the pointer. The only cleanup that exists is
collection-wide `delete_collection` and a lazy, collection-level
self-heal (`catalog_spans.py:367`) that cannot see a dead chunk inside a
live collection. Fixing this per-caller is the N-opt-ins failure class;
fixing it at the engine choke point (`nexus-9zuks`) still means the leak
class exists and must stay fixed forever.

#### Gap 3: Keeping the router costs a permanent maintenance apparatus for zero unique capability

The `nexus-9zuks` + `nexus-uu4ue` path — choke-point cleanup DML, a 292k
bulk delete under a predicted envelope, then VALIDATE — is the cost of
KEEPING the table: a census leg, a VALIDATE ceremony, a self-heal, a
dual-write hook, and cleanup DML in every delete transaction, all to keep
a copy synchronized with a source that can serve every one of its queries
directly (verified: research findings 1-3). The router provides no
capability the chunks tables lack; it provides only the obligation to
keep it consistent.

The design-level fix is to stop deriving the copy.

## Decision (draft)

Drop `nexus.chash_index`. Serve every consumer from the chunks tables,
which are the authoritative chash-keyed store. Keep the `/v1/chash/*` HTTP
surface shape so clients are unaffected; reimplement it engine-side over
`chunks_384/768/1024`. Add the missing chash-only index — `(tenant_id,
chash)` per chunk table — which the census and alias-resolution probes
would benefit from today regardless.

`chash_alias` is NOT in scope and stays permanent (RDR-180 decision:
legacy references resolvable forever). The alias map is identity
translation; the router was location lookup. Only the router dies.

The client-side SQLite twin (`db/t2/chash_index.py` over the local
`chash_index` table) is migration-source, frozen, and out of scope here —
it rides RDR-158 P3/P4 (the T2 SQLite backend retirement; RDR-186's DDL
census records it as `rides-RDR-158-P3/P4`, and RDR-158's RF-158-1 parity
tripwire names `chash_index` among the nine service-drop-in-ready store
pairs). NOT RDR-155 P4b, which is the Chroma/T3 deletion — a different
substrate. This RDR retires the PG table and the engine/client live
paths only.

## Verified Ground (2026-07-20, all file:line checked this session)

1. **Chunks PK**: `(tenant_id, collection, chash)` in
   `vectors-001-baseline.xml:80/110/140`. Multi-collection membership is
   native. A chash-only probe cannot use this PK (collection leads) — the
   new `(tenant_id, chash)` index per table is required, not optional.
2. **Three write paths, all fed by chunk-bearing flows** (corrected at
   gate — the first inventory missed the third): (a) the client
   dual-write hook on every CLI index; (b) the legacy `--cold`-style
   `_chash` ETL (`doImport`/`doImportBatch`); (c) the guided-upgrade
   staging pipeline — client lands SQLite router rows into
   `staging.chash_index` (`LANDING_MANIFEST`, `staging_land.py:70-76`;
   `StagingHandler.STORES:74-76`), and
   `StagingPromoteOps.promoteCollection:240-258` runs a live
   `INSERT INTO nexus.chash_index ... FROM staging.chash_index` with
   alias resolution on every collection promote. The guided path
   deliberately does NOT run (b) at all
   (`GUIDED_LAND_EXCLUDED_STORES`, `orchestrator.py:302`). Path (c) is
   also HOW the source store's already-orphaned router rows were
   imported: promote copies the SQLite router's rows, orphans included.
   The only nexus.chash_index rows without chunks are those orphans
   plus leak residue.
3. **The leak inventory** (nexus-uu4ue comment 2026-07-20): L1
   `PgVectorRepository.delete:2008` (chunk rows only; manifest punted to
   callers; chash_index not even mentioned); L2 PDF stale-prune
   `pipeline_stages.py:~1048`; L3 misclassified prune `indexer.py`; L4
   quarantine/expiry `chunk_quarantine.py` (zero chash_index references);
   L5 upsert-supersede. All leak today.
4. **Census predicate** (`ChashCensus.danglingPointers`, post-kmd5b):
   dangling = resolves by NO route, direct or via chash_alias. The
   chash_index leg dies with the table; the manifest leg and the three
   TEXT debt-column legs stay.
5. **Consumers of the router**: `/v1/chash/*` endpoints (ChashHandler:
   upsert, upsert_many, lookup, delete_collection, distinct_collections,
   rename_collection, delete_stale, is_empty, count_for_collection,
   import, registered_chashes); `catalog_spans.py` span resolution
   (lookup + created_at ordering + prefer-collection sort);
   `resolveLegacyRef` (nexus-84tr4 alias-chaining); the client post-store
   dual-write hook; ETL import legs; StagingPromoteOps.

## Design questions to pin before implementation

1. **created_at ordering for span resolution.** `catalog_spans` sorts
   lookup results by the router's `created_at` (newest first, preferred
   collection first). The chunks tables must supply an equivalent ordering
   source — verify `chunks_<dim>` carries a usable timestamp column (or
   metadata field) and that its semantics match "when this chash entered
   this collection". If the chunks side only has an upsert-refreshed
   timestamp, decide whether the ordering contract weakens acceptably
   (likely yes — the sort is a tiebreak, not a correctness gate).
2. **Endpoint semantics under the reroute.** `lookup` becomes a 3-table
   UNION probe; `distinct_collections`, `is_empty`,
   `count_for_collection`, `registered_chashes` become chunks queries;
   `upsert`/`upsert_many`/`import` become no-ops (or 410s) — decide
   whether the client stops calling (preferred: remove the dual-write
   hook in the same release) or the server absorbs no-op calls during the
   mixed-version window. `delete_stale` and the catalog_spans self-heal
   are deleted outright — with no derived copy there is nothing to heal.
3. **rename_collection.** Today it must touch both stores; after the drop
   it touches only chunks (and the manifest's collection column — verify
   who owns that today, RDR-164 cascade or caller).
4. **Mixed-version window.** An old client against a new engine fires
   FOUR removed write shapes (corrected at gate): `upsert`/`upsert_many`
   (dual-write hook, every index run), `delete_stale` (rare),
   `import` (legacy ETL, mid-migration only), and the staging
   `chash_index` LANDING leg (mid-guided-upgrade — the one that would
   break a migration in flight). All four accept-and-no-op with a
   deprecation field (the b878d precedent); the engine-side staging
   PROMOTE insert is not a window case — it is removed at the same tag
   as the DROP, atomically. One release later the client legs are gone
   and the endpoints can 410.
5. **ETL / migration legs — TWO mechanisms, corrected at gate.** The
   guided-upgrade path (the supported user migration) moves chash rows
   via the STAGING pipeline exclusively: land into `staging.chash_index`
   → `StagingPromoteOps.promoteCollection`'s INSERT (Verified Ground 2c);
   the legacy `doImport`/`doImportBatch` ETL is bypassed
   (`GUIDED_LAND_EXCLUDED_STORES`). Post-drop BOTH are removed: the
   promote INSERT block dies (Approach step 5), the legacy import leg is
   skipped, and the client landing leg no-ops through the deprecation
   window. Rehearsal fixtures assert DIRECTLY against the table
   (`rehearse_guided.sh:392` promote-count fields; `:588-591`
   `SELECT ... FROM nexus.chash_index` — these ERROR post-drop, not
   fail) and `LANDING_MANIFEST`'s census "fails on any chash-bearing
   column outside this set" — all updated in the same step.
6. **Census + doctor + forensics surfaces.** The `dangling.chash_index`
   leg, the diag view's chash_index legs (conexus-provisioned
   `nexus.diag_chash_conformance` — coordinate the view change with
   conexus, it is THEIR DDL now), doctor's chash-conformance counts, and
   `catalog-013`'s chash_index octet CHECK all die with the table.
   RDR-180's VALIDATE 3-of-5 becomes 3-of-4 with only
   `catalog_document_chunks` left NOT VALID.
7. **What nexus-uu4ue keeps.** The 426 dangling manifest rows are real
   documents with real position gaps — per-doc attribution and cleanup
   survive this RDR untouched (conexus Q5, relay [20992]). The 292,230
   pointer deletion collapses to the DROP TABLE. The step-3 VALIDATE
   shrinks to the manifest CHECK alone.

## Consequences

- `nexus-9zuks` (pointer-leak fix): mooted — close on this RDR's
  acceptance, the leak class cannot exist without the table.
- `nexus-uu4ue`: step 2's 292k envelope becomes the DROP; the 426-row
  manifest cleanup and the final VALIDATE remain.
- One less NOT VALID constraint, one less census leg, one less dual-write,
  one less self-heal path, ~11 fewer HTTP endpoint implementations after
  the deprecation window.
- Engine-side schema change through Liquibase (drop table + 3 new
  indexes), second release lifecycle: rides an engine-service tag, with
  the client hook removal in the paired conexus release.
- The diag view edit crosses the bus: conexus owns the deployed DDL
  (conexus-3ilh); the changelog is ours. Coordinate, don't surprise.

## Approach (numbered, for phase-gate cross-walk)

1. Add `(tenant_id, chash)` index per chunk table (Liquibase, VALID,
   cheap). Ship ahead of everything — improves census/alias probes now.
2. Reimplement `/v1/chash/*` read endpoints over chunks tables behind the
   existing HTTP shape; write endpoints become accept-and-no-op with a
   deprecation marker. Conformance-test against the old implementations
   on a populated store: reroute answers must be a SUPERSET of the
   router's and agree exactly on every chash the router returns.
   (Identity is deliberately NOT claimed: RDR-169 reference-only chunks
   land in `chunks_<dim>` via the engine bridge with no dual-write-hook
   `chash_index` row, so the reroute correctly resolves chashes the
   router never knew — a strict improvement, asserted as such.)
3. Remove the client dual-write hook + `delete_stale` self-heal
   (`catalog_spans.py`); reroute span-resolution lookup to the kept
   endpoint (unchanged shape = no client protocol change).
4. Census/doctor/forensics: delete the chash_index legs; update the
   fixture expectations; coordinate the diag-view change with conexus.
   Conexus-side lockstep items (inventoried, [21000], all theirs, all
   mechanical): conformance-view leg in bootstrap-engine-db.sh +
   provision_diag_path.py, rekey driver CHECK_TABLES/conformance SQL +
   grandfathered ceiling, restore_rowcount.sql leg, rdr164 cascade
   EXPLAIN probe retarget, two backend-switch doc copy-edits.
5. Retire the staging chash pipeline (gate Finding 1 — this precedes
   the DROP, or the next guided-upgrade promote crashes on a missing
   relation): remove the `chash_index` INSERT block from
   `StagingPromoteOps.promoteCollection` (:240-258) and its
   `chash_index_promoted` count; KEEP the `StagingHandler.STORES`
   `chash_index` entry and the `staging.chash_index` table for one
   release as an accepted dead sink (an old client mid-guided-upgrade
   must not 400 on its landing calls — same window discipline as the
   write endpoints); remove the client `LANDING_MANIFEST` entry and
   update its census in the client release that pairs with the engine
   tag; update `rehearse_guided.sh` (:392 promote-count field list,
   :588-591 direct `nexus.chash_index` SQL asserts — these ERROR
   post-drop, they do not soft-fail).
6. Drop `nexus.chash_index` + its octet CHECK via Liquibase (with the
   catalog-013 precondition discipline: sqlCheck-gated, MARK_RAN-safe).
   The 292,230 orphans die here — record the count in the changeset
   comment for the audit trail.
7. Legacy ETL: skip the chash_index `doImport`/`doImportBatch` leg
   (the `--cold`-style path only; the guided path never ran it).
8. One release later: 410 the write endpoints, delete the no-op shims,
   drop `staging.chash_index` + its `STORES` entry.

## Alternatives Considered

- **Fix the leak, keep the table** (nexus-9zuks as filed): engine-side
  pointer cleanup in the delete transaction + 292k bulk delete + VALIDATE.
  Rejected: permanent maintenance of a derived copy (DML, census leg,
  self-heal, dual-write) whose every query the source can serve. The
  choke-point fix is the right shape ONLY if the table earns its keep;
  it does not.
- **Materialized view over chunks**: recreates the drift problem with
  refresh semantics; nothing needs the router shape badly enough.
- **Do nothing until P4b**: P4b deletes the CLIENT SQLite twin with
  Chroma; the PG table is not on P4b's critical path and its debt
  (292k rows, blocked VALIDATE, open leak) is live now.

## Research

- [x] Verify chunks_<dim> timestamp column semantics for design question 1
      → finding 1, VERIFIED: design question 1 closes, no weakening.
- [x] Inventory every CHASH_INDEX reference in the engine (jOOQ AND raw
      SQL) — the compile surface for the drop → finding 2, VERIFIED.
- [x] Conexus round-trip: diag view legs referencing chash_index, and
      whether any of their tooling reads the table directly → finding 5,
      VERIFIED ([21000]): five direct readers, all conexus-owned, all
      mechanical, landed in lockstep with the view-changelog change.
- [ ] Perf sanity: lookup-by-chash via 3-table probe with the new indexes
      vs the router → finding 4, ASSUMED; measure during Approach step
      1/2 in the rehearsal container, gate step 2 on it.
- [x] Mixed-window matrix: old-client/new-engine and new-client/old-engine
      for every kept endpoint → finding 3, VERIFIED (one hazard
      direction; three no-op shapes).

## Research Findings

### Key Discoveries

- **✅ Verified** (source search) — `chunks_<dim>.created_at` is
  `TIMESTAMPTZ NOT NULL DEFAULT now()` and BOTH upsert `ON CONFLICT`
  set-lists exclude it (regular `:683-688`, reference-only `:745-749`) —
  first-insert-per-`(tenant_id, collection, chash)`, semantically
  identical to `chash_index.created_at`. Span-resolution ordering
  reroutes with the SAME contract. Design question 1 closes.
  *Source: vectors-001-baseline.xml:72-81; PgVectorRepository.java:683-688,745-749*
- **✅ Verified** (source search; CORRECTED at gate, Layer-3 Critical) —
  Full compile surface: jOOQ `CHASH_INDEX` in ChashRepository (44 refs —
  the class dies) + CatalogRepository (3: two cascade legs that simply
  drop); raw SQL in ChashCensus (1 leg), RekeyOps (6 — the drop runs at
  boot before any rung, so the rekey legs are removed, not skipped),
  CatalogRepository (1), and **StagingPromoteOps.promoteCollection
  :240-258 — a LIVE `INSERT INTO nexus.chash_index` on every
  guided-upgrade collection promote**, which the first inventory
  recorded as merely "1 finalize count" (the sampling-as-inventory
  failure, caught by the gate critic). Plus the staging surface:
  `StagingHandler.STORES` `chash_index` entry → `staging.chash_index`,
  fed by the client `LANDING_MANIFEST` leg, and `rehearse_guided.sh`
  hard SQL asserts (:392, :588-591). Client: 11 http_chash_index
  methods, the dual-write hook, catalog_spans (3 lookups + self-heal),
  collection_health, collection_audit, commands/doc,
  migration/orchestrator (2 legacy ETL legs, bypassed by the guided
  path per `GUIDED_LAND_EXCLUDED_STORES`), t2_daemon RPC registry. The
  SQLite twin rides RDR-158 P3/P4, untouched here.
  *Source: exhaustive grep, engine + client, corrected per gate Finding
  1; T2 187-research-2*
- **✅ Verified** (source search; CORRECTED at gate) — Mixed-version
  window: ONE hazard direction (old client + new engine), FOUR removed
  client-fired write shapes — `upsert`/`upsert_many` (dual-write hook,
  every index run), `delete_stale` (rare), `import` (legacy ETL,
  mid-migration), and the staging `chash_index` landing leg
  (mid-guided-upgrade). All four 200-and-no-op for one release (b878d
  precedent), then 410. The engine-side staging PROMOTE insert is not a
  window case: removed atomically with the DROP at the same tag. New
  client + old engine touches only kept reads the old engine serves
  fine. The dual-write hook is best-effort-swallow, so even the 410 era
  cannot break old-client indexing — only spam the RDR-129 drop
  counter, which is itself the argument for the no-op window.
  *Source: caller inventory (finding 2, corrected); mcp_infra.py hook
  contract; staging_land.py LANDING_MANIFEST*
- **❓ Assumed** (pending spike) — 3-table probe with the new
  `(tenant_id, chash)` indexes performs at parity or better than the
  router lookup. Measure via EXPLAIN ANALYZE on the populated rehearsal
  store during Approach step 1/2; the conformance test doubles as the
  correctness gate. Gates step 2, not the RDR.
  *Source: to be measured in the migration-rehearsal container*
- **✅ Verified** (conexus grepped inventory, [21000]) — Five direct
  `nexus.chash_index` readers on the conexus side, all theirs, all
  mechanical: the restore-rowcount leg, the rekey driver's conformance
  SQL + read_counts (whose GRANDFATHERED 292,230 debt ceiling
  evaporates with the table — "the cleanest possible resolution of that
  debt"), the diag-view DDL in two provisioning paths, and the RDR-164
  cascade EXPLAIN probe. Three of these are the SAME conformance object
  seen three ways (their driver SQL is a generator-pinned copy of the
  view definition) — a three-call-site lockstep edit, each dropping one
  UNION-ALL leg, landed with our view-changelog change. Their
  `cutover_smoke.py` goes via `/v1/chash/` and survives by design.
  Their probe finding also cross-confirms the cascade edit is in scope:
  `CatalogRepository.deleteCollection`/`rename` lose their chash_index
  leg (already in finding 2), and their EXPLAIN probe retargets in
  lockstep. Named conexus-side migration item recorded on their side.
  *Source: T2 conexus [21000]; cross-checked against finding 2*
