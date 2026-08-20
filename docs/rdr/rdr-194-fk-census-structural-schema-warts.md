---
title: "Post-RDR-187 FK Census and Structural Wart Retirement: One chash Encoding, One doc_id Meaning, Tenant-Keyed Uniqueness Everywhere, One TTL Semantics, and Every Enforceable Relationship Enforced"
id: RDR-194
type: Architecture
status: closed
closed_date: 2026-08-20
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-08-15
accepted_date: 2026-08-15
related_issues: [nexus-tk070, nexus-coeff, nexus-3n7pr, nexus-ysrwi]
related: [RDR-108, RDR-152, RDR-154, RDR-156, RDR-180, RDR-187, RDR-191]
---

# RDR-194: Post-RDR-187 FK Census and Structural Wart Retirement

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The schema type-hygiene arc (epic nexus-cefa1, 2026-08-15) retired the
RDR-152 SQLite-port *type* debt: every TEXT timestamp, INT flag, and
JSON-as-TEXT column that was typed by neighbourhood at port time is now
timestamptz / boolean / jsonb. What it deliberately left, and what this RDR
owns (Hal directive 2026-07-21, mid-RDR-187 .9; re-sequenced 2026-08-15 as
the arc after cefa1.7), is the *structural* debt that the split-store era
made unenforceable by construction and that co-residency in one PG now makes
fixable for the first time:

#### Gap 1: Three live encodings for one logical value, the chunk chash

**Three live encodings for one logical value, the chunk chash.** 32 raw
`bytea` (`chunks`, `catalog_document_chunks`, `chash_alias.new_chash`,
RDR-180); 32-or-64-char hex TEXT (`chash_remap.new_chash`, CHECK widened
at rdr180-001 instead of converted); 64-char hex TEXT
(`topic_assignments.doc_id`, writer taxonomy-006 `encode(chash,'hex')`).
`chash_alias.old_bytes` is the only bytea chash column with no width
check (deliberate: carries 16-byte legacy refs; undocumented at the
column). Vestigial references to the dropped `chash_index` remain in
fk-002-collection-registry.xml and fk-002-validate.xml comments.

> **CORRECTION (2026-08-20, P4 implementation, verified not assumed):**
> `chash_alias` — table and `old_bytes` column both — was DROPPED
> 2026-08-16 at `legacy-001-drop-chash-alias.xml` changeset `legacy-001-5`
> (bead `nexus-lgdel.l1`, Hal delete directive), one day after this RDR's
> P4 text was drafted. Every reference to `chash_alias.old_bytes` below is
> historical: correct at time of writing, superseded by the deletion. Full
> correction at § D7 (below); do not re-open the P4 acceptance criterion
> over this — see that note for the disposition.

#### Gap 2: doc_id means two things

**`doc_id` means two things.** Tumbler with an FK to `catalog_documents`
in `catalog_document_chunks`, `document_aspects`, `document_highlights`,
`aspect_extraction_queue`; a hex chunk chash with NO FK in
`topic_assignments.doc_id` (contradicting taxonomy-001's own comment,
which says "tumblers"); unconstrained in `hook_failures.doc_id`. The
ChashCensus "dangling legs" exist precisely because no FK does that job.
#### Gap 3: tenant_id inconsistently in the primary key

**tenant_id inconsistently in the primary key.** Composite-PK tables lead
with it; surrogate-PK tables demote it to a UNIQUE or nothing. Sharpest:
`migration_jobs` PK=(job_id) with NO tenant-scoped uniqueness at all,
RLS-only protection.
#### Gap 4: TTL is two names and two null-semantics

**TTL is two names and two null-semantics.** `ttl` vs `ttl_days`;
NULL=permanent in one place, 0 in another. The `memory_put ttl=0` trap
is this wart.
#### Gap 5: The FK census itself

**The FK census itself.** Which inter-table relationships SHOULD be
enforced now that every store is co-resident: fk-001/002/003/004 cover
the catalog cross-store and collection-registry edges (some NOT VALID);
the RDR-191 manifest->chunks FK is validated. Known unenforced:
`catalog_links.(from_tumbler,to_tumbler)` -> `catalog_documents`
(`deleteCollectionTxn` step 6 hard-deletes documents and removes no
links: 277 dangling rows on the live store 2026-07-25, nexus-ysrwi;
detection ships in `nx doctor --check-dangling-links`, prevention does
not); the three chash-debt TEXT columns; `topic_assignments.doc_id`;
`hook_failures.doc_id`; and whatever the information_schema-derived
enumeration finds that a hand list would miss.
#### Gap 6: Empty-string sentinels instead of NULL

**Empty-string sentinels instead of NULL** are pervasive in the
baselines; fk-001 had to DROP DEFAULT/NOT NULL on two doc_id columns just
to attach FKs (direct cost evidence). Not every sentinel is a wart (the
`catalog_links.created_at` one was retired at catalog-031); this RDR
decides the remainder per column, not en bloc.

Excluded and staying excluded (owned elsewhere or reasoned): `plans.dimensions`
/ `parent_dims` (byte-equality plan identity), `pdf_*` JSON + the three-state
`pdf_chunks.embedding` sentinel (pipeline-001 rationale), the staging schema
(deliberately typeless landing zone), time-column naming, and the ChashCensus
observability itself (it is the stopgap an FK retires, and the census legs are
retired one-for-one as FKs land, never ahead of them).

## Context

Inventory of record: T2 `nexus/research-schema-type-debt-inventory-2026-08-12`
sections 5-8 and "FK posture correction" (the type-arc research that first
enumerated these). Prior art in-tree: fk-001-catalog-cross-store.xml,
fk-002-collection-registry.xml (+ -validate), fk-003-collection-registry-extra.xml
(+ -validate), fk-004-chunks-collection-registry.xml, catalog-029-manifest-chunk-fk.xml
(the RDR-191 three-step NOT VALID -> anti-join remediation -> VALIDATE shape,
which is the template for every FK this RDR adds over a populated store),
rdr180-001-bytea-chash.xml (the chash encoding change that left the TEXT
stragglers), RDR-187 (retired the chash_index router; census sequencing note:
"decide whether to census before or after the .11 staging drop" -- the drop
has shipped, so the census runs against the current table set).

Method (from nexus-tk070): schema-wide join-column enumeration derived from
information_schema, census-style, not a hand list; classify each edge as
FK-able now / needs-design / deliberately-loose; then decide.

## Constraints and Verified Facts

Census method: `scripts/sql/fk_census.sql`, an information_schema/pg_catalog
-derived (not hand-listed) query over schemas `nexus`, `t1`, `staging`, run
against a freshly Liquibase-applied schema on the pytest engine substrate
(2026-08-15, `develop` ~c41f9e61e, jar built same day). Verified by
`tests/db/test_fk_census.py` (7 tests, all green) including four ground
truths pinned directly against `pg_constraint`/`information_schema`, not the
census script's own output, so the tests cannot pass by the script and the
test sharing the same bug.

**Headline counts (result set 1, join-column census):** 136 rows across
`nexus`/`t1`/`staging` (99 in-scope `nexus`+`t1`, 37 `staging` exempt).
Class breakdown (in-scope rows only): `fk_enforced` 39, `no_plausible_target`
59, `fk_able_now` 12 (**all 12 are the generic surrogate `id` bigint column
colliding with every other table's `id` PK under the name-equality
heuristic**, `n_candidate_targets` 13-14 in every case; there is no other
table's `id` a given `id` plausibly means, so **none of the 12 is a real
FK-able edge**; this is the heuristic's own honesty signal, not a hand
filter applied after the fact), `needs_design` 1 (`t1.scratch.id`, TEXT vs
the same bigint-`id` noise, so also spurious).

**A limitation of the name-equality heuristic that decides the census output, found by running
it:** it can only find a candidate target when the column name and the
target's PK/UNIQUE column name are IDENTICAL. Every column the Problem
Statement names as a *known* unenforced edge, `topic_assignments.doc_id`,
`hook_failures.doc_id`, `frecency.chunk_id`, `relevance_log.chunk_id`,
`pdf_chunks.chunk_id`, `chash_remap.old_id`/`new_chash`, targets a column
with a DIFFERENT name (`catalog_documents.tumbler`, `chunks.chash`), so the
census classifies every one of them `no_plausible_target` rather than
`needs_design`. **The census's mechanical class column under-classifies
exactly the hard cases; every finding below cross-checks the Problem
Statement's named columns against `pg_constraint` directly (as the ground-
truth tests do) rather than trusting `census_class` alone.** A future
revision of the script could special-case `doc_id -> tumbler` and
`chash/chunk_id -> chash`, but doing so folds the RDR's own decisions (Q1,
Q3) into the "mechanical" census, which is why this file leaves it manual.

### Finding: `fk_catalog_chunks_chunk` is `fk_enforced`+VALIDATED (ground truth)

`ALTER TABLE nexus.catalog_document_chunks ADD CONSTRAINT
fk_catalog_chunks_chunk FOREIGN KEY (tenant_id, collection, chash)
REFERENCES nexus.chunks (tenant_id, collection, chash) ... NOT VALID`
(`service/src/main/resources/db/changelog/catalog-029-manifest-chunk-fk.xml:159-164`),
validated by a bare `ALTER TABLE ... VALIDATE CONSTRAINT
fk_catalog_chunks_chunk` at line 236 of the same file (changeset
`catalog-029-2`). Live: `pg_constraint.convalidated = t`. This also
resolves the "FK posture correction" T2 note's open question, `catalog_document_chunks.collection -> catalog_collections` is now covered
transitively through this same composite FK (census row: `collection`
column, `existing_fk_name=fk_catalog_chunks_chunk`), not a separate
omission needing its own FK.

### Finding (Q1 facts): `topic_assignments.doc_id` is a chunk chash by
### deliberate, previously-reverted design, not the tumbler its own
### column comment claims

- Schema: `nexus.topic_assignments` (`taxonomy-001-baseline.xml:101-110`)
  has no FK on `doc_id`; PK is `(tenant_id, doc_id, topic_id)`. Column
  comment at `taxonomy-001-baseline.xml:99` (T2 research quote) says
  "tumblers".
- Writer: `taxonomy-006-assign-from-chashes.xml` inserts
  `(tenant_id, doc_id, topic_id, ...)` from HDBSCAN cluster output
  (lines 122, 152, 203, 233, 284, 314, five near-identical INSERT/ON
  CONFLICT blocks across dim variants).
- **Direct evidence an FK was tried and removed on purpose:**
  `service/src/main/java/dev/nexus/service/db/TaxonomyRepository.java:1215-1219`
  (`importAssignment`, the fidelity-ETL import path):
  > "doc_id is a CHUNK content-hash (the HDBSCAN taxonomy clusters chunk
  > embeddings), not a document tumbler. fk_ta_catalog_doc was dropped
  > (nexus-sa14p) because it referenced catalog_documents(tumbler), a
  > different identity space, and could never be satisfied for
  > chash-keyed rows."
- Readers (all treat `doc_id` as an opaque string key, none assume tumbler
  shape): `TaxonomyRepository.java` lines 703 (`getTopicDocs`), 714/723
  (`getAssignmentsForDocs`), 774 (map key `"doc_id"`), 784
  (`getDocIdsForTopic`), 796 (`purgeAssignmentsForDoc`); `TaxonomyHandler.java`
  and `StagingPromoteOps.java` pass the value through without parsing.
- **Q1 answer-facts:** the writer's *intent* (chunk chash) is unambiguous
  and has ALREADY been the losing side of one FK attempt
  (`fk_ta_catalog_doc`, dropped nexus-sa14p) that assumed the *comment's*
  claim (tumbler) instead. A post-RDR-191 FK to `chunks(chash)` is
  dim-agnostic now that `chunks` is unified (RDR-191); it requires
  converting `doc_id` from hex TEXT to `bytea` first (this table's `doc_id`
  is `text`, confirmed live, census result set 2, row
  `nexus|topic_assignments|doc_id|text`). The column *comment* is stale
  and should be corrected regardless of which FK direction is chosen.

### Finding (Q2 facts): `deleteCollectionTxn` hard-deletes `catalog_documents`
### with no `catalog_links` step; `topic_assignments` already gets one

`CatalogRepository.java:5957-6029` (`deleteCollectionTxn`), the only
production hard-delete of document rows:
- Step 6 (line 6024): `ctx.deleteFrom(CATALOG_DOCUMENTS)
  .where(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(name)).execute()`, the
  comment above it (lines 6017-6023) enumerates what "fk-001 cascades": the
  four FK children (`catalog_document_chunks`, `document_aspects`,
  `document_highlights`, `aspect_extraction_queue`), explicitly noting
  `topic_assignments` is handled separately because it "has no doc-rooted
  FK". **`catalog_links` is named nowhere in this method**, confirmed by
  `grep -n catalog_links CatalogRepository.java` returning zero hits inside
  `deleteCollectionTxn`'s body.
- Contrast: step 3 (line 5995) explicitly deletes
  `topic_assignments WHERE source_collection = name` BEFORE the document
  cascade, i.e., the codebase already has the pattern (explicit
  collection-scoped cleanup for a table with no doc-rooted FK) that
  `catalog_links` would need, applied to a sibling table, just not to
  `catalog_links`.
- Live schema: `nexus.catalog_links` (`catalog-001-baseline.xml:127-159`)
  has `from_tumbler TEXT NOT NULL`, `to_tumbler TEXT NOT NULL`, no FK on
  either (confirmed both by the census, `no_plausible_target`, expected
  per the name-mismatch limitation above, and directly by
  `pg_constraint`, per `test_ground_truth_catalog_links_tumbler_columns_have_no_fk`).
- Prior measurement (nexus-ysrwi, cited in Problem Statement and the bead):
  277 dangling `catalog_links` rows on the live cloud store as of
  2026-07-25 (engine v0.1.56). This RDR does not re-run that count, the
  substrate this research ran against is a fresh local schema with zero
  rows, so a re-count here would prove nothing about the live store's
  current drift. **CLOUD COUNT NEEDED** (re-verify before Decision, the
  count is 3 weeks stale and RDR-191's chunk unification landed since):
  ```sql
  SELECT count(*) FROM nexus.catalog_links l
  WHERE NOT EXISTS (SELECT 1 FROM nexus.catalog_documents d
                     WHERE d.tenant_id = l.tenant_id AND d.tumbler = l.from_tumbler)
     OR NOT EXISTS (SELECT 1 FROM nexus.catalog_documents d
                     WHERE d.tenant_id = l.tenant_id AND d.tumbler = l.to_tumbler);
  ```
- **Q2 answer-facts:** an FK with `ON DELETE CASCADE` on
  `catalog_links.(tenant_id, from_tumbler)` and `(tenant_id, to_tumbler)`
  each referencing `catalog_documents(tenant_id, tumbler)` is schema-
  expressible (both are `TEXT`, matching `catalog_documents.tumbler TEXT`)
  and would make future `deleteCollectionTxn` calls self-cleaning without a
  new step. The alternative (explicit 8th step, mirroring the
  `topic_assignments` pattern already in the method) requires two
  additional `DELETE ... WHERE from_tumbler IN (...) OR to_tumbler IN (...)`
  statements scoped by the same `physical_collection` predicate as step 6,
  needs a join back to the just-deleted `catalog_documents` rows (must run
  BEFORE step 6, not after, unlike the FK approach which needs no
  ordering) and does not protect any OTHER caller that deletes
  `catalog_documents` rows outside this method (a grep for other
  `deleteFrom(CATALOG_DOCUMENTS)` call sites is the concrete way to check
  that surface before deciding).

### Finding (Q3 facts): `chash_remap` is a live, actively-used rekey
### tracking table, not a retired leg

- Schema: `chash_remap-001-baseline` created it; `rdr180-001-bytea-chash.xml:231-232`
  widened `new_chash`'s CHECK to `length(new_chash) = ANY (ARRAY[32,64])`
  instead of converting the column to `bytea`, confirmed live (census
  result set 2: `nexus|chash_remap|new_chash|text` with that exact CHECK
  text). PK is `(tenant_id, source_collection, old_id)`
  (`old_id` is also TEXT, unconstrained, census: `no_plausible_target`).
- Live usage (NOT vestigial): `service/src/main/java/dev/nexus/service/db/RemapRepository.java`
  and `service/src/main/java/dev/nexus/service/http/RemapHandler.java`
  implement the remap membership function and HTTP surface;
  `NexusService.java` wires the handler; `ChashCensus.java` reads
  `chash_remap` as one of its dangling-leg inputs; `CatalogRepository.java`
  references it (join surface for chash-aware reads). Python:
  `src/nexus/health.py` and `src/nexus/engine_version.py` reference remap
  in doctor/health-check plumbing. **None of these are marked deprecated or
  scheduled for removal**, this is a currently-served capability, not a
  stub.
- `remap-002-membership-function.xml` exists as a dedicated changelog file
  for `chash_remap` (a membership/lookup function), further evidence of
  active design investment, not abandonment.
- **Q3 answer-facts:** the T2 research inventory's framing ("retire the
  table if RDR-180's rekey has converged fleet-wide") does not hold, the table is a standing membership index the remap HTTP surface serves
  live traffic through, not a one-time migration scratch table. Converting
  `new_chash` to `bytea` (dropping the 32-or-64 CHECK in favor of a fixed
  32-byte width, matching every other post-RDR-180 chash column) is
  possible in principle but requires auditing every 64-hex-char row
  (the CHECK's `ANY (ARRAY[32,64])` exists because SOME live rows are still
  64-char hex, not 32-byte-equivalent), **CLOUD COUNT NEEDED**:
  ```sql
  SELECT length(new_chash), count(*) FROM nexus.chash_remap GROUP BY 1;
  ```
  A 64 count of zero would mean the CHECK is now over-wide dead latitude
  and safe to narrow before converting; a nonzero count means the two
  encodings are still concurrently live and conversion needs a decode step,
  not just a type change.

### Finding (Q4 facts): tenant-in-PK census (result set 4)

Full table (44 rows; `has_tenant_id_column=true` filter already applied, every row below carries a `tenant_id` column):

**Composite PK includes `tenant_id`, no separate UNIQUE needed (18 tables):**
`catalog_collections`, `catalog_document_chunks`, `catalog_documents`,
`catalog_meta`, `catalog_owners`, `chash_alias`, `chash_remap`, `chunks`,
`frecency`, `ladder_completions`, `pdf_chunks`, `pdf_pages`, `pdf_pipeline`,
`retention_markers`, `search_telemetry`, `taxonomy_centroids`,
`taxonomy_meta`, `topic_assignments`, `topic_links` (+ 7 `staging` mirrors,
exempt).

**`tenant_id` in PK AND redundantly in a UNIQUE (2, `catalog_links`,
`catalog_owners`):** `catalog_links` PK is `(tenant_id, id)` (a surrogate
`id` composite with `tenant_id`) plus the pre-existing
`catalog_links_unique (tenant_id, from_tumbler, to_tumbler, link_type)`, belt-and-suspenders, not a gap.

**Surrogate PK (`id`/`token_hash`/`session_token_hash`), `tenant_id` NOT in
PK (16 tables):** `aspect_extraction_queue`, `aspect_promotion_log`,
`claude_assisted_remediation_consents`, `document_aspects`,
`document_highlights`, `gc_audit`, `hook_failures`, `memory`,
**`migration_jobs`**, `nx_answer_runs`, `plans`, `relevance_log`,
`service_tokens`, `session_tokens`, `tier_writes`, `topics`. Of these,
`tenant_in_some_unique=true` for 4 (`aspect_extraction_queue`,
`document_aspects`, `document_highlights`, `memory`, `plans`,
`session_tokens`, 6 actually, see raw census output), those have a
tenant-scoped UNIQUE elsewhere even though the PK itself is bare `id`.
**`migration_jobs`, `nx_answer_runs`, `gc_audit`, `hook_failures`,
`relevance_log`, `tier_writes`, `topics`, `aspect_promotion_log`,
`claude_assisted_remediation_consents` have NEITHER tenant-in-PK NOR any
tenant-scoped UNIQUE constraint**, RLS is the only tenant-isolation
mechanism for these 9 tables. `migration_jobs` additionally has a *partial*
UNIQUE INDEX (`idx_migration_jobs_active_dedup ON (tenant_id,
collections_key) WHERE state IN ('queued','running')`,
`migration-001-baseline.xml:91-93`) which IS tenant-scoped but does not
show up in `pg_constraint` (partial indexes aren't constraints), the
census's `tenant_in_some_unique` column is a `pg_constraint`-only check and
undercounts partial-index protection; this is a second documented
limitation of the mechanical census (the schema-parity story for
`migration_jobs` is better than the raw census row alone suggests, but
still RLS-only for any row NOT in `('queued','running')`, i.e. every
finished job).
- **RLS coverage:** every in-scope table except `service_tokens` and
  `session_tokens` has `rls_enabled=t, rls_forced=t, n_policies=1`.
  `service_tokens`/`session_tokens` have `rls_enabled=f` (0 policies), worth a separate look but outside this RDR's named scope (auth-token
  tables, not catalog/knowledge data).
- **Q4 answer-facts:** the census makes the "9 tables, RLS-only, no
  tenant-scoped uniqueness at all" set precise (list above) rather than
  the Problem Statement's "sharpest: migration_jobs" single example, the
  decision (composite PK vs tenant-scoped UNIQUE-plus-RLS per table) needs
  to be made once and applied to all 9, or a documented reason given per
  table for why RLS-only is sufficient there.

### Finding (Q5 facts): TTL census (result set 5), two names, two null-semantics, confirmed live

| table | column | type | live semantics (code-verified) |
|---|---|---|---|
| `nexus.frecency` | `ttl_days` | `integer NOT NULL DEFAULT 0` | `0` = **permanent**; `src/nexus/db/t3.py:750-752,1123-1124`: "`ttl_days` = 0 means permanent. Expiry is computed Python-side from `indexed_at + ttl_days`"; query-side pre-filter `{"ttl_days": {"$ne": 0}}` (`http_vector_client.py:2830`, comment at 2787-2801 explicitly documents the sentinel and a prior CRITICAL bug, nexus-o8dil.5, where `ttl_days` wrongly gated catalog registration). |
| `nexus.plans` | `ttl` | `integer NULLABLE` | not traced to a single doc comment in this pass, needs the `PlanRepository`/plan-library write path read explicitly before Decision (not yet done; flagged, not verified). |
| `nexus.memory` (T2) | `ttl` | `integer NULLABLE` (Java: `MemoryRepository.java` `Integer ttlDays` param, column `MEMORY.TTL`) | **`NULL` = permanent**; the MCP tool layer (`src/nexus/mcp/core.py:3967`, `ttl=ttl if ttl > 0 else None`) coerces caller `ttl=0` to `NULL` before the HTTP call, this is an MCP-LAYER shim, not a store-level guarantee. `memory_put`'s own tool docstring (visible in this agent's own tool schema) states literally: "PERMANENT AT THE STORE IS NULL... a caller that bypasses MCP and writes ttl=0 directly (the store API, or POST /v1/memory/put) gets effective_ttl = 0 and the row is deleted on the next expire() sweep." So `memory.ttl` has the OPPOSITE null-semantics from `frecency.ttl_days` at the store level (`0`=expire-now vs `0`=permanent) with the trap papered over by ONE caller (the MCP tool), not by the schema. |
| `staging.frecency` | `ttl_days` | same as `nexus.frecency` | exempt (staging). |

- **Q5 answer-facts:** unifying to one name AND one null-semantics means
  picking a direction for TWO independent axes. Name: `ttl_days` (2 tables)
  vs `ttl` (2 tables), no majority. Null-semantics: `frecency`'s `0`=
  permanent is store-level and has ecosystem-wide callers already coded
  around it (`http_vector_client.py`'s `$ne` filter, `t3.py`'s docstring), changing frecency's semantics touches every T3 TTL reader. `memory`'s
  `NULL`=permanent is ALSO store-level but is undermined by allowing
  `ttl=0` to reach the store at all (the MCP shim is the only thing
  preventing the trap, and it's bypassable exactly as the tool's own
  docstring warns). The lower-risk normalization is likely NULL=permanent
  everywhere (matches SQL convention, matches `memory`'s store-level
  contract already) with `frecency.ttl_days`'s existing rows migrated
  `0 -> NULL` and every `$ne`/`$gt` T3 reader updated to `IS NOT NULL`, but this is a decision, not a fact this section should make. `plans.ttl`
  needs its write path traced before it can be classified either way.

### Finding: `hook_failures.doc_id` unconstrained, confirmed live (Problem Statement item 2, unconstrained bucket)

Schema: `telemetry-001-baseline.xml`; live: `nexus|hook_failures|doc_id|text|NO|''::text` (census result set 1, note the `''` empty-string default, itself an instance of Problem Statement item 6). Writers:
`TelemetryRepository.java:1030,1064` (`.set(HOOK_FAILURES.DOC_ID, str(docId))`);
readers: line 470 (select list), 1246 (grouping), 1371
(`HOOK_FAILURES.DOC_ID.eq(ks(key, 0))`, used as an opaque dedup key, not
parsed as tumbler or chash). No code site inspected treats this column as
anything but an opaque string; no evidence found of what identity space
`doc_id` is drawn from here (unlike `topic_assignments`, no in-tree
comment states an intent), **this is itself a fact worth carrying into
Decision**: `hook_failures.doc_id` may need its own investigation
(what callers pass into it) before Q1's `topic_assignments` answer can be
assumed to generalize to it.

### Finding (Q6 facts): NOT VALID constraint census

Live `pg_constraint.convalidated = f` count in this fresh schema: **zero**, every FK in `fk-002`/`fk-003` has already been validated by its paired
`fk-00N-validate.xml` changeset (confirmed: `fk-002-validate.xml`,
`fk-003-validate.xml` both exist and run `VALIDATE CONSTRAINT` per-FK,
guarded per the project's "every bare VALIDATE CONSTRAINT is guarded"
tripwire per `fk-004-chunks-collection-registry.xml:34-35`). **This means
Q6, as posed ("which fk-002/003 NOT VALID constraints can now VALIDATE"),
has already been answered by prior work, there is nothing NOT VALID left
in the schema baseline this RDR would need to validate.** Any NOT VALID
constraint this RDR itself adds (P1-P3, following the `catalog-029`
three-step shape named in Phasing) is new work with its own validate step,
not a stale backlog item. Re-verify against the LIVE cloud schema before
Decision (a local fresh-Liquibase run proves the migration chain is
internally consistent, not that the cloud install has actually run every
changeset up to the current HEAD), **CLOUD COUNT NEEDED**:
```sql
SELECT n.nspname, c.relname, con.conname
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE con.contype = 'f' AND NOT con.convalidated
  AND n.nspname IN ('nexus','t1');
```

## Decision

### D0. Shape rules every phase obeys

These are not restated per decision below; every phase inherits them.

1. **All DDL through Liquibase run-once changesets appended at the tail**,
   immediately before the runAlways grants (`grants-nexus-svc.xml` /
   `grants-nexus-diag.xml`), never spliced back into a family's original
   block. Precedent: the `<include>` comments for catalog-031, telemetry-004,
   aspects-003, plans-002 and taxonomy-008 in `db.changelog-master.xml`.
2. **No applied changeset body is ever edited.** A superseding *fact* about an
   applied file goes into that file's HEADER as a stale-prose correction
   (precedent: `fk-002-validate.xml`'s own `STALE-PROSE CORRECTION
   (nexus-o8dil.49, RDR-191 Phase 5, 2026-08)` block, added post-application).
   A superseding *function body* goes into a NEW changeset via
   `CREATE OR REPLACE` layered on the old one (precedent: `catalog-029-3` on
   `vectors-005-15`, `catalog-029-manifest-chunk-fk.xml:116-133`).
3. **Every FK over populated data uses the catalog-029 three-step shape**:
   ADD CONSTRAINT ... NOT VALID -> anti-join remediation -> VALIDATE
   CONSTRAINT, all three shipping together in ONE file, ONE release, ONE
   migration walk (`catalog-029-manifest-chunk-fk.xml:19-45`). The
   remediation is an anti-join, never a count comparison (F17); it is RLS
   toggle-wrapped when either side carries FORCE ROW LEVEL SECURITY
   (`catalog-029-manifest-chunk-fk.xml:79-90`); it RAISE NOTICEs its affected
   row count. Each VALIDATE carries the per-constraint MARK_RAN precondition
   the project's blanket tripwire requires
   (`tests/test_changelog_validate_precondition_lint.py`;
   `catalog-029-manifest-chunk-fk.xml:219-221`).
4. **Every anti-join remediation's population is COUNTED before it runs.**
   The counts are gate preconditions, enumerated in "Gate preconditions"
   below, and each count is recorded in T2 before its phase ships. A phase
   whose count is unavailable does not ship.
5. **ANALYZE in the same pass as any rewrite** (`ALTER COLUMN ... TYPE`,
   generated-column re-add), enforced by `tests/test_changelog_analyze_lint.py`.
6. **Rollback restores the baseline.** A deliberately empty rollback carries
   its reason inline (`tests/test_changelog_rollback_lint.py`).
7. **jOOQ regenerates on every schema change; the regenerated types are the
   Java work list.** Compilation failure is the call-site census.
8. **The wire contract is preserved per column.** Python clients keep sending
   and reading exactly what they send and read today unless this Decision
   names the column and states the change. bytea columns stay hex on the wire
   and bytes in storage, per the house rule (`CLAUDE.md`, chunk identity) and
   the existing encode/decode contract in `remap_membership`.
9. **No silent fallback for a correctness problem.** Where a conversion or an
   FK cannot be satisfied by a row, the migration fails loud with a named
   remedy. It does not coerce, and it does not widen a constraint to admit the
   row.
10. **Census legs retire one-for-one.** A `ChashCensus` leg, or the equivalent
    `nx doctor` detection, is retired in the SAME commit as the VALIDATE of
    the FK that obsoletes it, never ahead of it and never as a batch of its own.
11. **No preventive scope beyond evidence.** A population nobody has observed
    is not chased. Where this Decision declines an FK, it names the
    occurrence-time remedy instead.

### D1 (Q1). `topic_assignments.doc_id`: convert to bytea, FK to `chunks`, and correct the prose that says otherwise

**Decision.** Convert `nexus.topic_assignments.doc_id` from hex TEXT to
`bytea` and attach a composite FK

```
FOREIGN KEY (tenant_id, source_collection, doc_id)
REFERENCES nexus.chunks (tenant_id, collection, chash)
ON UPDATE CASCADE ON DELETE CASCADE
```

`ON UPDATE CASCADE` for the parent-key reason catalog-029 makes it mandatory
(`catalog-029-manifest-chunk-fk.xml:49-52`; fk-002-5 already uses it on this
very table). `ON DELETE CASCADE` because an assignment whose chunk is gone is
exactly the dangling row census leg C1 exists to report.

**This decision was reversed once during drafting and then reversed back. The
reason is itself a finding, and it is recorded rather than tidied away.**
`ChashCensus.java:243-247` asserts that `topic_assignments.doc_id` "is a mixed
identity space that also holds memory-note titles (RDR-180 Item2)". Taken at
face value that kills the conversion, since `decode(doc_id,'hex')` would raise
on every title. It does not survive checking:

- **RDR-180 says the opposite, three times.** Item6: "`topic_assignments`
  (its `doc_id` is a chunk chash - nexus-sa14p; the taxonomy-001 header's
  'doc tumblers' comment is stale)"
  (`docs/rdr/rdr-180-content-address-chash-binary-32byte.md:80`). Item6a:
  "`topic_assignments` (doc_id = chunk chash, no FK)" (`:82`). Failure Modes:
  "`topic_assignments.doc_id` is a chunk chash with NO FK (soft reference by
  design)" (`:135`). The `(RDR-180 Item2)` citation in the census comment
  points at a section about converting the five poison columns, which does not
  mention this table at all.
- **The claim originates as an INFERENCE from a DELETE predicate.** Its source
  is `rdr180-001-bytea-chash.xml:10-19`, whose entire stated evidence is
  "`TaxonomyRepository.purgeAssignmentsForDoc` binds a title". That method's
  parameter is NAMED `title` (`TaxonomyRepository.java:799-805`) because its
  caller has a `(project, title)` pair, and `http_taxonomy_store.py:438-452`
  returns `{"doc_id": d, "title": d}` - the "title" IS the doc_id. A delete
  predicate's parameter name is not evidence of what any INSERT writes.
- **The one real memory-note-clustering path died with SQLite.** It lived in
  `src/nexus/db/t2/catalog_taxonomy.py`'s `LEFT JOIN memory m ON m.title =
  ta.doc_id`, deleted at commit `f24bdb853` (nexus-i711w Stage 2C). Nothing
  replaced it.
- **The inference propagated verbatim into five more places**:
  `rdr180-002:288`, `src/nexus/db/chash_tables.py:247`,
  `StagingPromoteOps.java:955`, `ChashCensus.java:246`, and the RDR-180
  post-mortem. Correcting all six is part of this phase, not a follow-up: an
  unverified claim repeated six times is what made this decision take two
  reversals, and leaving it in place guarantees a third.

**Every live writer emits hex, and the census of them is closed.** The plpgsql
writers insert `encode(c.chash,'hex')` selected FROM `nexus.chunks` at all six
INSERT sites (`taxonomy-006-assign-from-chashes.xml:110, 140, 191, 221, 272,
302` - six blocks, two per dim, not the five the research recorded; current
bodies at `vectors-005-repoint-functions-views.xml:2412, 2574, 2736`), so
those are structurally 64-hex. `TaxonomyRepository.java:373` (mergeTopics) and
`:2194` (rebuild transfers) carry existing keys forward. `:2331`
(batchInsertAssignments) takes T3 chunk ids from the rebuild spec. `:516`/`:546`
(assignOne) and `:1235`/`:1435` (legacy ETL) are caller-supplied. On the
Python side every producer is a chunk id: `taxonomy_cmd.py:157-164` into
`_fetch_service_vectors:70-140` pages `collection.get()`, and `:40-64`
enumerates `t3.list_collections()` only - T2 memory is never a source;
`mcp_infra.py:825-842` states "doc_ids ARE the chunk chashes".

**Two paths can still admit a non-hex value, and both are closed in this
phase rather than assumed away.**

- `StagingPromoteOps.java:974-986`: the legacy-SQLite ETL passthrough,
  `COALESCE(hex(chash_alias.new_chash), staging.doc_id)` with a
  `notLikeRegex` legacy arm. This is the only production path that can write
  arbitrary text. It gains an explicit reject on a non-conformant value, with
  the offending value named in the error, rather than passing it through to a
  column that can no longer hold it.
- `nx taxonomy assign <DOC_ID> <LABEL>` (`taxonomy_cmd.py:883-899`): an
  unvalidated click argument. It gains 64-hex validation at the CLI boundary,
  so the operator gets a named error instead of a Postgres decode failure.

`rdr180-001-bytea-chash.xml:22` hedges on "historic tumblers in
topic_assignments" from those same eras. That hedge is the reason the
conversion is gated on a count rather than assumed (cloud-count-4), not a
reason to skip the conversion.

**Why `chunks(chash)` and not `catalog_documents(tumbler)`.** The one FK this
table ever had, `fk_ta_catalog_doc`, was added and then DROPPED (nexus-sa14p)
precisely because it referenced `catalog_documents(tumbler)`, "a different
identity space", and "could never be satisfied for chash-keyed rows"
(`TaxonomyRepository.java:1215-1219`). A tumbler FK would repeat that against
the same table. The stale comment that would motivate it
(`taxonomy-001-baseline.xml:99`, "tumblers") is corrected in this phase, and
RDR-180 already flagged it as stale at `:80`.

**Why the composite is expressible now and was not before.** RDR-191 unified
the three dim shards into `nexus.chunks` with PK
`(tenant_id, collection, chash)` (`vectors-004-unify-chunks.xml:262-273`).
Before unification the referent lived in exactly one of three tables and no
single declarative FK could name it. A single-column FK to `chunks(chash)`, as
Q1 literally posed it, is still not expressible: `chash` alone is not unique.
The composite needs `source_collection`, which makes the writer fix below a
prerequisite rather than a nicety.

**Prerequisite: the writer must persist `source_collection` on the `centroid`
branch.** At current HEAD the retargeted `assign_from_chashes_<dim>` writes
`source_collection` only on the `projection` branch
(`vectors-005-repoint-functions-views.xml:2445-2451`); the `centroid` branch
inserts `(tenant_id, doc_id, topic_id, assigned_by)` only (`:2478-2481`, and
its 768/1024 twins at `:2574` and `:2736`) even though `c.collection =
p_collection` is that branch's own filter three lines above. The collection is
known and discarded. Without this fix every `centroid` row carries NULL
`source_collection`, escapes the composite FK through MATCH SIMPLE, and the
VALIDATE goes green over a mostly-exempt table - the vacuous-gate failure
mode. `source_collection` then becomes NOT NULL.

**Remediation, in order, each counted first (D0.4).** (a) fix the writer and
the two admitting paths; (b) backfill `source_collection` for existing NULL
rows whose `doc_id` resolves to exactly ONE collection in `nexus.chunks`;
(c) DELETE the ambiguous remainder (a chash present in more than one
collection cannot be attributed after the fact) and the unresolvable
remainder, RAISE NOTICE'ing both counts - priced as safe here because topic
assignments are DERIVED data, recomputable by re-running assignment, and the
writers' own `ON CONFLICT DO NOTHING` makes recomputation idempotent;
(d) SET NOT NULL; (e) `ALTER COLUMN doc_id TYPE bytea USING
decode(doc_id,'hex')` plus ANALYZE (the PK `(tenant_id, doc_id, topic_id)` and
the four `doc_id` indexes at `taxonomy-001-baseline.xml:114-118` rebuild);
(f) the three-step FK.

**Fail-loud point, and the named stop.** Step (e) is all-or-nothing: it raises
on any non-hex `doc_id` rather than coercing, and no CASE branch is added to
absorb one. cloud-count-4 runs FIRST, with the predicate `WHERE doc_id !~
'^([0-9a-f]{2})+$'` plus a length census. **If that count is nonzero, the
phase STOPS and the rows are inspected before anything converts.** If they
prove to be historic tumblers or ETL-era external ids from the two paths
above, they are deleted as unrecomputable derived garbage with the count
recorded. If they prove to be a live identity space this Decision did not
find, D1 is re-decided as a documented no-FK, and the census leg stays. That
branch is named in advance precisely because the inference chain above shows
how easily this column's contents get asserted rather than measured.

**Test-suite debt, in scope.** Fixtures write non-hex `doc_id` today
(`tests/test_taxonomy.py:1010-1022`,
`tests/test_taxonomy_rebuild_link_cleanup.py:167-173`,
`TaxonomyRepositoryTest.java:865-1357`, and `seed_legacy.py:880-900` seeds a
16-char legacy chash). They break at the conversion by design and are fixed in
the same phase. A fixture that could not be written after the change is the
point of the change, not collateral.

**Readers that change.** Java only. Roughly twenty-five
`TaxonomyRepository.java` sites bind or read `doc_id` as an opaque String
(`:375, 384, 394, 518, 527, 548, 554, 706-711, 718-724, 762-774, 787-792, 802,
1030, 1237, 1246, 1436, 1446-1451, 1820, 1875, 1923, 2086, 2094, 2196, 2202,
2332, 2335-2340`), plus `StagingPromoteOps.java:975, 986` and seven
`TaxonomyHandler.java` sites (`:412, 452, 514, 529, 659, 696, 945`). jOOQ
regeneration turns the compiler into the work list for the repository half.
The handler half is the part the compiler will NOT catch, because those sites
already handle `String` and keep compiling against a `String` DTO while the
value underneath changes shape; they are enumerated here for exactly that
reason and each gains an explicit hex encode outbound and decode inbound
(D0.8).

**Python is unchanged.** It sends and receives hex end to end
(`src/nexus/db/t2/http_taxonomy_store.py:409, 422, 430-435, 445-470, 472-495,
740-753, 901, 939-949`; `search_engine.py:425, 673, 698, 734-738`;
`scoring.py:312, 337`) and continues to; two of those sites key a dict by
`doc_id`, which is a further reason the wire stays hex rather than moving to
bytes or base64. `taxonomy-006`'s own `RETURNS TABLE (chash text)` contract is
likewise unchanged. Only the column under it moves. The single Python change
is the `nx taxonomy assign` argument validation named above.

**Census leg.** `ChashCensus` leg C1, `dangling.topic_assignments`
(`ChashCensus.java:253-256`, sharing `unresolvableHexCount` at `:212-221`),
retires in the same commit as this FK's VALIDATE, and not before (D0.10).
`ChashCensus` has no `nx doctor` flag and no HTTP endpoint: it runs FATAL
inside `StagingPromoteOps.finalizeTenant` (`:1224-1231`, reached via
`POST /v1/staging/finalize`) and report-only in `RekeyOps.java:774-780`.
Retiring a leg therefore narrows what `finalize` refuses on, which is why it
must land WITH the FK that makes the leg impossible, never before it. C1's
"either era" shape filter (`ChashCensus.java:195`) exists only to accommodate
the titles claim disproved above, so it retires with the leg rather than being
tightened first.

Consequence recorded: with this FK ON DELETE CASCADE, any deletion of a `nexus.chunks` row (`purge_trash`'s orphan-chunk sweep, RDR-192-class superseded-chunk reaping, collection deletion) now also removes that chunk's `topic_assignments` rows. That is the intended semantics (an assignment for a chunk that no longer exists is exactly a dangling leg the ChashCensus reports today) and it is what retires census leg C1; it is called out here so no later reader mistakes the cascade for data loss.

### D2 (Q2). `catalog_links`: FK with ON DELETE CASCADE on both tumbler columns

**Decision.** Add two FKs on `nexus.catalog_links`,
`(tenant_id, from_tumbler)` and `(tenant_id, to_tumbler)`, each referencing
`nexus.catalog_documents (tenant_id, tumbler)` (that pair is the table's PK,
`catalog-001-baseline.xml:96`), `ON DELETE CASCADE ON UPDATE CASCADE`, in the
three-step shape. Do NOT add an explicit delete step to
`deleteCollectionTxn`.

**Why the FK and not the explicit step.** Three facts decide it.

1. **The creation path is already guarded; deletion is the sole producer.**
   `CatalogRepository.upsertLink` calls `requireLiveEndpoints` unless the
   caller passes `allow_dangling`
   (`CatalogRepository.java:3086-3092`), and `CatalogHandler.java:702`
   returns `400 {"code":"dangling_endpoint"}`. So the 277 rows measured on
   the live store (nexus-ysrwi, 2026-07-25) were not created dangling; they
   were made dangling by document deletion. Prevention therefore has to live
   at the deletion sites, and there is more than one.
2. **At least one deletion site is unreachable from Java.**
   `deleteCollectionTxn`'s step 6 (`CatalogRepository.java:6024`) is one; the
   other is `nexus.purge_trash`'s Step 4, a `DELETE FROM
   nexus.catalog_documents` executing entirely inside a plpgsql body
   (`catalog-029-manifest-chunk-fk.xml:328-331`), with no Java on the path.
   An explicit Java step cannot cover it. A `catalog_links` FK does, for
   free, because `purge_trash` Step 4 already relies on fk-001's CASCADE to
   clear four child tables; this adds a fifth with zero code change.
3. **The explicit step is prevention by remembering.** The sibling pattern
   it would copy (`topic_assignments` gets a collection-scoped DELETE at
   `CatalogRepository.java:5995`, because it has no doc-rooted FK) has to be
   re-applied by hand at every future site. The FK is the only form of this
   that a new call site cannot forget.

**Wire contract change, one flag, stated.** `allow_dangling=True`
(`src/nexus/catalog/http_catalog_client.py:2061-2075`) narrows rather than
disappears. `requireLiveEndpoints` today requires a LIVE document; the FK
requires only that the row EXIST. So after this change `allow_dangling=True`
still writes an edge to a TOMBSTONED document (`deleted_at IS NOT NULL`) and
no longer writes an edge to a tumbler with no row at all. That residual case
must fail loud with the code the client already understands: `upsertLink`
WILL map SQLSTATE 23503 on this constraint (new work in P1, not present today) to the existing
`400 {"code":"dangling_endpoint"}` payload, so the client's own translation
to `ValueError` (`http_catalog_client.py:2044-2049`) keeps working unchanged.
No production caller passes `allow_dangling=True`; the only in-tree caller is
a client test (`tests/catalog/test_http_catalog_client.py:1183`).

**Remediation.** The anti-join deletes every link row whose from- or
to-endpoint has no `catalog_documents` row. A link with a vanished endpoint
is unrepairable bookkeeping, the same class catalog-029-1 prices as safe to
delete (`catalog-029-manifest-chunk-fk.xml:209-214`). Population counted
first: cloud-count-1, below.

**Detection leg.** `nx doctor --check-dangling-links` (42ce8872,
`src/nexus/commands/doctor.py:832` and its flag at `:1402-1417`) retires in
the same commit as this VALIDATE, and not before (D0.10). It is a separate
mechanism from `ChashCensus`, which has no doctor flag at all.

Stated plainly: the pre-flight remediation DELETES the dangling `catalog_links` rows (edges whose endpoint document no longer exists). Those rows have no restore path once removed; the count is recorded in T2 before the delete and the deleted rows are exported to the run's log first, so the loss is bounded, visible, and auditable, never silent.

### D3 (Q3). `chash_remap.new_chash`: convert to bytea; the table stays

**Decision.** `chash_remap` is NOT retired. It is a live, served rekey
membership index (`RemapRepository.java`, `RemapHandler.java`,
`remap-002-membership-function.xml`, and `ChashCensus` reads it), so the T2
inventory's "retire if RDR-180's rekey converged" framing is void. Convert
`new_chash` from TEXT to `bytea`, drop the
`length(new_chash) = ANY (ARRAY[32,64])` CHECK (`rdr180-001-bytea-chash.xml:231-232`)
and replace it with `CHECK (octet_length(new_chash) = 32)`.

**Why convert rather than keep the widened CHECK.** Keeping it has a measured
running cost and a silent fallback. The current membership function
(`vectors-005-repoint-functions-views.xml:789-793`) pays a per-row branch:

```
CASE WHEN r.new_chash ~ '^[0-9a-f]+$' AND length(r.new_chash) % 2 = 0
     THEN decode(r.new_chash, 'hex')
     ELSE convert_to(r.new_chash, 'UTF8') END
```

The ELSE arm silently reinterprets a malformed value as raw UTF-8 bytes and
compares it against a chash. That is exactly the silent-fallback-for-a-
correctness-problem shape D0.9 forbids, and it exists only because the column
is TEXT. Conversion deletes the whole CASE: the body becomes a direct
`LEFT JOIN nexus.chash_alias a ON a.old_bytes = r.new_chash`, preserving the
alias-chaining contract verbatim.

**The 32-character rows are 16-byte legacy refs, not short chashes.** A
32-hex `new_chash` decodes to 16 bytes, which is what
`chash_alias.old_bytes` deliberately carries; that is why the CHECK was
widened rather than the column converted. **[Historical as of 2026-08-16 —
`chash_alias` was DROPPED, see the § Gap 1 / § D7 CORRECTION notes.]** The
conversion therefore requires
cloud-count-2 (below), **sharpened**: the recorded query
(`SELECT length(new_chash), count(*) ... GROUP BY 1`) is necessary but not
sufficient, because neither the CHECK nor `RemapRepository.java:100-105`
enforces hex-ness (only length), and `RemapHandler.normalizeChash`
(`RemapHandler.java:421-426` -> `Chash.requireCanonical`) enforces 64-hex for
NEW facts only. The precondition adds a non-hex count.

**Fail-loud arms.** A nonzero 32-char count means legacy-era rows are still
live: the named remedy is to resolve them through `chash_alias` to their
canonical 32-byte form (exactly what `remap_membership` does at read time)
and re-record, or to clear the leg through the existing
`POST /v1/remap/clear_leg` surface (`RemapHandler.java:329-339`). A nonzero
non-hex count is corruption: clear the leg. Neither is absorbed by widening
the target CHECK.

**No FK from `chash_remap` to `chunks`.** Deliberately loose, and recorded as
such: a remap fact asserts a claim whose target may legitimately not be
present yet, which is the entire measurement `remap_membership` performs
(`present_count` vs `mapped_total`). An FK would make the function
tautological. `chash_remap.old_id` stays unconstrained TEXT for the same
reason it is documented as "any pre-remap id"
(`RemapRepository.java:110-112`): it names a foreign identity space.

**Wire contract.** Unchanged. `new_chash` remains a 64-hex string on
`/v1/remap/*` in both directions; `RemapRepository` encodes at the boundary
(D0.8).

### D4 (Q4). tenant-in-PK: decided per table, not en bloc

**First, a census correction this Decision depends on.** The census's
`tenant_in_some_unique` column is a `pg_constraint`-only check. The
Constraints section already records that it misses `migration_jobs`' partial
unique index; reading the DDL for all nine tables shows it misses **six**,
not one. Five of the nine carry a plain (non-partial) tenant-scoped UNIQUE
INDEX that `pg_constraint` cannot see:

| table | PK | tenant-scoped uniqueness that actually exists | ruling |
|---|---|---|---|
| `nx_answer_runs` | `(id)` BIGSERIAL | `idx_nx_answer_runs_etl_dedup (tenant_id, question, created_at)`, `telemetry-001-baseline.xml:204-206` | **leave** |
| `hook_failures` | `(id)` BIGSERIAL | `idx_hook_failures_etl_dedup (tenant_id, doc_id, hook_name, occurred_at)`, `telemetry-001-baseline.xml:250-252` | **leave** |
| `relevance_log` | `(id)` BIGSERIAL | `idx_relevance_log_etl_dedup (tenant_id, query, chunk_id, action, COALESCE(session_id,''), timestamp)`, `telemetry-001-baseline.xml:74-76` (expression index) | **leave** |
| `tier_writes` | `(id)` BIGSERIAL | `idx_tier_writes_etl_dedup (tenant_id, session_id, ts, tool, tier)`, `telemetry-001-baseline.xml:159-161` | **leave** |
| `aspect_promotion_log` | `(id)` BIGSERIAL | `idx_aspect_promo_etl_dedup (tenant_id, field_name, promoted_at)`, `aspects-001-baseline.xml:216-218` | **leave** |
| `migration_jobs` | `(job_id)` TEXT | `idx_migration_jobs_active_dedup (tenant_id, collections_key) WHERE state IN ('queued','running')`, `migration-001-baseline.xml:91-93` (partial) | **change: PK -> `(tenant_id, job_id)`** |
| `gc_audit` | `(id)` BIGSERIAL | none, `catalog-018-gc-audit.xml:36-52` | **leave** |
| `claude_assisted_remediation_consents` | `(id)` BIGSERIAL | none, `telemetry-002-consents.xml:33-46` | **leave** |
| `topics` | `(id)` BIGSERIAL | none, `taxonomy-001-baseline.xml:47-62` | **change: add UNIQUE `(tenant_id, id)`, repoint four FKs** |

**Decision rules applied.**

- **A BIGSERIAL surrogate PK cannot collide across tenants.** A sequence hands
  out one value per row for the whole table, so `PRIMARY KEY (id)` already
  gives per-tenant uniqueness for free, and promoting it to `(tenant_id, id)`
  buys nothing while rewriting the PK index. That is the ruling for the seven
  "leave" rows, five of which additionally carry a tenant-scoped UNIQUE index
  for their own ETL-dedup reasons. `gc_audit` and
  `claude_assisted_remediation_consents` are append-only audit trails with no
  natural key at all; RLS FORCE plus a sequence PK is the correct and
  sufficient posture, and this Decision records that reason at the table
  rather than leaving it to be re-derived.
- **`migration_jobs` is the one genuine gap.** Its PK is a caller-visible TEXT
  `job_id`, not a sequence, so two tenants CAN collide, and the collision
  surfaces as a PK violation that leaks the existence of another tenant's job.
  The partial dedup index protects only rows in `('queued','running')`, so
  every finished job is unprotected. Nothing FK-references `migration_jobs`,
  so the clean fix is the composite PK `(tenant_id, job_id)`. The partial
  index stays.

  > **CORRECTION (2026-08-20, P5b implementation, stacked-review round, T2
  > `substantive-critique-tk070-p5b-2026-08-20`):** the tenant-keying remedy
  > above is SUPERSEDED. `nexus.migration_jobs` has ZERO live producers or
  > consumers — `MigrationHandler.java` and `MigrationJobRepository.java`
  > were deleted at commit `7bcf29c67` (2026-07-24), before this Decision
  > was even researched, and this research missed it. Sam's disposition
  > (2026-08-20, relayed via the orchestrator): DROP the table outright
  > rather than widen the PK of a table nothing writes to or reads from.
  > `migration-002-tenant-pk.xml` was reworked from a PK-widening changeset
  > to a `DROP TABLE` changeset (same file, same changeset id
  > `migration-002-1`, `EXPECT_NEW_CHANGESETS=1` unchanged). The "clean fix
  > is the composite PK" sentence above is the ORIGINAL, now-superseded
  > analysis — correct as far as it went, wrong about whether the table was
  > worth fixing at all. See the § P5b phasing bullet below for the matching
  > correction to what actually shipped.
- **`topics` is the "surrogate PK that other FKs reference" case.** Its
  `id` is the target of four FK columns, and all four are tenant-blind:
  `topics.parent_id` (`taxonomy-001-baseline.xml:60`),
  `topic_assignments.topic_id` (`:104`), and `topic_links.from_topic_id` /
  `.to_topic_id` (`:132-133`). PostgreSQL's referential-integrity checks run
  outside row security, so a tenant-blind FK accepts a reference to another
  tenant's topic. The PK stays (four FKs depend on it) and the table gains
  `UNIQUE (tenant_id, id)`, per the rule. The four FKs are then repointed to
  the composite as a SEPARATE, independently-gated step, so the UNIQUE lands
  even if the repoint's precondition comes back nonzero.

**Fail-loud point.** The repoint's precondition is a cross-tenant reference
count that MUST be zero (cloud-count-4, below). A nonzero count is
cross-tenant data corruption, not a population to remediate silently: the
named remedy is to quarantine the offending rows, report them, and re-run
taxonomy assignment for the affected tenants, since assignments are derived
(D1). No preventive scope is claimed beyond this: no cross-tenant population
has been observed, and the count is the evidence gate.

> **CORRECTION (2026-08-20, P5a implementation, code-review round, T2
> [22964]):** "cloud-count-4" two sentences above is a pre-existing typo in
> this RDR's own text — D4's precondition is **cloud-count-5**, exactly as
> the Gate-preconditions table below (`cloud-count-5 | ... | P5a repoint`)
> and every other D4 reference in this file already say. `cloud-count-4` is
> D1's own precondition (`topic_assignments.doc_id`'s bytea conversion), a
> different gate on a different table. Not a diff defect in the shipped
> `taxonomy-014-topics-tenant-unique.xml` — that file's own header names
> cloud-count-5 correctly throughout — only this one prose sentence drifted.

**`service_tokens` / `session_tokens` stay out of scope.** They are the only
in-scope tables with `rls_enabled=f`, they are auth-token tables rather than
catalog or knowledge data, and this RDR does not touch auth posture. Recorded
here so the exclusion is deliberate rather than an omission.

### D5 (Q5). TTL: one semantics (`NULL` = permanent), one name (`ttl_days`)

**Decision.** `NULL` means permanent in every TTL column. `0` becomes
UNREPRESENTABLE, enforced by `CHECK (ttl_days IS NULL OR ttl_days > 0)` on
every one of them. The column is named `ttl_days` everywhere.

**Why NULL and not frecency's 0.** Only one of the two candidate semantics
can be defended by a constraint. Under `NULL` = permanent the ambiguous value
is simply not writable, so the trap becomes structurally impossible; under
`0` = permanent, `0` is both a legal integer TTL and a sentinel and no CHECK
can distinguish a caller who meant "no expiry" from one who meant "zero
days". `NULL` = permanent is also already the STORE-level contract for the
larger-stakes table: `MemoryRepository.expire()` selects
`WHERE MEMORY.TTL IS NOT NULL` (`MemoryRepository.java:566-573`) and computes
`effectiveTtl = ttl * (1 + ln(access_count + 1))`
(`MemoryRepository.java:584`), so `ttl=0` yields `effectiveTtl=0` and the row
is deleted on the first sweep. That is the trap, verbatim, and it is a store
property, not a client bug.

**Why the rename.** `ttl` is unit-less, which is precisely what lets `0` read
as "no TTL" rather than "zero days". All four columns are integer days.
`ttl_days` names the unit and removes the second axis of the wart at the cost
of one metadata-only rename per table.

**Migration of existing rows, per table.**

- `nexus.memory.ttl` -> `ttl_days`. Values unchanged (`NULL` already means
  permanent). Add the CHECK. Any existing `ttl = 0` row is a row already
  scheduled for deletion by the next sweep; the migration DELETEs it rather
  than admitting it under the new CHECK, with the count RAISE NOTICE'd.
  Counted first (phase-local precondition).
- `nexus.plans.ttl` -> `ttl_days`. The research flagged this write path as
  untraced; it is now traced, and it is a THIRD variant rather than a copy of
  either. `NULL` already means permanent, `0` is not special-cased, and there
  is NO sweep at all: expiry is a READ-TIME predicate,
  `TTL.isNull().or(extract(epoch from now() - coalesce(last_used, created_at))
  / 86400 <= TTL)`, duplicated verbatim at `PlanRepository.java:251-254,
  303-306, 349-352`. So `ttl = 0` here hides the row from every read almost
  immediately without ever deleting it, and the clock is anchored on DISUSE
  (`last_used`), not creation age. The consequence for this Decision is
  favourable: `plans` already has `NULL` = permanent, so only the CHECK and
  the rename apply, and existing `ttl = 0` rows are already unreachable
  through every read path, which is why deleting them matches the `memory`
  treatment rather than diverging from it. `plan_save` needs no client change:
  omitting `ttl` already passes NULL end to end with no coercion
  (`src/nexus/mcp/core.py:4399, 4477`; `http_plan_library.py:87, 146, 209,
  244`; `PlanHandler.java:127, 492-500` returns null on an absent field),
  which is exactly the contract `memory_put` should have had and did not.
- `nexus.frecency.ttl_days` (`INTEGER NOT NULL DEFAULT 0`,
  `telemetry-001-baseline.xml:282`). DROP DEFAULT, DROP NOT NULL,
  `UPDATE nexus.frecency SET ttl_days = NULL WHERE ttl_days = 0`, add the
  CHECK, ANALYZE. This is the only large rewrite in the arc (one row per chash
  per tenant), so its population is counted first and its ANALYZE is
  mandatory under D0.5.
- `staging.frecency` follows `nexus.frecency` mechanically, as the staging
  mirror always does; staging remains otherwise exempt.

**Wire contract change, stated per column.**

- **`memory_put ttl=0` is RETIRED, and the MCP shim is DELETED.**
  `src/nexus/mcp/core.py:3967` currently coerces `ttl=ttl if ttl > 0 else
  None`, which papers over the trap for exactly one caller while
  `POST /v1/memory/put` and the store API stay trapped, as the tool's own
  docstring admits. After this change the ENGINE rejects `ttl=0` with a loud
  `400` naming the fix, for every caller and every path; the MCP tool
  signature becomes `ttl: int | None = 30` with "omit or pass null for
  permanent" documented, and the coercion line is removed rather than moved.
  Retiring the shim is the point: a correctness contract enforced by one
  client is not enforced.
- **`frecency.ttl_days = 0` on the wire is RETIRED.** The T3 read path's
  pre-filter `{"ttl_days": {"$ne": 0}}`
  (`src/nexus/http_vector_client.py:2830`, sentinel documented at
  `:2787-2801`) becomes an `IS NOT NULL` predicate, and `src/nexus/db/t3.py`'s
  `ttl_days = 0 means permanent` docstring and Python-side expiry computation
  (`:750-752, :1123-1124`) change with it. A client that writes
  `ttl_days = 0` gets a loud rejection, not a silent reinterpretation.
- **No other wire field changes.** The column rename is invisible: the HTTP
  field names on `/v1/memory/*` and the plan-library surface stay as they are,
  and the repositories bind the renamed column (D0.8). jOOQ regeneration is
  the Java work list (D0.7).

**Client pairing.** Both bullets above are client-visible, so D5 ships with a
paired client release whose floor names the engine carrying the CHECKs (see
Delivery).

### D6 (Q6). Zero NOT VALID constraints remain: recorded, phase dropped

**Decision.** Q6 is answered, not scheduled. `pg_constraint.convalidated = f`
returns zero rows against a freshly Liquibase-applied schema:
`fk-002-validate.xml` and `fk-003-validate.xml` already validated every FK
from those families, and `catalog-029-2` validated the manifest FK. There is
no backlog of stale NOT VALID constraints for this RDR to work through, so
the draft's P6 "VALIDATE the NOT VALID stragglers" phase is DELETED rather
than carried as an empty phase.

Two things survive from Q6. First, cloud-count-3 (below) still runs, because
a local fresh-Liquibase run proves the changelog is internally consistent, not
that the live install has walked every changeset up to HEAD; a nonzero cloud
result is a deployment-lag finding for the deploy gate, not new RDR scope.
Second, every NOT VALID this RDR itself adds carries its own paired VALIDATE
in the same file and release (D0.3), so the class does not reopen.

### D7. The remaining census edges: deliberately loose, with reasons recorded

The census's mechanical classes do not decide these; this Decision does. Each
gets a `COMMENT ON COLUMN` in its phase so the reasoning is at the column
rather than in this file only.

- **`frecency.chunk_id`** and **`relevance_log.chunk_id`**: NO FK. Not a
  deferral. `nexus.chunks` is keyed `(tenant_id, collection, chash)`
  (`vectors-004-unify-chunks.xml:273`) and neither table has a collection
  column: `frecency`'s PK is `(tenant_id, chunk_id)`
  (`telemetry-001-baseline.xml:286`), one row per chash per tenant ACROSS
  collections by design. Adding a collection column to make an FK expressible
  would change the table's identity grain, which is a behaviour change, not
  hygiene. Their `ChashCensus` legs therefore STAY as the standing detection,
  and D0.10's one-for-one retirement does not fire for them.
- **`pdf_chunks.chunk_id`**: NO FK, same grain argument, and the pdf pipeline's
  wire-parity rationale (`pipeline-001` header) is already excluded from this
  RDR's scope by the Problem Statement.
- **`hook_failures.doc_id`**: NO FK, and the reason is stronger than "no
  evidence". The column holds THREE identity spaces, discriminated by the
  sibling `chain` column: `chain='single'` stores a T3 chunk chash in hex
  (`src/nexus/hook_registry.py:146-151`, ids from `doc_indexer.py:1381`);
  `chain='batch'` stores the FIRST chunk chash of the batch
  (`hook_registry.py:307-312, 602-623`; `mcp_infra.py:755-757`,
  `doc_ids[0] if doc_ids else ""`); `chain='document'` stores a FILE PATH,
  stated in-tree as "Stores source_path in the legacy doc_id column"
  (`hook_registry.py:650-667`; producers `pipeline_stages.py:691-693`,
  `doc_indexer.py:2716-2718`, `aspect_worker.py:1131`). It is never a tumbler
  and never a title: the tumbler travels beside it as a separate
  `catalog_doc_id` kwarg (`hook_registry.py:481`). No single FK target exists
  for a column with three referents, and PostgreSQL has no conditional FK. Two
  further obstacles hold independently: the `NOT NULL DEFAULT ''`
  (`telemetry-001-baseline.xml:234`) can never satisfy an FK, and dropping it
  breaks `idx_hook_failures_etl_dedup (tenant_id, doc_id, hook_name,
  occurred_at)` (`telemetry-001-baseline.xml:250-252`), since NULLs are
  distinct in a unique index and every NULL-`doc_id` failure would stop
  deduplicating. Decision: keep the column opaque, record the three-way
  `chain` discrimination in the column comment (this is the fact the research
  found missing), and name the occurrence-time remedy: if a dangling-`doc_id`
  population is ever observed on the telemetry read path, file then, with the
  count in hand. Q1's answer explicitly does NOT generalize here.
- **`chash_alias.old_bytes`**: NO width CHECK, by design (it carries 16-byte
  legacy refs, which is what makes D3's 32-char rows resolvable). This RDR
  adds the column comment the Problem Statement notes is missing. Adding a
  width CHECK would break D3's own remedy.

  > **CORRECTION (2026-08-20, P4 implementation, verified not assumed):**
  > this bullet is now historical. `nexus.chash_alias` — the table and this
  > column both — was DROPPED 2026-08-16 at
  > `legacy-001-drop-chash-alias.xml` changeset `legacy-001-5` (bead
  > `nexus-lgdel.l1`, epic `nexus-lgdel`, Hal directive: "I'm tired of
  > carrying these legacy things... Delete it... Ours is empty and we don't
  > care."), one day after this bullet was drafted (2026-08-15) and
  > confirmed still present when the bead was last touched (2026-08-17) —
  > the drop landed in the gap between those two dates. `COMMENT ON COLUMN
  > nexus.chash_alias.old_bytes` would fail outright: the column no longer
  > exists. **Disposition (P4, nexus-tk070.p4):** the P4 comment-only
  > changelog (`fk-005-deliberately-loose-edge-comments.xml`) ships the
  > OTHER seven D7 targets and omits this one — not a silent scope
  > reduction, a structural impossibility, documented in that file's own
  > header, in T2 `nexus/tk070-p4-dev-notes-2026-08-20`, and on the bead.
  > D3's remedy this bullet references is likewise superseded: L1
  > simplified `remap_membership` to drop the `chash_alias` LEFT JOIN
  > entirely (`chash_remap` was empty on cloud at drop time, RDR-194 cc2).
  > This decision text is NOT rewritten (D0.2's spirit extended to RDR
  > prose) — it stood correctly at the time it was decided; this note is
  > the pointer a later reader, or the p7 phase-review-gate, needs to not
  > mistake the omission for missed scope.

### D8. Empty-string sentinels: converted only where an FK requires it

Per column, not en bloc, per the Problem Statement's own framing. A `''`
sentinel is converted to NULL in the SAME changeset as the FK that cannot
attach around it, and nowhere else. Under this Decision that means: no
sentinel conversion in D1 (`topic_assignments.doc_id` is already NOT NULL with
no default, and `source_collection` moves from nullable to NOT NULL by
backfill, which is the opposite direction from a sentinel retirement and is
driven by the composite FK's MATCH SIMPLE exemption, not by a `''`), none in
D2
(`catalog_links.from_tumbler`/`to_tumbler` are NOT NULL with no default), and
none for `hook_failures.doc_id`, `gc_audit.collection`/`.actor`, or
`relevance_log.collection`/`.session_id`, since D7 attaches no FK to any of
them. The `catalog_links.created_at` sentinel was already retired at
catalog-031. The remaining sentinels are recorded as accepted, with the FK
that would justify converting them named as the trigger.

### D9. Vestigial `chash_index` prose

The dropped-router references in `fk-002-collection-registry.xml` (changeset
`fk-002-4`) and `fk-002-validate.xml` (changeset `fk-002-10`) sit inside
APPLIED changesets, so under D0.2 they are not edited. A stale-prose
correction is appended to each file's HEADER instead, using the mechanism
`fk-002-validate.xml` already carries for the RDR-191 case.

### Gate preconditions

The three counts the research recorded are **gate preconditions**, not
background reading. Each blocks the phase named beside it, must be run against
the LIVE cloud store, and must be recorded in T2 before the phase ships.

| # | query (verbatim in Constraints and Verified Facts) | gates |
|---|---|---|
| cloud-count-1 | dangling `catalog_links` rows (both endpoint predicates) | P1 |
| cloud-count-2 | `SELECT length(new_chash), count(*) FROM nexus.chash_remap GROUP BY 1`, **sharpened** with a non-hex count per D3 | P2 |
| cloud-count-3 | live `pg_constraint` NOT VALID enumeration | the deploy gate, per D6 |

Each phase that runs an anti-join remediation or a table rewrite adds its own
count under the same rule (D0.4). These are phase-local and are not
substitutes for the three above:

| # | count | gates |
|---|---|---|
| cloud-count-4 | `SELECT count(*) FROM nexus.topic_assignments WHERE doc_id !~ '^([0-9a-f]{2})+$'` plus a length census (64 / 32 / other) and the NULL vs non-NULL `source_collection` split; of the NULL rows, how many resolve to exactly one collection in `nexus.chunks`, more than one, or none. The non-hex arm MUST be zero or its rows dispositioned before P3c converts. A HARD STOP with the re-decision branch named in D1, not a formality | P3b, P3c |
| cloud-count-5 | `topic_assignments` rows whose `topic_id` belongs to another tenant; same for `topic_links` and `topics.parent_id`. MUST be zero | P5a repoint |
| cloud-count-6 | `nexus.memory` and `nexus.plans` rows with `ttl = 0`; `nexus.frecency` rows with `ttl_days = 0` and total row count | P6 |

## Trade-offs

- **An all-or-nothing conversion on a column whose contents were asserted, not
  measured.** D1 is the arc's largest single risk. `ALTER ... TYPE bytea USING
  decode(...)` cannot partially succeed, and the in-tree prose about this
  column has been wrong in two different directions for over a month. The
  trade is that risk against permanently keeping a third chash encoding and an
  unretirable census leg. It is priced by making cloud-count-4 a hard stop
  with a named re-decision branch rather than a formality, and by fixing the
  six propagated prose sites so the next reader is not misled the way this
  RDR's own drafting was.
- **Deleting rows vs preserving them.** D1's ambiguous-remainder deletion,
  D2's dangling-link deletion, and D5's `ttl = 0` sweep all delete rather than
  coerce. The trade is data loss against a coercion that would encode a guess.
  Each population is derived and recomputable (topic assignments),
  unrepairable bookkeeping (a link with a vanished endpoint), or already
  unreachable (a `ttl = 0` memory row is deleted on the next sweep, a
  `ttl = 0` plan is invisible to every read). It does NOT extend to chunks or
  documents, and D3 explicitly declines to delete remap facts, routing them to
  a named remedy instead.
- **One rewrite of a large table.** `frecency`'s `0 -> NULL` update (D5) is
  the arc's only rewrite over a big population. The alternative, keeping
  `frecency` at `0` = permanent and migrating `memory` the other way, is
  cheaper in rows moved and strictly worse in what it can guarantee (no CHECK
  can defend the sentinel). Cost accepted, ANALYZE mandatory, count first.
- **Composite FK with a nullable component.** D1's FK exempts NULL
  `source_collection` rows through MATCH SIMPLE, which is why the writer fix
  and the NOT NULL promotion are part of the same decision rather than
  follow-ups. Shipping the FK without them would produce a green VALIDATE over
  a mostly-exempt table, the vacuous-gate failure mode.
- **Four commits for one column.** D1 is deliberately split (writers, count
  plus backfill, type, FK) so each is independently green and independently
  revertible, and so the count sits at a commit boundary where stopping is
  cheap. The alternative, one large changeset, cannot be bisected when the
  count comes back surprising, which is the case this phase is built for.
- **A narrowed client capability.** D2 narrows `allow_dangling=True` rather
  than preserving it exactly. The trade is a documented, loud, correctly-coded
  refusal for a case with no production caller, against permanently
  un-preventable dangling rows.
- **Column renames touch Java broadly.** D5's `ttl` -> `ttl_days` rename
  produces a wide but mechanical jOOQ-driven diff. The trade is one noisy
  commit against a permanently unit-less column name that is half the cause of
  the trap.
- **What is deliberately NOT bought.** D7 leaves three chash-shaped columns
  unenforced. The arc therefore does not reach "every enforceable relationship
  enforced" in the absolute; it reaches "every relationship enforceable
  WITHOUT changing a table's identity grain is enforced", and it records the
  grain argument at each column so the next census does not re-litigate it.

## Alternatives Considered

**A1. `topic_assignments.doc_id` keeps hex TEXT with a 64-char CHECK and a
documented no-FK (Q1's second branch).** Rejected. It preserves the third live
chash encoding that Problem Statement item 1 exists to retire, it leaves
census leg C1 permanently unretirable so D0.10 can never fire, and the CHECK
it adds constrains width only. That is precisely the shape that already failed
for `chash_remap` (D3): `rdr180-001` widened a CHECK instead of converting, and
the cost surfaced as a per-row runtime decode branch with a silent fallback
arm. Cheap once, expensive forever.

**A1a. Keep TEXT because `doc_id` is a polymorphic identifier holding
memory-note titles as well as chashes.** This was this RDR's working decision
for part of the drafting pass, on the strength of `ChashCensus.java:243-247`.
Rejected on primary evidence: RDR-180 states the opposite at `:80`, `:82` and
`:135`; the claim traces to an inference from a DELETE predicate's parameter
name at `rdr180-001-bytea-chash.xml:10-19`; and the memory-clustering path it
described died with the SQLite store at commit `f24bdb853`. Recorded here
rather than deleted because the alternative was believed, and because the same
prose is still live in six places that D1 corrects.

**A2. `topic_assignments.doc_id` FK to `catalog_documents(tumbler)` (Q1's
implied third option).** Rejected on direct evidence: this exact FK existed as
`fk_ta_catalog_doc` and was DROPPED because tumblers are "a different identity
space" that "could never be satisfied for chash-keyed rows"
(`TaxonomyRepository.java:1215-1219`, nexus-sa14p). RDR-180 had already
recorded the motivating comment as stale (`:80`).

**A3. Single-column FK `topic_assignments.doc_id -> chunks(chash)`, as Q1
literally posed it.** Not expressible. `chunks` is keyed
`(tenant_id, collection, chash)` (`vectors-004-unify-chunks.xml:273`); `chash`
alone is not unique, so there is no single-column target. This is why D1's FK
is composite and why the `source_collection` writer fix is a prerequisite.

**A4. Convert `doc_id` with a tolerant `CASE WHEN doc_id ~ hex THEN
decode(...) ELSE convert_to(doc_id,'UTF8') END`, the expression
`rdr180-001:22` describes.** Rejected under D0.9. It is the same silent
fallback D3 is deleting from `remap_membership`, and it would convert the
open question (are there non-hex rows?) into an un-asked one by absorbing any
answer. The all-or-nothing `decode` plus a counted pre-flight is the fail-loud
form.

**A5. Explicit `catalog_links` delete step in `deleteCollectionTxn` (Q2).**
Rejected. It cannot reach `purge_trash`'s plpgsql `DELETE FROM
nexus.catalog_documents`, it must be re-applied by hand at every future
deletion site, and it protects nothing against the direct-SQL paths an FK
covers by construction. It is also strictly more code than the FK, which
needs no ordering constraint at all (the explicit step must run BEFORE step 6,
a sequencing requirement the FK does not have).

**A6. Both the FK and the explicit step (Q2).** Rejected as redundant work
that also makes the FK's own remediation harder to reason about: with the
step in place, a nonzero anti-join count could come from either mechanism.

**A7. Retire `chash_remap` entirely (Q3, the T2 inventory's framing).**
Rejected on the research's finding: the table backs a live HTTP surface
(`RemapHandler`), a dedicated membership function
(`remap-002-membership-function.xml`), and a `ChashCensus` input. Nothing is
marked deprecated. Retiring it would delete a served capability.

**A8. Keep `chash_remap.new_chash` TEXT and narrow the CHECK to 64 (Q3).**
Rejected. It leaves the encode/decode branch and its silent UTF-8 fallback in
the read path, and it still requires the same cloud count to prove the 32-char
rows are gone. Paying the count without collecting the conversion is the worst
of both.

**A9. Composite PK `(tenant_id, id)` on all nine RLS-only tables (Q4).**
Rejected: a BIGSERIAL PK cannot collide across tenants, so seven of the nine
would rewrite a PK index for no invariant gained. Deciding en bloc is exactly
what the per-table instruction forbids.

**A10. Leave `topics` alone because RLS covers reads (Q4).** Rejected: RLS
covers reads, and PostgreSQL's referential-integrity checks are not reads.
Four tenant-blind FK columns target `topics.id`, so a cross-tenant reference
is schema-legal today. The UNIQUE plus repoint is the minimum that closes it
without disturbing the four dependents.

**A11. Replace `migration_jobs`' TEXT PK with a BIGSERIAL surrogate (Q4).**
Rejected: `job_id` is the caller-visible handle returned by the migration API,
so a surrogate would add an identity without removing one. The composite PK
keeps the handle and scopes it.

**A12. Unify TTL on `0` = permanent, `frecency`'s convention (Q5).**
Rejected: no constraint can defend it (see D5), it forces `memory.ttl` and
`plans.ttl` to NOT NULL and rewrites their NULL rows, and it breaks any client
reading NULL from those columns today. It is cheaper in rows moved and strictly
weaker in what it guarantees.

**A13. Keep the MCP `ttl=0` coercion and add the CHECK behind it (Q5).**
Rejected: that is the current state with a constraint bolted on, and the
constraint would then reject the very callers the shim exists to serve. Fixing
a store-level contract in one client is what produced the trap.

**A14. Unify TTL semantics but leave the two column names (Q5).** Considered
and rejected as a half-measure: the unit-less name is half the reason `0` reads
as "no TTL". The rename is metadata-only per table and rides the same
changeset.

**A15. Schedule a VALIDATE phase anyway "for the live store" (Q6).** Rejected:
there is nothing to validate. The live-store question is a deployment-lag
question, so it belongs to the deploy gate (cloud-count-3), not to a phase.

**A16. Special-case the census script for `doc_id -> tumbler` and
`chunk_id -> chash`.** Rejected for the reason the Constraints section already
gives: it folds this RDR's own Q1 and Q3 decisions into a supposedly mechanical
census. The manual cross-check against `pg_constraint` stays.

## Consequences

**Enforced after the arc.** Three relationships that no constraint guards
today: `catalog_links` to `catalog_documents` on both endpoints (D2),
`topic_assignments` to `chunks` (D1), and tenant-scoped topic references
(D4). One encoding retired (`topic_assignments.doc_id`), one converted
(`chash_remap.new_chash`), leaving `bytea` as the single live chash encoding
apart from the deliberately-untyped `chash_alias.old_bytes`. **[Historical
as of 2026-08-16 — `chash_alias` was DROPPED (`legacy-001-5`,
`nexus-lgdel.l1`); see the § D7 CORRECTION note. `bytea` is now the ONLY
live chash encoding, full stop — the "apart from" carve-out no longer
applies.]**

**Retired detections, one-for-one.** `nx doctor --check-dangling-links` at
D2's VALIDATE; `ChashCensus` leg C1 (`dangling.topic_assignments`) at D1's
VALIDATE, which narrows what `POST /v1/staging/finalize` refuses on
(`StagingPromoteOps.java:1224-1231`). Legs C2 and C3 (`frecency.chunk_id`,
`relevance_log.chunk_id`) STAY, and so does the `pdf_chunks` gap (D7); this
RDR records why, so the next census does not read their survival as an
oversight. Leg C4 (`dangling.catalog_document_chunks`) is untouched.

**Read-path simplification.** `remap_membership` loses its per-row regex plus
CASE plus `convert_to` fallback and becomes a direct bytea join (D3).
`purge_trash` gains a fifth cascade child at no code cost (D2).

**Client-visible changes, both in one paired release.** `memory_put ttl=0` and
any direct `POST /v1/memory/put` with `ttl=0` become a loud `400`; T3 writers
sending `ttl_days = 0` likewise (D5). Nothing else on the wire moves: the
bytea conversions stay hex at the boundary and the TTL column rename is
invisible to clients.

**Narrowed client capability.** `allow_dangling=True` writes edges to
tombstoned documents but not to nonexistent tumblers, refused with the code
the client already translates (D2).

**Deletions.** Ambiguous and unresolvable `topic_assignments` rows (D1),
dangling `catalog_links` rows (D2), and `ttl = 0` memory and plan rows (D5).
Every one is counted before it runs and its count is RAISE NOTICE'd during the
run.

**Java work list.** jOOQ regeneration drives most of it: `TaxonomyRepository`
and `TaxonomyHandler` for the `doc_id` bytea binding, `RemapRepository` for
`new_chash`, `MemoryRepository` and the plan repository for the TTL rename,
`CatalogRepository.upsertLink` for the 23503 mapping,
`StagingPromoteOps.java:974-986` for the passthrough reject. Compilation
failure is the census, with ONE named exception: the seven
`TaxonomyHandler.java` `doc_id` sites keep compiling against a `String` DTO
while the column changes shape, so they are hand-enumerated in D1 and cannot
be left to the compiler.

**Python work list.** `src/nexus/mcp/core.py` (delete the `ttl` coercion,
change the tool signature and docstring), `src/nexus/http_vector_client.py`
and `src/nexus/db/t3.py` (the `ttl_days` predicate and docstrings),
`taxonomy_cmd.py:883-899` (validate the free-form `doc_id` argument),
`src/nexus/db/chash_tables.py:247` (prose correction). No client contract
changes beyond D5's two.

**Prose corrections, six sites, in D1's phase.** `ChashCensus.java:246`,
`rdr180-001-bytea-chash.xml:10-19` (applied file, so a header stale-prose
correction per D0.2), `rdr180-002:288`, `chash_tables.py:247`,
`StagingPromoteOps.java:955`, the RDR-180 post-mortem, plus
`taxonomy-001-baseline.xml:99`'s "tumblers". An unverified inference repeated
seven times cost this RDR two reversals during drafting; leaving it in place
guarantees a third.

**Test-suite work.** Fixtures that write non-hex `topic_assignments.doc_id`
(`tests/test_taxonomy.py:1010-1022`,
`tests/test_taxonomy_rebuild_link_cleanup.py:167-173`,
`TaxonomyRepositoryTest.java:865-1357`, `seed_legacy.py:880-900`) break at
D1's conversion by design and are fixed in the same phase.

**Operational risk accepted.** One large table rewrite (`frecency`) with a
mandatory ANALYZE; three FK VALIDATEs, each taking SHARE UPDATE EXCLUSIVE on
its table while concurrent reads and writes proceed.

**What this RDR does not close.** Three chash-shaped columns stay unenforced
by grain (D7), `service_tokens` and `session_tokens` keep `rls_enabled=f`
(D4), and `hook_failures.doc_id` stays opaque with a named occurrence-time
trigger. Each is recorded at the column, not only here.

## Open Questions (to resolve before gate)

All six are resolved. What remains open is recorded per question, sharpened
rather than restated.

- **Q1 RESOLVED -> D1.** `topic_assignments.doc_id` becomes `bytea` with a
  composite FK `(tenant_id, source_collection, doc_id)` to
  `chunks (tenant_id, collection, chash)`. A single-column FK to
  `chunks(chash)` as the question posed it is not expressible: `chash` alone
  is not unique post-RDR-191. **Sharpened, and the sharpening is the finding:**
  the question's premise (the column holds a chunk chash) is correct, but the
  in-tree prose contradicting it (`ChashCensus.java:243-247` and five other
  sites) is not, and it is wrong on the strength of an inference from a DELETE
  predicate's parameter name (`rdr180-001-bytea-chash.xml:10-19`) that RDR-180
  itself contradicts at `:80`, `:82` and `:135`. Correcting those six sites is
  in D1's phase. **Residuals, all carried into P3:** the `centroid` branch must
  persist `source_collection` before the FK can be non-vacuous; the two paths
  that can still write arbitrary text (`StagingPromoteOps.java:974-986`,
  `nx taxonomy assign`) are closed in the same phase; and cloud-count-4's
  non-hex arm is a hard stop with a named re-decision branch, because
  `rdr180-001:22` hedges on "historic tumblers" that nobody has counted.
- **Q2 RESOLVED -> D2.** FK with ON DELETE CASCADE on both tumbler columns;
  no explicit step. **Sharpened:** the question framed the two options as
  equivalent-coverage alternatives, and they are not. The creation path is
  already guarded (`CatalogRepository.java:3086-3092`), so deletion is the
  sole producer, and one deletion site (`purge_trash` Step 4) is plpgsql-only
  and unreachable from a Java step. **Residual:** the `allow_dangling` wire
  narrowing, decided in D2 and to be re-stated in the client's own docstring
  at P1.
- **Q3 RESOLVED -> D3.** Convert `new_chash` to `bytea`; the table is NOT
  retired (the "retire if converged" half of the question is void on the
  research's finding). **Sharpened:** the recorded cloud count is necessary
  but insufficient. Neither the CHECK nor `RemapRepository.java:100-105`
  enforces hex-ness, so the precondition adds a non-hex count, and a 32-char
  row means a 16-byte legacy ref rather than a short chash.
- **Q4 RESOLVED -> D4, per table.** Seven leave, `migration_jobs` gets a
  composite PK, `topics` gets a tenant-scoped UNIQUE plus an FK repoint.
  **Sharpened:** the question rested on the census's nine-table RLS-only set,
  and that set is wrong in one direction: five of the nine carry a plain
  tenant-scoped UNIQUE INDEX invisible to `pg_constraint`, on top of the
  partial index the Constraints section already flags for `migration_jobs`.
  The census column undercounts by six, not one. **Residual:**
  `service_tokens` / `session_tokens` `rls_enabled=f`, explicitly out of scope
  and recorded as such.
- **Q5 RESOLVED -> D5.** `NULL` = permanent everywhere, `0` unrepresentable
  by CHECK, `ttl_days` as the single name; existing-row migration and both
  wire-contract retirements (`memory_put ttl=0`, `frecency ttl_days=0`)
  stated per column in D5. **Sharpened:** the research left `plans.ttl`
  untraced and it is now traced. It is a THIRD variant, not a copy of either:
  `NULL` = permanent as in `memory`, but there is no sweep at all, only a
  read-time predicate duplicated at `PlanRepository.java:251-254, 303-306,
  349-352`, so `ttl = 0` hides a plan forever instead of deleting it. That
  makes three columns with three different consequences for the same written
  value, which is the strongest single argument for D5's CHECK.
  **Residual, not scope:** the read-time predicate is copy-pasted at three
  sites. Nothing in this RDR depends on de-duplicating it, and no defect has
  been observed from the triplication, so it is recorded here as a finding to
  bead separately rather than folded into D5.
- **Q6 RESOLVED -> D6.** Zero NOT VALID constraints remain; the phase is
  deleted rather than carried empty. **Residual:** cloud-count-3 moves to the
  deploy gate, where a nonzero result is deployment lag, not RDR scope.

## Phasing

Each phase is commit-sized, independently green, and independently
revertible. Every phase runs, before it is considered done: `./mvnw test`
(full engine suite), `scripts/build-gate-jar.sh`, `uv run pytest -n auto`,
`uv run pytest -m lint` (the changelog lints in D0: analyze, rollback, rls,
markran, validate-precondition), and the stacked reviewers
(`/conexus:review-code` then `/conexus:substantive-critique`). Every phase
that adds changesets runs
`EXPECT_NEW_CHANGESETS=<n> tests/e2e/migration-rehearsal/run.sh
--candidate-migration` BEFORE any engine cut, with `<n>` pinned to the
authored file's own changeset count. The counts below are design intent; they
are re-pinned to the file as authored, never the reverse.

**P0. Census and live counts.** Done for the local half
(`scripts/sql/fk_census.sql`, `tests/db/test_fk_census.py`, 7/7 green). The
remaining work is running cloud-count-1, -2 and -3 against the live store and
recording all three in T2. No changesets. Gate: the three counts recorded.

**P1. `catalog_links` FK (D2).** New changelog
`catalog-032-links-tumbler-fk.xml`: ADD both FKs NOT VALID (1 changeset, two
ALTER statements), anti-join remediation RLS-toggle-wrapped with a
RAISE NOTICE'd count (1), VALIDATE per constraint (2). Java companion in the
same commit: map SQLSTATE 23503 on these constraints in
`CatalogRepository.upsertLink` to the existing
`400 {"code":"dangling_endpoint"}`; update the `allow_dangling` docstring in
`src/nexus/catalog/http_catalog_client.py`. Retire `nx doctor
--check-dangling-links` in this same commit (D0.10). Precondition:
cloud-count-1. `EXPECT_NEW_CHANGESETS=4`.

**P2. `chash_remap.new_chash` -> bytea (D3).** New changelog
`remap-003-new-chash-bytea.xml`: ALTER COLUMN TYPE with
`USING decode(new_chash,'hex')`, drop the 32-or-64 CHECK, add
`CHECK (octet_length(new_chash) = 32)`, ANALYZE. Layered `CREATE OR REPLACE`
of `remap_membership` in the same file removing the CASE branch. Java:
`RemapRepository` binds `byte[]`, encodes hex at the boundary; the
length-only guard at `:100-105` becomes a canonical-form guard. Precondition:
cloud-count-2, sharpened. `EXPECT_NEW_CHANGESETS=2`.

**P3. `topic_assignments` (D1), four commits.** The arc's highest-risk phase,
and the one whose ordering is not negotiable: every writer that could produce
a non-conformant value is closed BEFORE anything is counted, and the count is
taken BEFORE anything converts.
- **P3a writers and admitting paths.**
  `taxonomy-009-assign-source-collection.xml`: layered `CREATE OR REPLACE` of
  `assign_from_chashes_{384,768,1024}` on `vectors-005-16/17/18`, persisting
  `source_collection` on the `centroid` branch. Java companion:
  `StagingPromoteOps.java:974-986` rejects a non-conformant passthrough value
  by name instead of writing it. Python companion:
  `taxonomy_cmd.py:883-899` validates the free-form `doc_id` argument. Also
  the seven prose corrections (D0.2 header form for the applied
  `rdr180-001`), including `taxonomy-001-baseline.xml:99`'s stale "tumblers".
  No data change. `EXPECT_NEW_CHANGESETS=3`.
- **P3b count and backfill.** Run cloud-count-4 FIRST and record it in T2.
  **If its non-hex arm is nonzero, STOP and inspect** per D1's named
  re-decision branch; do not proceed to P3c on a nonzero count. Then
  `taxonomy-010-source-collection-backfill.xml`: unique-resolution backfill,
  counted deletion of the ambiguous and unresolvable remainders with both
  counts RAISE NOTICE'd, SET NOT NULL. `EXPECT_NEW_CHANGESETS=1`.
- **P3c type.** `taxonomy-011-doc-id-bytea.xml`: ALTER COLUMN TYPE bytea
  USING `decode(doc_id,'hex')`, ANALYZE (PK plus four indexes rebuild). Java:
  jOOQ regeneration for the repository half, hand edits at the seven
  `TaxonomyHandler` sites the compiler will not flag. Test fixtures fixed in
  this commit. SQL functions (plan audit 2026-08-15, HIGH): six live functions
  bind `doc_id` as hex TEXT and would fail at first call after the ALTER with
  no Liquibase error, since nothing depends on them as a view: the writers
  `nexus.assign_from_chashes_{384,768,1024}` (vectors-005:2412/2574/2736,
  `encode(c.chash,'hex') AS m_chash`) and the readers
  `nexus.search_topic_scoped_{384,768,1024}` (`ta.doc_id = c.chash` join).
  The same file carries CREATE OR REPLACE for all six binding `doc_id` as
  bytea (`c.chash` directly, no encode/decode), and a post-migration
  round-trip test writes through `assign_from_chashes` and reads through
  `search_topic_scoped` against `nexus.chunks` rows.
  `EXPECT_NEW_CHANGESETS=1+6` (re-pinned to the file as authored).
- **P3d FK.** `taxonomy-012-doc-id-chunk-fk.xml`: the three-step shape.
  Retire `ChashCensus` leg C1 and its "either era" shape filter in this same
  commit. `EXPECT_NEW_CHANGESETS=3`.

**P4. `hook_failures.doc_id` and the recorded-reason comments (D7, D8, D9).**
Comment-only changelog plus the two file-header stale-prose corrections. No
DDL beyond `COMMENT ON COLUMN` for `hook_failures.doc_id`,
`chash_alias.old_bytes`, `frecency.chunk_id`, `relevance_log.chunk_id`,
`pdf_chunks.chunk_id`, `chash_remap.old_id`, `gc_audit`, and
`claude_assisted_remediation_consents`. `EXPECT_NEW_CHANGESETS=1`.

> **CORRECTION (2026-08-20, P4 implementation, verified not assumed):**
> `chash_alias.old_bytes` in the target list above is N/A as shipped — the
> column (and its table) was DROPPED 2026-08-16, before P4 executed; see
> the § D7 CORRECTION note for the full disposition. P4 shipped
> `COMMENT ON COLUMN` for the other SEVEN targets listed here plus the four
> D8 accepted-sentinel columns (`gc_audit.collection`/`.actor`,
> `relevance_log.collection`/`.session_id`), still within
> `EXPECT_NEW_CHANGESETS=1` (one file, one changeset). This is not a scope
> reduction to flag at close-out — read this note, not the omission, as
> the disposition.

**P5. Tenant keying (D4), two commits.**
- **P5a `topics`.** `taxonomy-013-topics-tenant-unique.xml`: add
  `UNIQUE (tenant_id, id)`. Then, as a separate changeset in the same file
  gated on cloud-count-5 being zero, DROP and re-ADD the four FKs as
  tenant-scoped composites in the three-step shape. If cloud-count-5 is
  nonzero the UNIQUE still ships and the repoint waits behind the named
  remedy in D4. `EXPECT_NEW_CHANGESETS=5`.
- **P5b `migration_jobs`.** `migration-002-tenant-pk.xml`: PK ->
  `(tenant_id, job_id)`, partial index retained, ANALYZE.
  `EXPECT_NEW_CHANGESETS=1`.

  > **CORRECTION (2026-08-20, P5b implementation, stacked-review round, T2
  > `substantive-critique-tk070-p5b-2026-08-20`):** this bullet describes
  > what P5b was PLANNED to ship, not what shipped. `nexus.migration_jobs`
  > was found dead during implementation (zero producers/consumers,
  > `MigrationHandler.java`/`MigrationJobRepository.java` deleted at
  > `7bcf29c67`, 2026-07-24) and Sam's disposition (2026-08-20) was to DROP
  > the table instead. `migration-002-tenant-pk.xml` was reworked to a
  > single `DROP TABLE nexus.migration_jobs` changeset (pre-drop row-count
  > NOTICE, shape-agnostic, rollback recreates the baseline shape empty) —
  > same file, same changeset id, `EXPECT_NEW_CHANGESETS=1` unchanged. See
  > the § D4 CORRECTION above for the full disposition.

**P6. TTL (D5), two commits.** No longer blocked: the Q5 residual
(`plans.ttl`'s write and expiry path) is traced in D5.
- **P6a `memory` and `plans`.** `memory-003-ttl-days.xml` and
  `plans-003-ttl-days.xml`: counted deletion of `ttl = 0` rows, RENAME
  COLUMN, add the CHECK. Python companion: delete the coercion at
  `src/nexus/mcp/core.py:3967` and change the `memory_put` signature and
  docstring. `EXPECT_NEW_CHANGESETS=2`.
- **P6b `frecency`.** `telemetry-006-frecency-ttl-null.xml`: DROP DEFAULT,
  DROP NOT NULL, `0 -> NULL` update, CHECK, ANALYZE; staging mirror in the
  same file. Python companion: `http_vector_client.py` predicate and
  `db/t3.py` docstrings and Python-side expiry. Precondition: cloud-count-6.
  `EXPECT_NEW_CHANGESETS=2`.

**P7. Close-out.** Verify every census leg that D0.10 retired is gone and
every leg D7 preserved is still running; re-run `tests/db/test_fk_census.py`
and extend its ground truths to pin the three new FKs as
`fk_enforced`+VALIDATED (the same positive-control shape
`fk_catalog_chunks_chunk` already provides); re-run cloud-count-3 as the
deploy-window verify; close the RDR. No changesets.

### Delivery

A tag gates delivery, not work: every phase above lands on `develop`, fully
tested against a `scripts/build-gate-jar.sh` dev jar, without waiting for any
tag. One engine cut carries the whole arc, taken after P7 (or after P6 if P7
lands docs-only), via the `engine-release` skill's pre-tag battery: full
engine suite green on the tagged commit, `--shakeout`, and the mandatory
`--candidate-migration` rehearsal leg, which every db/changelog cut now
requires. The client release that pairs with it bumps
`REQUIRED_ENGINE_VERSION` to that tag in the SAME release, because D5's
client halves (the `memory_put` signature change and the `ttl_days`
predicate) are inert or wrong against any earlier engine. Per the
paired-release choreography the engine tag is cut FIRST, the client release
gates its battery against it, and the deploy relay fires at client-tag push
in parallel with the PyPI publish, so there is no refusal window and no inert
window. `scripts/check_engine_release_floor.py --paired-deploy
engine-service-vX.Y.Z` is the pre-tag gate; the same script without the flag
is the post-tag verify.

An interim cut after P3 is permissible if the arc runs long, at the cost of a
second paired client release. The default is one.

## Research Findings

P0 census executed 2026-08-15 (`develop` ~c41f9e61e) via
`scripts/sql/fk_census.sql` against the pytest engine substrate's fresh
Liquibase-applied schema; verified by `tests/db/test_fk_census.py` (7/7
green, includes 4 ground truths pinned independently of the census
script's own logic). Full evidence, code citations, and per-Q1-Q6
answer-facts are in `## Constraints and Verified Facts` above (kept there
per that section's own citation discipline); this section is the index.

1. **[VERIFIED]** `fk_catalog_chunks_chunk` is `fk_enforced`+VALIDATED, the census's own positive control passed.
2. **[VERIFIED]** Headline: 136 census rows (99 in-scope, 37 staging-exempt);
   39 `fk_enforced`, 59 `no_plausible_target`, 12 `fk_able_now` (all 12 are
   generic-`id` heuristic noise, zero genuine new single-column FK-able
   edges exist in the current schema by this method alone).
3. **[VERIFIED, limitation, not a schema fact]** The name-equality
   heuristic cannot see `doc_id -> tumbler` or `chunk_id/chash -> chash`
   shaped candidates (different column names); every Problem-Statement-
   named unenforced column falls into `no_plausible_target` for this
   reason, not because no relationship exists. Cross-checked directly
   against `pg_constraint` for all four ground truths plus every Q1-Q6
   column discussed.
4. **[VERIFIED, Q1]** `topic_assignments.doc_id` is a chunk chash by
   deliberate design; an FK to `catalog_documents(tumbler)`
   (`fk_ta_catalog_doc`) was added and then DROPPED (nexus-sa14p) because
   it assumed the wrong identity space. Column comment is stale.
5. **[VERIFIED, Q2]** `deleteCollectionTxn` (`CatalogRepository.java:5957`)
   has no `catalog_links` cleanup step; the sibling `topic_assignments`
   case (no doc-rooted FK) already gets an explicit collection-scoped
   DELETE in the same method, establishing the pattern `catalog_links`
   lacks. Live dangling count (277, nexus-ysrwi 2026-07-25) is 3 weeks
   stale, CLOUD COUNT NEEDED before Decision (query in Constraints
   section).
6. **[VERIFIED, Q3]** `chash_remap` is a live, actively-served rekey
   membership index (`RemapRepository`/`RemapHandler`/`ChashCensus`), not
   a retired migration leg, the "retire if converged" framing in the T2
   inventory does not hold. CLOUD COUNT NEEDED for the 32-vs-64-char
   split before deciding on a bytea conversion.
7. **[VERIFIED, Q4]** 9 tables have neither `tenant_id` in the PK nor any
   `pg_constraint`-visible tenant-scoped UNIQUE (RLS-only):
   `migration_jobs`, `nx_answer_runs`, `gc_audit`, `hook_failures`,
   `relevance_log`, `tier_writes`, `topics`, `aspect_promotion_log`,
   `claude_assisted_remediation_consents`. `migration_jobs` has a
   tenant-scoped PARTIAL unique index not visible to `pg_constraint`
   (second census limitation, documented in Constraints).
8. **[VERIFIED, Q5]** `frecency.ttl_days` (`0`=permanent) and
   `memory.ttl` (`NULL`=permanent, `0`=expire-on-next-sweep) have
   opposite null-semantics at the STORE level; the MCP tool layer papers
   over `memory`'s trap for one caller only. `plans.ttl`'s write path is
   UNVERIFIED, flagged, not traced, in this pass.
9. **[VERIFIED, Q6]** Zero `NOT VALID` FK constraints exist in the current
   schema baseline, `fk-002-validate.xml`/`fk-003-validate.xml` already
   validated everything from the fk-002/003 families. Q6 as posed against
   the CURRENT baseline is already answered; re-verify against the live
   cloud install before Decision (CLOUD COUNT NEEDED, query in
   Constraints) since a local fresh-Liquibase run proves changelog
   consistency, not that the cloud install is caught up to HEAD.
10. **[VERIFIED]** `hook_failures.doc_id` is unconstrained with no in-tree
    comment stating its identity space (unlike `topic_assignments`), flagged for its own investigation before assuming Q1's answer
    generalizes to it.
