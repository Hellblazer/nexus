---
title: "Cloud-Mode nx backup: an Engine Tenant Export Streamed to One File, with a Tested Restore into an Empty Tenant"
id: RDR-248
type: Feature
status: draft
priority: high
author: Claude (linda, for Sam)
reviewed-by: self
created: 2026-11-11
accepted_date:
related_issues: [nexus-f3yqd.4]
related_rdrs: [RDR-108, RDR-155, RDR-243]
---

# RDR-248: Cloud-Mode nx backup: an Engine Tenant Export Streamed to One File, with a Tested Restore into an Empty Tenant

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

## Problem Statement

Sam, 2026-11-11: "We need to have nx-backup work for the cloud mode, also, too." Asked where a
cloud backup should be made, he chose an export made by the engine for one tenant over a bundle the
client assembles from the existing per-store exports.

`nx backup` (nexus-f3yqd.4) dumps the LOCAL engine's Postgres with the bundled `pg_dump`, connected as
the cluster's bootstrap superuser. A cloud install has neither: its client holds a tenant token and
talks to the managed engine over HTTP, so `nx backup` refuses with "cloud backups are on the conexus
side, not something you run here". A cloud user therefore has no copy of their own data that they hold.

Terms. A **tenant** is one account's slice of the shared cloud database; every tenant-owned table carries
row-level security (RLS), a Postgres rule that shows a session only the rows of the tenant named in the
`nexus.tenant` setting. **T2** is the notes-and-state tier (memory, plans, taxonomy and the rest), **T3**
the vector tier (`nexus.chunks`), and the **catalog** the document graph (`catalog_documents`, links,
manifests).

### Enumerated gaps to close

#### Gap 1: A cloud user cannot back up at all

`nx backup` refuses managed mode before reading anything, and nothing else writes a complete copy of a
cloud tenant to the user's disk.

#### Gap 2: The exports that do work in cloud mode are partial and not one snapshot

`nx store export --all` writes T3 collections with their embeddings; `nx catalog export` writes catalog
links and `store_put` notes, without embeddings. Nothing exports T2, document aspects, highlights, tuples
or the full catalog document rows. Run one after another they read different moments, so a write between
them leaves the copies disagreeing.

#### Gap 3: No restore path exists for a cloud tenant, so a backup would be unproven

The local runbook restores with `pg_restore` into a fresh database. A cloud user cannot reach the cloud
database, so a cloud backup is only worth something if the engine can load it, and a test proves the
round trip.

## Relationship to Prior RDRs

- **Origin: nexus-f3yqd.4 (`nx backup`, local).** Its rules carry over unchanged: no automatic backup,
  ever (Sam, 2026-11-10); `nx backup` is the only caller (`tests/test_nx_backup_pin.py`); a partial file
  is never left behind; restore never overwrites live data. This RDR adds the cloud arm and does not
  change the local one.
- **Precedent: RDR-108 (catalog/T3 split).** Chunks are content-addressed and document structure lives in
  the catalog manifest. An export must carry both sides or the restored tenant has chunks with no
  documents, or documents with no chunks.
- **Adjacent: RDR-243 (document sensitivity).** A sensitive document is the user's own data and is in the
  backup. The file is written owner-read-only and the verb says the file holds sensitive documents.
- **Adjacent: RDR-155 (engine everywhere).** One engine code path serves local and cloud, so the export
  route also works against a local engine. That is how the round trip is tested without the cloud.

## Context

### Background

The local `nx backup` shipped in 8.1.x after the 2026-11-10 incident (nexus-f3yqd) in which an upgrade
rewrote user data at first boot. Cloud users were left out because the client cannot reach the cloud
database. The tenant `nexus` holds about 2.4 GB and 86k document rows (T2 critique on nexus-f3yqd.5).

### Technical Environment

- Engine: Java 25, Postgres 17 with pgvector; cloud is a self-managed PG 17.11 on arm64 EC2 behind the
  managed service. Schema by Liquibase.
- RLS: at the schema head (Liquibase `grants-nexus-diag-5`, 572 changesets) 50 parent tables carry
  `FORCE ROW LEVEL SECURITY`, keyed on `current_setting('nexus.tenant')` (`t1.scratch` on a second
  setting, `nexus.t1_tenant`), plus 40 RLS partition leaves of `nexus.chunks`, which is LIST-partitioned by
  model, then tenant. The global tables outside RLS: `embedding_models`, `install_pings`,
  `request_ledger_tenants`, `service_tokens`, `session_tokens`, `tuple_tenants`, and Liquibase's own.
  (A first count from the changesets said 60; it included tables since dropped.)
- The engine connects as `nexus_svc`: not superuser, no BYPASSRLS, not the tables' owner. It has SELECT
  and INSERT on all 50 tables and TEMP on the database, USAGE but not UPDATE on their sequences, and it
  cannot change `session_replication_role`, so it cannot switch triggers off.
- `CatalogRepository.COLLECTION_SCOPED_TABLES` plus a parity test against the live schema already keep
  one table list honest (nexus-20890); the export uses the same pattern.

## Research Findings

### Investigation

Read for this draft: `src/nexus/backup/pg_backup.py`, `src/nexus/commands/backup_cmd.py`, the Liquibase
changesets under `service/src/main/resources` (RLS and CREATE TABLE statements), `nx store export --help`,
`nx catalog export --help`, `tests/catalog/test_collection_scoped_tables_schema_parity.py`.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Postgres `COPY` under RLS | No (docs only) | `COPY table TO` is refused on an RLS table; `COPY (SELECT ...) TO STDOUT` applies the policies. |
| pgvector binary I/O | No | `vector` has binary send/receive, so binary COPY should round-trip. To verify (A3). |
| Cloud ingress | No | Whether a multi-minute, multi-GB streamed response survives the managed service's front end is unknown (A1). |

### Key Discoveries

- **Verified (source reading):** `nx backup` refuses managed mode first (`pg_backup.resolve_target`).
- **Verified (source reading):** the tenant is selected by the `nexus.tenant` setting in every RLS policy.
- **Verified (source reading):** credentials live only in global tables, so a tenant export cannot carry a
  token by construction.
- **Verified (local spike, 2026-11-11, T2 `nexus/rdr248-local-spike-2026-11-11`):** on chas, with two
  seeded tenants (catalog, 57 chunks with bge-768 vectors, T2, aspects, highlights, tuples, a sensitive
  document), as `nexus_svc` in one REPEATABLE READ READ ONLY transaction:
  - the tenant's visible rows equal its true rows on every populated table, and the other tenant's rows
    are invisible on all 50 tables (and absent byte-for-byte from the frames); a write committed during
    the export is not in it;
  - binary `COPY (SELECT ...) TO STDOUT` round-trips bigint, boolean, bytea, double precision, integer,
    integer[], jsonb, `vector(768)`, numeric, text, timestamptz and generated tsvector;
  - `COPY ... FROM` into any RLS table is refused ("COPY FROM not supported with row-level security"), so
    a load stages each frame in a temp table and inserts from it;
  - 21 tables key on a global serial or hashed id without the tenant, so a restore into a second tenant
    in the same database collides unless ids are reassigned; reassigning through per-table maps, with
    foreign-key children rewritten and tables loaded in foreign-key order, loaded A into an empty C with
    counts and checksums equal (outside the reassigned columns and one trigger-maintained column);
  - tuple ids hash the tenant, so tuples cannot move to another tenant by copying;
  - the seeded tenant exported 312 KB in 0.04 s, 285 KB of it chunks: size follows chunk count, about
    5 KB per bge-768 chunk.
- **Not exercised by the spike:** 29 tables that got no rows, and the types bigint[], smallint, uuid and
  the one identity column (`pdf_pipeline.pipeline_id`); all but two of those tables are classed
  `excluded` below.

### Critical Assumptions

| # | Assumption | Status | Method |
| --- | --- | --- | --- |
| A1 | The cloud front end lets one authenticated response stream for tens of minutes and several GB | Unverified | Spike against the cloud in an announced window (bob, on Sam's go) |
| A2 | One `REPEATABLE READ READ ONLY` transaction with `set_config('nexus.tenant', T, true)` returns exactly that tenant's rows from every RLS table | Verified locally (spike); under the managed pooler unverified | Cloud spike for the pooler half |
| A3 | Binary `COPY` of the column types in these tables round-trips | Verified for every type that held data; staged load required | Local spike; the remaining types (bigint[], smallint, uuid, identity) by the Step 5 test's seed |
| A4 | Import loads in one transaction | Verified with id reassignment, foreign-key order and tuples excluded | Local spike |
| A5 | A read-only snapshot held for the export does not hurt the shared cloud database | Unverified | Cloud spike: time and size for the `nexus` tenant |

## Proposed Solution

### Approach

The engine writes the backup, because only the engine can read a tenant completely and consistently. One
read-only transaction, scoped to the caller's tenant by RLS, copies every tenant-owned table into one
framed stream. `nx backup` in cloud mode saves that stream to one file, checks it, and says how to keep
it. A second route loads such a file into an EMPTY tenant, so a restore can never overwrite data. The
same routes run against a local engine, which is where the round trip is tested.

### Technical Design

**The table registry** (`TenantExport.TABLES`, engine). One entry per RLS parent table, built and checked
against the live catalog (`relrowsecurity AND NOT relispartition`), never a hand count. Each entry has:
- a **class**: `data` (user data, always exported), `derived` (rebuildable, exported because it is cheap
  and saves a rebuild) or `excluded` (operational state, with a reason);
- a **load order**: the topological order of the foreign keys (there are no cycles today, and only one
  foreign key is deferrable, so deferring constraints does not replace ordering);
- its **reassigned id**, if its primary key is a global serial (18 tables), and the **columns that refer
  to it**, both foreign keys (`nx_answer_steps.run_id`, `topic_assignments.topic_id`,
  `topic_links.from_topic_id` and `to_topic_id`, `topics.parent_id`) and references with no foreign key
  (`nx_answer_runs.plan_id` to `plans.id`, `taxonomy_centroids.topic_id` to `topics.id`);
- the **columns a trigger maintains** (`topics.updated_at` and its doc count, restamped when assignments
  load), which verification skips.
A parity test fails when a live RLS table has no entry, when a foreign key or serial primary key is not
reflected in the entry, or when an entry names a column that no longer exists.

The classes, from the spike (one reason each in the spike record):
- `data`: catalog_owners, catalog_collections, catalog_documents, catalog_document_chunks, catalog_links,
  catalog_meta, catalog_sensitivity_changes, chunks, embedding_profile, memory, memory_summaries, plans,
  document_aspects, document_highlights, topics, topic_assignments, topic_links, taxonomy_meta,
  claude_assisted_remediation_consents.
- `derived`: frecency, taxonomy_centroids, search_telemetry, relevance_log, nx_answer_runs,
  nx_answer_steps, chunk_orphaned_at.
- `excluded`: in-flight work (pdf_pipeline, pdf_pages, pdf_chunks, reembed_jobs, reembed_job_moves),
  coordination state whose ids hash the tenant and whose claims would be stale (tuples, tuple_claim_log,
  tuple_deliveries), session scratch (t1.scratch), and operational logs (agent_dispatches,
  aspect_promotion_log, canary_query_ring, capability_census, catalog_reconcile_runs and _rows,
  chash_remap, gc_audit, hook_failures, index_failures, ladder_completions, request_ledger,
  retention_markers, routing_events, tier_writes).

**Export route.** `GET /v1/tenant/export`, authorized by the caller's own tenant token (no admin token, no
other tenant). In one `REPEATABLE READ READ ONLY` transaction as the engine role, with
`set_config('nexus.tenant', T, true)`:
1. a header: format version, engine version, the last Liquibase changeset id, tenant id, UTC time, the
   table list with each table's columns and types, and the `embedding_models` rows the tenant's rows
   refer to (a global table, outside the export, that two exported tables reference by foreign key);
2. per `data` and `derived` table, in load order: a frame (table name, row count) holding
   `COPY (SELECT <non-generated columns> FROM <table>) TO STDOUT (FORMAT binary)`;
3. a trailer: per-table row counts and a SHA-256 of each frame.
The body streams with constant engine memory.

**Client (`nx backup`, cloud arm).** In managed mode `resolve_target` no longer refuses. The verb streams
the route to `~/nexus-backups/nexus-<tenant>-<UTC stamp>.nxtenant.partial`, mode 0600, verifies the
framing and every checksum and count against the trailer, then renames. It prints size, time, row counts
by class, `ONLY_COPY_SENTENCE`, and that the file holds sensitive documents when any are present. The
local arm is unchanged. A client older than the route's engine version refuses with the version it needs.

**Import route.** `POST /v1/tenant/import`, for the caller's own tenant, which must exist through tenant
create (that is what makes its `chunks` partition leaves; rows are never how a tenant comes to exist). It
refuses unless every `data` and `derived` table is empty for that tenant, ignoring the rows tenant create
itself writes; unless the header's changeset id equals the engine's (a mismatch names both); and unless
every `embedding_models` row in the header exists. Then, in one transaction, table by table in load order:
1. stage the frame: a temp table shaped like the target, `COPY ... FROM STDIN (FORMAT binary)` into it
   (COPY into the RLS table itself is refused);
2. for a table with a reassigned id, build a temp map of old id to a new id from the table's own sequence;
3. `INSERT ... SELECT` from the stage with the caller's tenant in the tenant column, new ids from the map,
   and referring columns, foreign key or not, rewritten through their parent's map.
It then checks each table's row count against the trailer, and a checksum over the columns that neither
the reassignment nor a trigger touches. Any failure rolls the whole load back. Reassigning ids always,
not only on a collision, also leaves the sequences correct: the engine role cannot `setval`, and a load
that kept the old ids made the engine's next insert collide.

**Restore verb.** `nx backup restore FILE` (cloud and local) calls the import route. The local `pg_restore`
runbook stays for whole-cluster restores. An empty tenant comes from tenant create: the operator on the
cloud (conexus), `nx init` on a fresh local install. The runbook says how.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Table registry + parity test | `COLLECTION_SCOPED_TABLES` and its parity test | Reuse the pattern, new list |
| Partial-file publish, verify, interrupt handling | `pg_backup._publish`, `_interrupts_held` | Reuse |
| Destination, size warning, step report | `default_destination`, `free_space_warning`, `StepReporter` | Reuse |
| Tenant-scoped session | the engine's RLS session setup | Reuse |
| `nx store export`, `nx catalog export` | per-store exports | Keep; not replaced |

### Decision Rationale

Sam chose the engine export (2026-11-11). It is the only design that is complete (every RLS table, by a
tested registry) and consistent (one snapshot), and it gives the cloud a restore path that the user can
run. RLS confines it to the caller's tenant without any new authorization code.

## Alternatives Considered

### Alternative 1: Client-side bundle of the existing exports

**Description**: `nx backup` runs `nx store export --all`, `nx catalog export` and a new T2 export, and
packs them into one archive.

**Pros**: client-only; fastest to ship.

**Cons**: not one snapshot; misses aspects, highlights, tuples and full catalog rows until each gets an
exporter; every new table needs a new exporter.

**Reason for rejection**: Sam chose the engine export (2026-11-11).

### Briefly Rejected

- **Operator-run `pg_dump` per tenant on the conexus side**: the user cannot run it or hold the result,
  and the superuser connection bypasses RLS, so separating one tenant is hand work.
- **Logical replication to the user**: far more machinery than a backup needs.

## Trade-offs

### Consequences

- A new engine route pair and a file format that must stay readable across engine versions (the header's
  format version and changeset id).
- The cloud holds one read-only transaction per running export.

### Risks and Mitigations

- **Risk**: a new reference to a reassigned id with no foreign key (like `nx_answer_runs.plan_id`) is
  missed and silently points at the wrong row after a restore.
  **Mitigation**: the registry lists those references; a reviewer adding such a column must add it, and
  the round-trip test seeds every reassigned table with children so a broken join shows as a count
  mismatch.
- **Risk**: a trigger added later changes rows during import, and verification reports a false mismatch.
  **Mitigation**: the registry names trigger-maintained columns, and the parity test lists the tables'
  triggers so a new one is a reviewed change.

- **Risk**: an export left running holds back vacuum on the shared database (A5).
  **Mitigation**: a statement timeout on the export transaction sized from the cloud spike, and one export
  per tenant at a time.
- **Risk**: a schema change between export and import makes a file unloadable.
  **Mitigation**: import refuses on a changeset mismatch and names both; the runbook says to restore with
  the engine version that wrote the file, then upgrade.

### Failure Modes

- Stream cut mid-export: the client keeps only `.partial`, deletes it, and says to run again.
- Import fails mid-load: one transaction, so the tenant stays empty; the error names the table.
- Registry out of date: the parity test fails in CI before release.

## Implementation Plan

### Prerequisites

- [ ] A1, A2 (cloud half) and A5 measured in an announced cloud window (bob, on Sam's go)
- [ ] A2 (local half), A3, A4 verified by a local spike

### Minimum Viable Validation

Done locally for the method (the spike). The MVV is the Step 5 test doing the same against the shipped
routes, in-process with the test fixtures (a subprocess CLI from a checkout is refused by the
production-write guard even against a loopback engine), seeding every `data` and `derived` table,
including the types the spike did not exercise; then one export and import against a cloud test tenant
in a window, with time and size recorded.

### Phase 1: Code Implementation

#### Step 1: Table registry and parity test (engine): class, load order, reassigned ids and their references, trigger-maintained columns

#### Step 2: Export route, streamed, with header and trailer (engine)

#### Step 3: `nx backup` cloud arm: stream, verify, publish (client)

#### Step 4: Import route (staged load, id reassignment, foreign-key order, verification) and `nx backup restore` (engine + client)

#### Step 5: The round-trip test (local engine, two tenants) and the runbook

### Phase 2: Operational Activation

#### Activation Step 1: Engine release carrying the routes, then the cloud deploy (Sam's go)

#### Activation Step 2: A cloud round trip on a test tenant in an announced window (bob)

### Day 2 Operations

| Resource | List | Info | Verify | Delete | Rename |
| --- | --- | --- | --- | --- | --- |
| Backup file | `ls ~/nexus-backups` | header print in `nx backup restore --check` | trailer checksums | user deletes | n/a |

### New Dependencies

None.

## Test Plan

- **Scenario**: export then import into an empty tenant — **Verify**: per-table counts and checksums equal.
- **Scenario**: two tenants — **Verify**: the export holds none of the other tenant's rows.
- **Scenario**: import into a non-empty tenant — **Verify**: refused, nothing written.
- **Scenario**: changeset mismatch — **Verify**: refused, both ids named.
- **Scenario**: stream cut — **Verify**: no file left but nothing at the destination.
- **Scenario**: an RLS table added without a class — **Verify**: the parity test fails.
- **Scenario**: import into a second tenant in the same database — **Verify**: no key collision; foreign
  keys and the two unenforced references resolve to the new rows; the next engine insert after the load
  succeeds.
- **Scenario**: the target lacks an `embedding_models` row the file needs — **Verify**: refused before
  any write, naming the model.

## Validation

### Testing Strategy

Engine-substrate integration tests drive both routes against a local engine (no cloud). The cloud is
exercised once per release in an announced window.

### Performance Expectations

To be set from the cloud spike: export rate in MB/s and total time for the `nexus` tenant (about 2.4 GB).

## Finalization Gate

### Contradiction Check

To be completed at gate.
