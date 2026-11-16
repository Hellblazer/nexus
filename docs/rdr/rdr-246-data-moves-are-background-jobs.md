---
title: "Data Moves Run as Batched, Resumable Background Jobs, Never at Engine Boot"
id: RDR-246
type: Architecture
status: accepted
priority: high
author: linda (session), for Sam
reviewed-by: pending (code-review-expert and substantive-critic at the gate)
created: 2026-11-10
accepted_date: 2026-11-16
related_issues: [nexus-f3yqd, nexus-f3yqd.1, nexus-f3yqd.2, nexus-f3yqd.3]
---

# RDR-246: Data Moves Run as Batched, Resumable Background Jobs, Never at Engine Boot

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Two upgrades in a row rewrote user data on the first boot of the new engine:
the 8.0.0 repartition of the chunk tables and an index rebuild started by a
startup reconciler (index work, which nexus-f3yqd.3 covers; Research
Findings). Each took about 20 minutes of writes at about 65 MB/s.
During that time the engine did not serve, and nothing told the user what it was
doing or whether stopping it was safe. Sam, 2026-11-10: "christ. this is *bad*."

A *data move* here means any step that reads and writes existing rows in bulk:
a backfill of a new column, a copy into a new table layout, a rewrite of a
column's values, a mass delete. Schema changes that touch no existing rows (a
new empty table, a function, a grant) are not data moves.

### Enumerated gaps to close

#### Gap 1: Data moves run inside the boot-time schema walk

The engine applies its Liquibase changelog at startup (a *changeset* is one
migration step; the walk runs them in order before the engine serves). A
changeset that does row DML runs as one statement in one transaction, so its
cost grows with the user's data, it blocks the engine from serving until it
ends, and an interrupted walk restarts the whole statement. The inventory in
Research Findings lists every such site. This RDR closes the gap for new data
moves; the moves already shipped stay at boot (Research Findings, What this
means for the design).

#### Gap 2: No rule stops the next data move from landing at boot

Nothing in review, CI or the release process says a data move may not be a
boot step, and nothing measures what a boot writes. The second rewrite shipped
the release after the first. nexus-f3yqd.1 (wren) adds the measurement, an
upgrade-boot write gate in the engine release. This RDR adds the rule the gate
checks and the mechanism a data move uses instead.

#### Gap 3: A long move gives no progress and no safe-stop answer

A user watching a busy disk cannot tell whether the engine is migrating,
stuck, or broken, how far it has got, or whether a restart loses the work.
The background job reports its progress in chunks, says it is safe to stop, and
resumes from its last committed batch (the /v1/status `boot_work` shape that
nexus-f3yqd.3 defines).

## Relationship to Prior RDRs

Searched docs/rdr for repartition, boot-time rewrite, backfill, runAlways and
background jobs.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-225 (one vector table per embedding model) | Origin | Its changesets copy every chunk row into the per-model tables during the schema walk ("The schema walk runs at engine boot", its glossary). That is the first of the two rewrites. Its layout stands; only how the copy runs changes. |
| RDR-193 (server-side catalog reconciliation) | Precedent | It introduced EngineJobs, the engine's registry of background jobs with bounds, scope and a poll route. A data-move job is one more kind there, except that its state must survive a restart, which EngineJobs (memory only) does not give. |

## Context

### Background

See the Problem Statement. The epic is nexus-f3yqd; its three parts are the
write gate (f3yqd.1, wren), this RDR's rule and mechanism (f3yqd.2), and
heavy boot work announcing itself (f3yqd.3).

### Technical Environment

- The engine (Java, nexus-service) runs Liquibase at boot against PostgreSQL 17.
- Local mode: a portable PostgreSQL 17 bundle that nexus builds itself (PG, pgvector,
  contrib) for Linux x86_64 and arm64, macOS arm64 and Windows x64.
- Cloud: self-managed PostgreSQL 17 on nexus's own arm64 EC2 database host,
  where the engine runs too (bob, 2026-11-10; the earlier Crunchy Bridge
  cluster is idle and pending decommission). Extensions on it are installed
  by nexus's own host configuration.
- The largest table, nexus.chunks, is list-partitioned by embedding model.
  Its primary key is four columns, (tenant_id, collection, chash,
  embedding_model) (vectors-030-model-tenant-partition-functions.xml line 677),
  and chash is a 32-byte sha256. dml_utils supports a primary key of one to
  three columns (its primary_key_columns.sql and primary_key_attributes.sql
  refuse more), so a move over nexus.chunks needs dml_utils to take a fourth
  key column (Phase 1 Step 1).

## Research Findings

### Investigation

#### Inventory of boot-time data moves

A read-only census of all 218 changelogs and the Java boot path (T2
`nexus/f3yqd.2-boot-row-work-inventory-2026-11-10` holds the full table with
file:line for each site). **Verified** (source read):

- Every row move is a one-time changeset, and no changeset commits in batches:
  none sets `runInTransaction="false"` and none has a COMMIT. Each move is
  one statement or one DO block in one transaction.
- No runAlways changeset does row DML. The four runAlways changesets recreate a
  view, set reloptions, or grant.
- The moves whose cost grows with user data, worst first:
  1. `vectors-030-1` is RDR-225's repartition. It copies every chunk and
     centroid, updates the manifest in full, rebuilds every secondary index,
     then validates and analyzes. It first shipped in engine v0.1.150.
  2. `vectors-004-1` (unify, RDR-191) copies every chunk and rebuilds every index.
  3. `rdr180-3..7` rewrite the chunk tables, the manifest and chash_index
     (ALTER TYPE text to bytea).
  4. Scans and validations over chunks and the manifest follow:
     `hygiene-001-5`, `hygiene-001-8`, `catalog-029-1/-2`, `fk-004-1/-2`, `fk-002-*`.
  5. Rewrites of topic_assignments and catalog_documents:
     `taxonomy-011-1`, `catalog-015-0`, `catalog-017-0`, `catalog-031-1-documents-temporal` and others.
- An install that upgrades across several releases runs every one of these
  it has not yet run, in one boot.
- The PCI reconciler is not a row move. It builds an HNSW index per large
  collection, in the background after the engine serves, one `CREATE INDEX
  CONCURRENTLY` at a time (`PciReconciler`, `PciBuilderSession`).
  Its cost still grows with chunk volume. When this RDR was drafted, every boot
  ended any build in flight. Since ed34ea26e (2026-11-09) a boot ends builders
  only when the walk has a schema changeset to run or repairs partition-access
  drift (reason `partition_access_drift`) (`SchemaMigrator.migrate`,
  the `terminatePciBuilders` call with reason `schema_changesets_pending`,
  around line 568), so an upgrade boot with pending changesets still ends a
  build in flight and the reconciler rebuilds that index from scratch. Its first pass also
  re-counts every index, because its readings live in memory only.
- 8.0.0 paired with engine v0.1.184, and the changelogs added in
  v0.1.175..v0.1.184 do no full-table move. So the 8.0.0 boot rewrite was
  `vectors-030-1` (on an install still below v0.1.150) or the PCI builds. The
  repository alone cannot say which. On Sam's machine it is `vectors-030-1`
  (wren, 2026-11-10). Rows that fail the changeset's own checks make it roll
  back, so it never commits, and every boot starts the whole copy again. The
  hotfix (wren, separate from this RDR) is a changeset that runs before it and
  preserves those rows. This is the failure the rule prevents: a move inside
  the walk is one transaction, so a single bad row discards all its work, on
  every boot.
- Java boot work that already runs batched and in the background:
  AspectQueueDrain, ChunkReaper, the T1, tuple and request-ledger sweeps, and ReembedRunner.

What this means for the design:

- Shipped changesets are not converted here. Liquibase checksums every
  changeset, and an install that already ran one fails validation if its text
  changes unless the changeset lists the old checksum in a `validCheckSum`
  element, standard Liquibase. This repo uses it once, on a runAlways
  changeset whose stored checksum the boot does not check
  (grants-nexus-svc.xml lines 305-320, "insurance, not the fix"), so there is
  no in-repo precedent for editing a one-time changeset. Converting a
  shipped move would need that edit, a registration of the move, and tests
  that the result is the same from every install state it could start in. That
  is a scope decision, not an impossibility: the rule governs new changesets.
- The old moves still bite an install that skips releases. That cost is named
  under Trade-offs and is not removed here.
- The PCI builds are index work. Making them announce themselves and survive
  a restart is nexus-f3yqd.3's scope. This RDR records the restart-from-scratch
  finding for it.

#### dml_utils

Sam's library (git/dml_utils, fork of eyupmiduck/dml_utils, packaged as
Liquibase SQL) rewrites a large table in fixed-row chunks cut on primary-key
ranges:

- `dml_utils.run_migration_chunks(sql_text, schema, table, label, chunk_size,
  threads)` takes a DML template with `<driving_table>` and `<chunking_clause>`
  placeholders. The first call for a label computes boundaries over
  `ORDER BY <pk>`. Each chunk runs in its own pg_background worker (an
  autonomous transaction in a separate backend), so each chunk commits on its
  own and a re-run with the same label resumes at the first unprocessed chunk.
  **Documented** (README, "Processing a table in chunks").
- Progress is in dml_utils_data.migration_run and migration_boundary, read
  through `dml_utils.migration_run_summary`. **Documented.**
- Why pg_background and not a procedure that commits between chunks: one
  backend's dead tuples cannot be reclaimed until its top-level call returns,
  so a procedure bloats the table by the whole migration. One worker per chunk
  bounds the bloat to one chunk. **Documented** (README, "Why pg_background").
  **Measured 2026-11-16, and it does not hold as called** (T2
  nexus_rdr/246-research-3): `run_migration_chunks` is one statement, and its
  own snapshot holds the vacuum cutoff for the whole move. A VACUUM mid-move
  removed none of 7,000 dead rows ("removable cutoff: 1692", the coordinator's
  backend_xmin); after the call returned it removed 20,004. A procedure
  coordinator that COMMITs after each chunk launch held no xmin, and a mid-run
  VACUUM left no dead row unremovable. Sam chose, 2026-11-16 (ask
  dml-utils-coordinator): "Procedure entry in dml_utils (Recommended)", a
  committing PROCEDURE entry point added to dml_utils upstream.
- `dml_utils_lib.process_migration_chunk(run_id, boundary_no, sql_text)` claims one
  boundary with `UPDATE ... RETURNING` and runs its SQL in the caller's
  transaction. Only the launcher needs pg_background. **Verified** (source
  read, functions/dml_utils_lib/process_migration_chunk.sql).
- Supported key column types: smallint, integer, bigint, text, uuid,
  timestamptz and, since Hellblazer/dml_utils PR #1 (merged a6cd544,
  2026-11-10), bytea, for a primary key of up to three columns (README lines
  102-105). Before that merge bytea was not supported. **Verified** (source
  read, T2 nexus_rdr/246-research-4).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| dml_utils (Hellblazer/dml_utils, 28b0b3aa9, PR #2 merged on PR #1's a6cd544) | Yes | Launcher needs pg_background; per-chunk routine does not; bytea keys supported; `run_migration_chunks_committing` is the committing procedure |
| pg_background (vibhorkum/pg_background) | Yes, v2.0.4 (tag and pinned commit) | Runs SQL in a background worker with its own transaction. No shared_preload_libraries needed (verified on four platforms). Calls POSIX random() (one site, polling jitter). PostgreSQL License |
| nexus PG bundle (scripts/build_pg_bundle.sh, build_pg_bundle_windows.py) | Yes | Before this RDR: PG, pgvector and contrib, with no pg_background. With Phase 1 Step 2, pg_background v2.0.4 is in every bundle, and the contrib checks fail closed without it |

### Key Discoveries

- **Verified**: before this RDR, no local bundle or nexus changeset provided pg_background. Phase 1 Step 2 adds it to the bundle.
- **Verified** (bundle code review): the engine substrate in CI downloads the pinned *released* bundle, and the Java suite boots the pgvector/pgvector:pg17 image. The downloaded bundle has pg_background from engine-service-v0.1.194's bundles on; the pgvector/pgvector:pg17 image still has none. No changeset runs `CREATE EXTENSION pg_background`: the superuser bootstrap creates it (Technical Design, Install sequence), and Java tests of a data move need an image with the extension.
- **Documented**: the cloud host is self-managed, so installing pg_background
  there is a nexus host-configuration step with no vendor allow-list (bob, 2026-11-10).
- **Verified**: before PR #1 (a6cd544), dml_utils could not chunk on a bytea key; that merge added it.

### Critical Assumptions

- [x] pg_background builds and loads in the relocatable bundle on all four
  platforms, Windows included — **Status**: Verified 2026-11-10. pg_background
  v2.0.4 is pinned to its release commit (in both bundle builders) and was built for Linux x86_64 and
  arm64 (manylinux_2_28), macOS arm64 and Windows x64. On each, a relocated
  bundle ran `CREATE EXTENSION` and a background worker that returned `SELECT
  1`, with no shared_preload_libraries. On Windows, v2.0.4 calls POSIX
  `random()`, which PG 17 does not provide there, so the build compiles with
  `/Drandom=rand` in place of patching the source. It is used only for
  polling jitter. Record: T2 nexus/pg_background-bundle-build-notes-nexus-f3yqd.2.
  — **Method**: Spike
- [x] dml_utils with bytea keys chunks a (text, text, bytea) key with each
  row in exactly one chunk, independent of bytea_output and
  standard_conforming_strings — **Status**: Verified in the upstream change
  (Hellblazer/dml_utils, feature/bytea-keys). Its tests cover edge keys
  0x00/0xff and prefixes of different lengths, resume, and both session
  settings. Boundary text is bare hex, and chunk literals are
  `pg_catalog.decode('<hex>','hex')`, a constant the planner folds. Merged as
  PR #1 (a6cd544). — **Method**: Spike
- [x] dml_utils works with a partitioned driving table (PK on the parent) — **Status**:
  Verified for bytea, non-partition-wise and partition-wise (dml_utils
  supports partition-wise runs since 1.16), in the same change. Repeated on a
  FORCE-RLS, list-partitioned (text, text, bytea) table as the engine's role,
  partition-wise and over the parent, with resume after a killed coordinator:
  every row processed exactly once (T2 nexus_rdr/246-research-2) — **Method**: Spike
- [ ] A plain run on one `nexus.chunks` leaf, CALLed under a per-tenant session as `nexus_admin` on FORCE RLS with the per-leaf grants, processes every row of the leaf exactly once and resumes after a killed coordinator — **Status**: Unverified; the partitioned spike above ran over the parent. — **Method**: Spike, in the DataMoveWorker tests (Step 4)
- [x] dml_utils needs Sam's ddl_utils installed first, and its install role needs
  ddl_utils_caller: the bytea change adds the first changeset that calls
  ddl_utils' constraint helpers, behind a HALT precondition — **Status**:
  Verified 2026-11-16 (T2 nexus_rdr/246-research-2): ddl_utils' changelog then
  dml_utils' applied as a non-superuser `dml_utils_owner` once pg_background,
  `ddl_utils_caller` and `dml_utils_caller` existed and `dml_utils_owner` held
  CREATE on the database, `ddl_utils_caller` and `pgbackground_role`. The owner
  role's name is fixed by the library (its default-privilege changeset names
  `dml_utils_owner`). Applied with psql in Liquibase order, not through
  Liquibase — **Method**: Spike
- [ ] dml_utils, once it takes a four-column primary key, drives
  nexus.chunks itself: four key columns, and two levels of list partitioning
  (embedding model, then tenant, vectors-030 lines 530 and 675) — **Status**:
  Unverified; the spikes used a three-column, one-level stand-in —
  **Method**: Spike with the four-column change (bead nexus-f3yqd.2.21)
- [x] Liquibase applies the vendored ddl_utils and dml_utils changelogs as
  `nexus_admin` with the session role `dml_utils_owner` — **Status**:
  Verified 2026-11-16 (T2 nexus_rdr/246-research-7): Liquibase 4.29.0, the
  engine's version, on one connection as `nexus_admin` ran `SET ROLE
  dml_utils_owner` as a statement, applied ddl_utils (77 changesets) then
  dml_utils (132, the committing-entry-point head) and then, after `SET ROLE
  NONE`, nexus's master changelog (vectors-029 EXECUTED). Every object in
  the library schemas and the library tracking tables in schema
  `liquibase` are owned by `dml_utils_owner`; vectors-029's functions and
  its `chunks_gate_probe_owner_read` policy bind `nexus_admin`. One backend
  pid served every statement and the server log holds exactly one `SET ROLE`
  and one `SET ROLE NONE`. One constraint for Step 4: `Liquibase.close()`
  closes the JDBC connection, so the walks must not close the first
  `Liquibase` object before the last walk on that connection finishes —
  **Method**: Spike with real Liquibase in Phase 1 Step 3b
- [ ] The cloud host's PostgreSQL loads pg_background and has spare
  max_worker_processes — **Status**: Checked 2026-11-10, NOT available
  today (bob's read-only check on Sam's go, SSM command 60942485, T2
  conexus/pg-background-availability-prod-2026-11-10). The host runs
  PostgreSQL 17.11 (PGDG build for Ubuntu 24.04, aarch64). Neither the nexus
  nor the conexus database has a pg_available_extensions row for it, and
  there is no package and no control file. shared_preload_libraries is
  pg_stat_statements, pgaudit, pg_prewarm. max_worker_processes is 8,
  cluster-wide, and parallel query and pg_prewarm's autoprewarm share it.
  The host has no general egress, so the PGDG package must be baked into the
  db-host golden image or db-configure, followed by CREATE EXTENSION in each
  database. That is a conexus host change on Sam's go. Raising
  max_worker_processes needs a PostgreSQL restart. Sam ruled 2026-11-15 that
  the install waits until the first job that uses it lands, or until PGDG ships
  2.0.4 (recorded on nexus-f3yqd.2). — **Method**: Live
  read-only check
- [x] `CREATE EXTENSION pg_background` needs a superuser, because the
  extension is not marked trusted. Locally, the engine's bundle cluster is
  initialized with the engine's own superuser. On the cloud host, the host
  configuration runs it, not the engine's migration role — **Status**:
  Documented: the bundled `pg_background.control` (2.0) sets neither `trusted`
  nor `superuser = false`, so PostgreSQL requires a superuser. Its functions are
  granted to the NOLOGIN `pgbackground_role`, which the caller must hold
  (T2 nexus_rdr/246-research-1) — **Method**: Docs only
- [x] A pg_background worker sees the rows the engine's role sees under FORCE
  row-level security — **Status**: Verified 2026-11-16, and the premise was
  wrong (T2 nexus_rdr/246-research-1). pg_background 2.0 runs the worker as the
  caller's role and copies the caller's settings, the tenant included. Under the
  engine's policy a NOBYPASSRLS role with no tenant set sees no rows, so a move
  sees exactly one tenant per run: the run with tenant a touched only tenant a's
  rows, an UPDATE aimed at tenant b touched 0, and an INSERT of a tenant-b row
  was refused — **Method**: Spike

## Proposed Solution

### Approach

**The rule.** A changeset may change schema. It may not move rows whose count
grows with user data. A data move is a dml_utils run that the engine starts
after it serves, as a background job. Sam decided the mechanism on 2026-11-10
(ask dml-utils-shape, option 2): dml_utils whole, its pg_background launcher
included, with pg_background shipped in the local bundle on every platform
and installed on the cloud host, and bytea keys added to dml_utils upstream.

**What a move looks like:**

1. A changeset creates the new shape (empty table, nullable column) and
   registers the move: a
   row naming the label, the DML template, the driving table and the chunk
   size. Registration writes one row, whatever the data size.
2. After boot, the engine's move worker finds every open move (Technical
   Design, The registry) and CALLs dml_utils' committing procedure entry point for
   it. Each chunk commits on its own, the coordinator commits between chunks so
   VACUUM can reclaim each chunk's dead rows, and a restart resumes from the
   last committed batch.
3. While a move runs, /v1/status `boot_work` reports it in nexus-f3yqd.3's
   shape: done and total in chunks, `safe_to_stop: true`, `on_stop: "resumes
   from the last committed batch"`. nx doctor shows the same.
4. Code that reads the moved data copes with a move in progress (it reads
   both shapes, or the feature waits for the move's completion row). A
   changeset that needs the move finished first carries a precondition on
   the move's completion (Technical Design, Finishing a move). The gate
   refuses, by table name, a destructive statement on a moved table without
   that precondition; a dependency it cannot see by name (a new unique index
   on the new column, a view) is the review's to catch.
5. From the release that registers a move, every writer produces only the
   new shape. dml_utils fixes a run's chunk boundaries when the run starts
   (README lines 108-110: a row inserted later with a key above the captured
   maximum is outside the final chunk), so a row written later is visited only
   if its key falls in a chunk not yet run. A move's template therefore leaves
   a row already in the new shape unchanged, the move only has to cover rows
   that existed before that release, and its verification query proves no
   old-shape row is left.

### Technical Design

**dml_utils in the engine.** The engine carries ddl_utils' and dml_utils'
changelogs vendored under its resources at the commits Sam pinned (ddl_utils
5f088e2, and the dml_utils commit with the bytea keys and the committing
entry point), with a test that checks the copies against those commits. At
boot, before nexus's own changelog runs, it applies ddl_utils' changelog and
then dml_utils'. Each keeps its own tracking tables in the `liquibase`
schema, separate from nexus's. Installing them is schema only and costs the
same whatever the data size. dml_utils cannot be created without pg_background: dml_utils' plpgsql functions
declare variables of pg_background's types (`pg_background_handle`,
`pg_background_run_result`), and dml_utils' own test base installs the
extension first because the routines "declare its types, so the extension must
exist when Liquibase creates them" (PostgresTestBase.java). So the engine
applies the dml_utils and ddl_utils changelogs only when `pg_extension` holds
pg_background and the three library roles exist (Install sequence, below).
When they do not, the engine skips them, serves, and reports every registered
move as blocked; the next boot that finds them applies them. A database
without the extension therefore never fails a boot,
and the cloud install only has to land before the first registered move is
expected to run.

**The registry.** One table, `nexus.data_moves`, has one row per registered
move. In this section a *move worker* is the engine thread, `DataMoveWorker`;
a *background worker* is a pg_background process in the database.

- `label`, the primary key. The move worker names each dml_utils run
  `<label>/<tenant>`, `<label>/<tenant>/<leaf>` for a per-leaf run (The move
  worker, step 2), or `<label>` alone for a table with no tenant column;
  a CHECK (`label !~ '/'`) keeps one move's runs from being read as
  another's;
- the DML template;
- the driving schema and table (the table whose primary key dml_utils cuts
  into chunks);
- the chunk size and `sleep_ms`, the pause after each chunk (dml_utils'
  `i_sleep_ms`), which throttles the move against serving traffic. This is
  the value measured in the Minimum Viable Validation, written into the
  registering INSERT before the release; an engine setting,
  `NX_DATA_MOVE_SLEEP_MS`, overrides it per environment, and the move worker
  applies the override to a run that already exists with
  `dml_utils.set_migration_run_sleep_ms`, since dml_utils keeps the value it
  was created with;
- `statement_timeout_ms`, NOT NULL and positive, the bound on each
  verification run and on `on_complete`;
- the verification query, NOT NULL: a SELECT that returns the old-shape rows
  still present, written with one bind parameter, the tenant: the move worker
  binds a tenant id for a per-tenant run and NULL for the table-wide run, and
  the query filters on `tenant_id = $1 OR $1 IS NULL`. It never reads
  `nexus.tenant`, so the two runs differ only in the bound value. It runs in a
  READ ONLY transaction, so it cannot write;
- `on_complete`, NULL or the SQL that finishes the new shape once the move is
  verified (a VALIDATE CONSTRAINT, dropping the old column, swapping a table);
- the changeset id that registered it;
- `supersedes`, NULL or the label of an earlier move this one replaces, a
  foreign key to `label`, unique, so one move is replaced at most once;
- `registered_at`, `failed_at` and `failure` (the error text, or the
  verification count), and `completed_at`;
- `transient_retries` (NOT NULL, default 0), `last_transient_at` and
  `last_transient`: the count of consecutive transient passes, when the last
  one happened and its error (Retries, pause and cancelled runs, below);
- `paused_at` and `paused_by`: an operator's pause, set by SQL (Day 2
  Operations).

The chunk size is bounded to 1 to 1,000,000 rows and `sleep_ms` to 0 to
60,000 ms by CHECK constraints, and three more CHECKs keep the hand-set
columns coherent: `transient_retries >= 0`, `last_transient_at` and
`last_transient` both NULL or both set, `transient_retries` zero exactly when
`last_transient_at` is NULL (so the backoff always has a time and a count),
and `paused_by` set only when `paused_at` is; dml_utils stores the chunk size as an
integer (`dml_utils_data.migration_run.chunk_size`), so the column is one
too.

A changeset registers a move with one INSERT. A move is *open* while
`completed_at` and `failed_at` are NULL and no row's `supersedes` names it.
The move worker works only on open moves, and boot_work and nx doctor list
only open and failed moves that no row supersedes.

**The move worker.** `DataMoveWorker` is an engine background thread. It
starts after the HTTP listener and handles one open move at a time, oldest
`registered_at` first. Each pass over a move:

1. grants what the libraries need on the driving table (Roles and grants,
   below), then checks that dml_utils is installed (`to_regprocedure` of the
   committing entry point, not only the extension), that the admin connection
   reads every row of the driving table (AdminDdlSession's
   `requireUnrestrictedView`: superuser, BYPASSRLS, or a true-qual SELECT
   policy), and that one `max_worker_processes` slot is free. If either is missing, the move is
   `blocked:` with the reason, and the move worker goes on to the next open
   move and comes back after the move's backoff (Retries, pause and
   cancelled runs, below);
2. for each tenant, on a dedicated connection that no other code borrows,
   runs a session-level `SET nexus.tenant`, CALLs dml_utils' committing entry
   point (added upstream by this RDR; it takes `run_migration_chunks`'
   parameters, with threads 1 and the move's chunk size and `sleep_ms`), then
   runs `RESET nexus.tenant`. When the driving table is list-partitioned with
   one tenant per leaf, as `nexus.chunks` is (by model, then by tenant), the
   move worker never runs it partition-wise (`i_partition_wise` stays false):
   it CALLs once per leaf of the tenant, with that leaf as the driving table,
   a plain table chunked by its primary key like any non-partitioned driving
   table (so the four-column key prerequisite still applies), and names the
   run `<label>/<tenant>/<leaf>`. That is one run per leaf, tenants times
   models of them, since a new model partition gets a leaf for every tenant
   (vectors-030); an empty leaf's run has no chunks. The
   tenant's leaves are the children of each model partition of the table
   whose single-value bound is the tenant, read with
   `nexus.partition_bound_value` (vectors-030), the way
   `nexus.drop_tenant_partitions` enumerates a tenant's leaves: from the
   catalog, never from a scan or a name pattern. The move worker never runs a
   tenant's session against another tenant's leaf. Row-level security on the
   partitioned parent does not apply when a leaf is named directly, but each
   leaf carries its own copy of the parent's FORCE RLS and policies
   (`nexus.partition_copy_access`, vectors-030), and a leaf holds one
   tenant's rows, so the leaf and its policies both scope the run. A leaf
   created after registration holds only new-shape rows (Approach, step 5),
   so it needs no run; old-shape rows in one mean a writer still produces the
   old shape, and the verification fails the move, as for any such writer. A move over a table partitioned
   another way runs per tenant over the parent, not partition-wise. The connection is autocommit, so the coordinator
   holds no snapshot between chunks;
3. once every tenant's run is complete, and after reading the row again (a
   paused move stops here), runs the verification query for each
   tenant under that tenant's setting, then once table-wide on a separate
   admin connection after checking that `current_setting('nexus.tenant',
   true)` is empty there. If every run returns no rows, it reads the row once
   more (a pause set now still stops it) and runs `on_complete`
   (if any) and the UPDATE that sets `completed_at` in one transaction on the
   admin connection, so a stop between them leaves neither; `on_complete`
   holds DDL that PostgreSQL runs inside a transaction (VALIDATE CONSTRAINT,
   DROP COLUMN, renames), never CREATE INDEX CONCURRENTLY or another statement
   that cannot.

Failures are classified by what the move worker can read. *Transient*,
retried after the move's backoff with the move `blocked:`: no free slot; a
missing install or role; an admin connection that cannot see every row; a
verification run or `on_complete` that hits `statement_timeout_ms`, or an
`on_complete` that hits its `lock_timeout` (SQLSTATE 55P03); a lost connection
or a stopped engine;
a chunk error dml_utils recorded with SQLSTATE 40001 (serialization), 40P01
(deadlock), 55P03 (lock not available) or 57014 (statement cancelled), which
live writers cause, or in class 53 (insufficient resources) or 57P01, 57P02
or 57P03 (shutdown and startup), which a busy or restarting host causes; a
later pass of the same label clears these, because dml_utils re-claims every
boundary whose `completed_at` and `cancelled_at` are NULL
(`claim_migration_boundary`) and a failed chunk's boundary is neither
(README: the call "raises the error, leaving the run to be resumed"); and an error the
CALL raised that dml_utils did not record as a chunk error, with SQLSTATE in
class 08 (connection exception) or class 53 (insufficient resources), or
57P01, 57P02 or 57P03 (shutdown and startup), 40001, 40P01, 55P03, 57014,
42501 (insufficient_privilege: a grant not yet made) or 42883
(undefined_function: an install that vanished between the check and the
CALL). A recorded chunk error with any other SQLSTATE is permanent, 42501 and
42883 included, because there it is the template's own fault (a function it
names, or a table its role cannot write), and so is a recorded class 08
error, since a chunk records its own statement's error, not the
coordinator's connection. dml_utils records a chunk's error on a best-effort
basis (its recorder swallows its own failure), so a template error it failed
to record reads as raised; a 42501 or 42883 then retries as transient and
shows "needs an operator" after the cap rather than `failed:`. That path is
rare and stays visible. *Permanent*, which sets `failed_at`
and `failure`: an error with any other SQLSTATE (class 22 data, class 23
integrity, the rest of class 42, class 3F such as `assert_schema_exists`'s
3F000 for a missing driving schema, P0 PL/pgSQL, and anything else), a
non-empty verification, and an `on_complete` that fails for a reason other
than its statement or lock timeout. The move worker tells a recorded chunk
error from a raised one by the run's `migration_error` rows: it reads the
run's highest `error_id` before the CALL (0 when the run does not exist yet,
since the first CALL creates it), and a row above that mark is this pass's
chunk error, read with its own `sqlstate`. dml_utils never deletes
`migration_error` rows on resume (only `delete_archived_migration_runs`
does), so an older row says nothing about this pass, and an id mark, unlike a
clock, needs no agreement between the engine and the database. An error the move worker cannot place in either list is permanent,
so nothing loops. A failed move stays reported `failed:` across restarts, is
not retried, and does not hold up the moves behind it. The fix is a new move
that supersedes it (Changing a move). A non-empty verification means an
old-shape row exists: a writer that still produces the old shape, a template
that missed a case, or a row put back.

**Retries, pause and cancelled runs (nexus-f3yqd.2.3).** Transient failures
are counted and spaced, so a blocked move is retried at a bounded rate and
always reports how many times it has been retried:

- *Counter.* A transient pass increments `transient_retries` and sets
  `last_transient_at` and `last_transient` to the error. A pass that ends
  without a transient error sets all three back to 0 and NULL. `blocked:`
  shows "retry N, last <time>: <error>". The counter survives a restart,
  being a column. An operator can clear it by SQL (Day 2 Operations).
- *Cadence.* A blocked move is not retried before `last_transient_at` plus a
  backoff of min(30 s × 2^(min(N, 6)−1), 15 min), N being `transient_retries`
  (the exponent is clamped, since N keeps growing): 30 s, 1, 2, 4 and 8
  minutes, then 15 minutes for every later retry. It is computed from the
  row, so no timer state is kept.
- *Cap.* The worker never stops retrying a blocked move; past the cap it
  retries every 15 minutes indefinitely, so a move registered before its
  install or roles exist runs once they appear (the cloud order in
  Activation Step 1). After 20 consecutive transients, about four hours of
  backoff, `blocked:` adds "needs an operator". nx doctor shows every
  `blocked:` move red already; the cap only changes the text. The move is
  never made `failed:` for transient errors: a busy slot, a missing grant or
  a missing install is not the move's fault, and `failed:` would need a
  superseding move to clear.
- *Pause.* A move with `paused_at` set is open but not worked. The move
  worker reads the row before each pass, before each run's CALL, before the
  verification and again before `on_complete` (steps 2 and 3), so a pause
  takes effect at the next of those points: a run already in flight
  continues to its end, because one CALL of the committing entry point runs
  every chunk of that run. A move on a table with no tenant column has one run,
  so a pause stops it only before that run starts, before its verification
  or before `on_complete`. boot_work reports the move `paused:` with `paused_by`; nx
  doctor shows it as a note, not red, since an operator chose it. It resumes
  when `paused_at` is set back to NULL. To stop a run in flight sooner, an
  operator cancels the CALL's backend with `pg_cancel_backend`, using the pid
  the engine logs for each CALL (`event=data_move_call`). The CALL raises
  57014, a transient error that counts a retry; it does not set the run's
  `cancelled_at` and is not dml_utils' `cancel_migration_run`, so the move
  does not become `failed:`. Chunks already committed stay committed and the
  run resumes on the next pass after the pause is cleared. What the cancel
  does to a chunk worker already launched is not documented by dml_utils (its
  coordinator drains workers in a `WHEN OTHERS` handler, which does not catch
  `query_canceled`), and the DataMoveWorker tests (Step 4) establish it.
  `NX_DATA_MOVE_SLEEP_MS` throttles a move; it does not pause one, and the
  move worker clamps it to the registry's bounds (0 to 60,000 ms) before
  applying it.
- *Order.* While the oldest open move is paused or backing off, the move
  worker takes the next open move, oldest `registered_at` first and `label`
  as the tiebreak. A move that must follow another is registered by a
  changeset whose precondition waits on the earlier move's `completed_at`
  (Finishing a move, below); no rule of the gate covers this ordering, so the
  review checks it.
- *Interrupted runs.* A run left incomplete by an engine stop, a lost
  connection or a cancelled CALL resumes: the next pass calls the same label,
  and dml_utils continues at the first unprocessed chunk.
- *Changed partition tree.* A per-leaf run on `nexus.chunks` is not
  partition-wise, so dml_utils makes no tree check on it, and a new leaf for
  any tenant, this one or another, cannot disturb a run in progress (step 2).
  The move worker never runs a move partition-wise (step 2), so the tree
  check never applies. If a 55000 arrives anyway (dml_utils also raises it
  for other states, such as an unusable index), it is permanent like any
  55000, and a superseding move does the work. Nothing is archived and re-created, so no chunk runs twice
  and `done` never falls.
- *Cancelled runs.* Only a run dml_utils records as deliberately cancelled
  (`dml_utils_data.migration_run.cancelled_at` IS NOT NULL, from
  `cancel_migration_run`) makes the move `failed:`, with the failure "run
  cancelled at <time>". A CALL on a cancelled run returns without raising, so
  the move worker reads `cancelled_at` of the run's active row (`archived_at
  IS NULL`) after each CALL rather than taking the return as completion. As for any failure, a superseding move does the work
  again under a new label, from its first chunk.
- *Run-creation refusals* are permanent by SQLSTATE: a template without
  `<chunking_clause>` (22023, `assert_chunking_template`) and a key of more
  than three columns before dml_utils supports it (22023).


A stopped engine leaves committed chunks committed. The next boot's move
worker calls the same label, which resumes at the first unprocessed chunk.

**Row-level security.** The engine's tables use FORCE row-level security
(FORCE RLS: the policies bind the table's owner too) keyed on the
`nexus.tenant` setting, and the roles the engine reads and writes data as,
`nexus_svc` and `nexus_admin`, are NOBYPASSRLS (they do
not skip the policies). Writes are therefore always tenant-scoped: an UPDATE
or INSERT sees and changes only the rows of the tenant set on the session,
and so does `dml_utils_owner`'s boundary scan, which runs as a role with no
other policy. pg_background workers run as the caller and copy its settings
(T2 nexus_rdr/246-research-1). A move over a tenant-scoped table therefore
runs once per tenant, as in step 2 (on a table partitioned one tenant per
leaf, such as `nexus.chunks`, once per leaf of the tenant, under that
tenant's setting). A move over a table with no tenant column runs once. It does not run as nexus_svc.

Reads by `nexus_admin` differ by table. On `nexus.chunks`, vectors-029 gives
the migrating role a permissive SELECT policy, `chunks_gate_probe_owner_read`
(`USING (true)`), so `nexus_admin` already reads every tenant's chunk rows; a
per-tenant verification run there must therefore filter by `tenant_id`
itself (the bound tenant in the verification query does this). Any other
driving table has only the tenant policy (a census of CREATE POLICY in the
changelogs finds `tenant_isolation` everywhere and the owner-read policy only
on `nexus.chunks`), so the move adds the same owner-read SELECT policy for
the migrating role (`TO CURRENT_USER`, the vectors-029 pattern) in its own
changeset, ordered after the registering changeset. That changeset carries
vectors-029's guard: a precondition that skips it (MARK_RAN) when `nexus_svc`,
or any other login role subject to row-level security, is or inherits the
migrating role, and a post-condition that the policy names exactly the
migrating role. A Liquibase precondition skips its whole changeset, which is
why the policy is never in the registering changeset: there the guard would
also skip the new shape and the `nexus.data_moves` INSERT. In the single-role
posture (the migrating role is `nexus_svc`) the guard skips only the policy:
the move is registered, the service role never gains a cross-tenant read, the
admin connection cannot see every row, and the move is `blocked:` (step 1).
MARK_RAN is decided once, so an install that later moves to two roles keeps
the move blocked until a new changeset adds the policy. The same holds on
`nexus.chunks` if vectors-029 itself was skipped there. A membership granted
after the policy exists is caught at boot: `ChunksIsolationCheck`, which today
refuses boot when any permissive policy other than `tenant_isolation` on
`nexus.chunks` or the centroid trees applies to the role serving traffic, is
extended to every driving table an open or failed row in `nexus.data_moves`
names (a completed move's table keeps its owner-read policy until a changeset
drops it, and stays in scope until then; a table that no longer exists
contributes no policy and passes). A table counts as chunk-family for
`on_complete` when the driving table or any table `on_complete` names is in
the family.

The tenant list is the set the table-wide retire-keys routes check
(ChunkKeysAdminHandler.java:97-100 and 116-119): the default tenant, every
tenant holding a `nexus.service_tokens` row, revoked or expired included, and
every tenant that holds rows in the driving table, which one probe on the
admin connection finds (ChunkConstraintAdmin's probe is the precedent). The
move worker builds the list at the start of each pass. Completion needs
every listed tenant's run complete, every tenant's verification empty, and
the table-wide verification empty, so a tenant the list missed fails the
move instead of being skipped.

**Install sequence.** Three steps, in this order (T2
nexus_rdr/246-research-5):

1. A superuser bootstrap, the step that already pre-installs pgvector and
   pg_trgm before the first schema walk (vectors-001-baseline.xml lines 28-36,
   SchemaMigrator's Phase-5 provisioning note): `nexus.db.pg_provision`
   locally, the db-host configuration in the cloud. It creates the
   pg_background extension and the three roles dml_utils names,
   `dml_utils_owner`, `dml_utils_caller` and `ddl_utils_caller`, all NOLOGIN
   and NOBYPASSRLS, and grants: CREATE on the database and on schema public,
   `ddl_utils_caller` and `pgbackground_role` to `dml_utils_owner`;
   `dml_utils_owner` to `nexus_admin` WITH INHERIT FALSE, SET TRUE (so
   `nexus_admin` reaches the owner's privileges, CREATE on public among them,
   only through `SET ROLE` and never by default); `dml_utils_caller` and
   `pgbackground_role` to `nexus_admin` WITH INHERIT TRUE (the move runs as
   `nexus_admin` and calls their functions directly). It also creates the `liquibase` schema owned by
   `dml_utils_owner`, where both libraries keep their tracking tables
   (dml_utils README lines 696-702; ddl_utils README lines 93-95). Roles live
   here, not in a changeset, because the dml_utils walk runs before nexus's
   changelog. An existing install on the bundled cluster gets this step from
   the fast path `pg_provision.provision()` takes on a cluster it already
   provisioned, which the storage daemon runs before it starts the engine
   (`_backfill_provision_grants`, storage_service_daemon.py; it runs only when
   the credentials name the bundled cluster's data directory) and which
   already backfills the diag role; it is extended to create the extension,
   roles, grants and schema when they are missing. A BYO or `--no-pg-bundle`
   PostgreSQL gets it from its DBA, as SQL in docs/configuration.md's
   prerequisites, and the cloud from Activation Step 1. In production the
   engine does not run as a superuser, so it cannot do this itself. Until the
   step has run, moves report `blocked:`. Three cases the bootstrap handles
   (nexus-f3yqd.2.9): a bundle with no pg_background library makes it log the
   reason and create nothing, roles and schema included, and the next start
   with a bundle that has it does all of it; an existing library role is
   converged to NOLOGIN NOBYPASSRLS NOSUPERUSER NOCREATEROLE NOCREATEDB, so a
   hand-made role cannot bypass the tenant policies; an existing `liquibase`
   schema owned by another role is handed to `dml_utils_owner` when it holds
   no relations, and otherwise left alone with a reason the engine reports as
   `blocked: liquibase schema owned by <role>`.
2. At boot the engine checks that the extension and the three roles exist,
   and that the `liquibase` schema is absent or owned by `dml_utils_owner`.
   If they do, it applies ddl_utils' then dml_utils' changelog on its
   migration connection as `nexus_admin`, after a `SET ROLE dml_utils_owner`
   statement, so the objects are owned by `dml_utils_owner` and no second
   login credential exists; then `SET ROLE NONE`, and nexus's changelog runs as
   `nexus_admin` (vectors-029's policy binds the migrating role by
   `CURRENT_USER`, so the reset matters). An engine configured with one
   database role (no separate `NX_DB_ADMIN_*`, so the admin connection is the
   service role) has no `nexus_admin` holding these grants; it skips the
   libraries and reports moves `blocked:`. That Liquibase, not only psql in
   Liquibase's order, applies the libraries this way is verified (Phase 1
   Step 3b, T2 nexus_rdr/246-research-7). The libraries' `Liquibase` objects
   share the one migration connection, so none may be closed before the walk
   that follows it: `Liquibase.close()` closes the connection.
3. If the extension or a role is missing, or either library's changelog
   fails, the engine skips the libraries, serves, and reports every
   registered move `blocked:` with the reason. No library install failure
   fails a boot (Test Plan).

**Roles and grants.** The move runs as `nexus_admin`, which holds
`dml_utils_caller` and `pgbackground_role` (step 1). `dml_utils_owner` needs
USAGE on the driving table's schema and SELECT on the table, because its
SECURITY DEFINER boundary scan (a function that runs with its owner's
privileges and reads the table to cut it into chunks) reads it and checks each
leaf partition (246-research-2). The move worker grants these at the start of
every pass (step 1), idempotently, on the parent and on every existing leaf,
and only when `dml_utils_owner` exists. Grants are not left to the registering
changeset, which runs once and may run before the bootstrap has created the
role. A leaf created later inherits the parent's grants through the partition
grant loop (`nexus.partition_copy_access`, which copies the parent's grants to
each new leaf, vectors-039).

**Progress.** `/v1/status` `boot_work` lists each open or failed move in
nexus-f3yqd.3's shape (BootWorkRegistry.java:45: id, what, startedAt, done,
total, unit, safeToStop, onStop, step). `done` and `total` are
`total_chunks_completed` and `total_chunks` from
`dml_utils_data.migration_run` (006-create-migration-tables.sql lines 27-28),
summed over the move's active runs (`archived_at IS NULL`), `unit: "chunks"`. A run with no rows has a total
of zero. The runs summed are those whose name is the label or begins with
`<label>/`. The total grows as each tenant's or leaf's run is created, so the fraction can
fall while `done` only rises. `safe_to_stop: true` and `on_stop: "resumes from
the last committed batch"`. The shape has no state field, so `what` begins
`blocked:` (no pg_background, a library role or the dml_utils install; an
admin session that cannot see every row; no free background worker slot; a
verification timeout; with the retry count, and "needs an operator" past the
cap), `paused:` (with `paused_by`) or `failed:` (with `failure`); nx doctor
shows a red line for `blocked:` and `failed:` and a note for `paused:`, one
line per move.

**Readers during a move.** Each move's RDR or bead states how readers behave
while it runs: read both shapes, or keep the feature off until `completed_at`
is set. A move whose readers cannot cope is redesigned, not run at boot.

**Finishing a move.** A move's finishing step (VALIDATE CONSTRAINT on a check
added NOT VALID, dropping the old column, swapping a table in) is its
`on_complete` SQL when the move's own release knows it. On the chunk-family
tables (`nexus.chunks`, its leaves, and every table with a foreign key into
it: `catalog_document_chunks`, `topic_assignments`, `chunk_orphaned_at`) that
is the only way to finish: a later-release changeset that waits with
`onFail="CONTINUE"` is refused there by
`tests/test_changelog_continue_precondition_chunks_lint.py` (which also
refuses the partition helpers, `partition_...`, and the builder, `pci_...`, in
such a body), because
`SchemaMigrator` does not count a self-retrying changeset as schema work
(`RetryingChangesets.isSelfRetrying`) and keeps the index builders running
through it, and a DROP or ALTER on those tables takes ACCESS EXCLUSIVE, which
would queue behind a `CREATE INDEX CONCURRENTLY`. A later finishing step on a
chunk-family table is therefore a new move whose `on_complete` does it. The
move worker runs `on_complete` on a chunk-family table under `SET LOCAL
lock_timeout` below `statement_timeout_ms`, so a lock it cannot get fails fast
as a transient `blocked:` (55P03) and readers never queue behind it; that is
the guarantee. Before it, as a best effort, it ends the per-collection index
builders (the query behind `SchemaMigrator.terminatePciBuilders`, extracted so
both callers share it; it selects every builder backend in the database, not
one table's, and reason `data_move_on_complete` is logged). Unlike the
migrator, the move worker does not hold the migration lock the builders
respect, so a builder can start again before `on_complete` takes its lock,
and `lock_timeout` then turns that into a retry. Ending a build discards
work that can take tens of minutes, the cost ed34ea26e removed for plain
restarts, so it happens only for an `on_complete` on a chunk-family table. On any other
table, a step that comes in a later release may be a changeset that carries a
Liquibase precondition on `nexus.data_moves.completed_at` for that label with
`onFail="CONTINUE"`, so it is skipped and retried on each boot until the move
completes; a VALIDATE CONSTRAINT never goes in such a changeset on a gate
rule 2 table. The gate (below) refuses a dependent destructive changeset
without the precondition. On a fresh install a
gated changeset applies at the first boot after the move completes, not the
first boot. If the move it waits for is superseded, the changeset that
registers the replacement also re-points the waiting step: it is re-issued
under a new id with a precondition on the new label.

**Writers during a move.** The engine keeps writing while a move runs, so a
new row can fall into a chunk already done. Three rules cover it. Writers
produce only the new shape from the registering release on (Approach, step
5). A move's template leaves new-shape rows unchanged and is correct against
rows that live writers update or delete while it runs (it rewrites a row from
its own current values, never from values read earlier). And the
verification query gates `completed_at` (The move worker, step 3).

**Changing a move.** dml_utils keeps a run's SQL when the run is created and
ignores a different template on a resumed call (README lines 114-120), so the
template stored in dml_utils is the one that runs; the registry's copy is the
text the run was created with. A move's template and parameters are never
edited (the operator's pause and counter columns are the only row fields
changed by hand): a failed move's fix is
a new changeset whose one INSERT registers a new move with a new label and
names the old label in `supersedes`. Among the ways a new changeset can pass
the gate, this INSERT is how a move is registered or replaced, and it closes
the old move without an UPDATE.

**The check that stops a new boot move.** `DataMoveChangesetGateTest` is an
engine test that reads every changelog. It has three rules.

1. *Row DML.* It refuses UPDATE, DELETE, INSERT ... SELECT, ALTER COLUMN TYPE,
   a generated column re-added and CREATE TABLE AS. The SQL inside an INSERT
   into `nexus.data_moves` (the template, the verification query,
   `on_complete`) is a string value and not DML. A changeset whose only DML is
   one INSERT into `nexus.data_moves` passes this rule.
2. *Boot scans.* On a table that grows with user data it refuses an ADD
   CONSTRAINT without NOT VALID, a VALIDATE CONSTRAINT and a SET NOT NULL,
   which scan the whole table at boot while writing almost nothing. The
   tables are `nexus.chunks` and its per-model tables,
   `nexus.catalog_document_chunks` (the manifest), `nexus.catalog_documents`,
   `nexus.catalog_links` and `nexus.topic_assignments`,
   listed in the test. An ADD CONSTRAINT ... NOT VALID passes.
3. *Waiting for a move.* A changeset that runs a destructive statement (DROP
   COLUMN, DROP TABLE, a rename) on a table some registered move drives must
   carry a precondition on that move's `completed_at` with
   `onFail="CONTINUE"` (Finishing a move); on a chunk-family table it is
   refused outright, by the CONTINUE-precondition lint, and the step belongs
   in a move's `on_complete`. Rule 2's statements stay refused on its tables
   with or without a precondition. The registering changeset is exempt from
   this rule only for what it adds: the new shape (new tables and nullable
   columns) and NOT VALID constraints; a destructive statement in it is
   refused like any other. The test matches by table
   name, so it cannot see which move a changeset depends on; the review checks
   that the precondition names the right label.

An exemption line, naming a table whose size does not grow with user data and
the reason, lifts all three rules for that changeset. The frozen list of
changesets that existed when the gate landed (derived from the changelogs at
that commit and stored as a resource) exempts them from all three rules.

The static check reads changelog text only: DML inside a `sqlFile`, a
function body or a DO block's dynamic SQL passes it, and so does a
non-concurrent CREATE INDEX. nexus-f3yqd.1's write gate, which measures the
WAL an upgrade boot writes, is the backstop for DML the static check cannot
see. A read-only scan writes little and is not caught by it; the table rule
above covers those scans on the tables that matter.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Chunked, resumable data move | dml_utils | Reuse (Sam's decision) |
| Background job registry and status | service/.../http/EngineJobs.java | Not reused: its jobs are memory only. DataMoveWorker keeps its state in nexus.data_moves and dml_utils' tables, so it survives a restart |
| Progress reporting | /v1/status boot_work (nexus-f3yqd.3) | Reuse its shape |
| Boot write measurement | upgrade-boot write gate (nexus-f3yqd.1) | Reuse: it is the check that no boot-time move remains |

### Decision Rationale

Sam chose dml_utils with its launcher over the engine's own JDBC loop (ask
dml-utils-shape, 2026-11-10), so nexus builds no chunking code of its own. The
bloat measurement (246-research-3) showed the library's function entry point
pins the vacuum horizon; Sam chose on 2026-11-16 to fix that upstream with a
committing procedure entry point rather than move the loop into the engine.
An engine-driven per-chunk loop (Alternative 1) would also avoid the bloat.

## Alternatives Considered

### Alternative 1: The engine runs process_migration_chunk itself, one JDBC transaction per chunk

**Description**: adopt dml_utils' tables and per-chunk routine; replace only
its pg_background launcher with the engine's job worker.

**Pros**: no new C extension on Windows or the cloud host; each chunk still
commits in its own transaction, which bounds bloat the same way.

**Cons**: uses dml_utils outside its supported entry point; nexus owns a
launcher that upstream does not test.

**Reason for rejection**: Sam chose option 2 (ask dml-utils-shape, 2026-11-10).

### Briefly Rejected

- **Nexus's own chunking framework**: Sam's instruction is to adopt dml_utils rather than build our own.
- **Keep moves at boot, but batch them in the changeset**: a batched changeset still blocks serving for the whole move.

## Trade-offs

### Consequences

- Upgrades boot in schema-change time; data moves run while the engine serves.
- Every reader of moved data must handle the in-progress state.
- A new C extension in the local bundle, on four platforms, and a host package on the cloud host.
- The moves that already shipped stay as they are. An install that skips releases across vectors-030-1 or rdr180 still pays for them at boot. Only new moves are removed from boot.

### Risks and Mitigations

- **Risk** (retired 2026-11-10): pg_background might not build on Windows. It builds on all four platforms (Critical Assumptions).
- **Risk** (retired 2026-11-16): a worker under FORCE RLS might see no rows. The spike showed it inherits the caller's tenant setting, so moves run once per tenant (Technical Design, Row-level security).
- **Risk**: a writer that still produces the old shape after the registering release leaves rows the move never visits. **Mitigation**: the verification query gates `completed_at`, and a non-empty result fails the move instead of completing it.
- **Risk**: background workers exhaust max_worker_processes. The cloud host
  has 8 slots, and parallel query and autoprewarm share them. **Mitigation**:
  one move at a time with threads=1, so one background worker at a time. The
  move worker checks for a free slot before it launches; a slot taken between
  the check and the launch is a transient `blocked:`, retried after its
  backoff, never `failed:`.
- **Risk**: a move slows serving or WAL archiving on the cloud host, as the
  boot rewrite did, now while the engine serves. **Mitigation**: a move runs
  at threads=1 with the registry's `sleep_ms` pause after each chunk, and the
  Minimum Viable Validation measures request latency and WAL rate during a
  move and sets the cloud's `sleep_ms` from them.
- **Risk**: the cloud host lacks pg_background until its image is rebuilt.
  **Mitigation**: the engine applies dml_utils' changelog only when the
  extension and the library roles are present (Technical Design, Install
  sequence), so an engine without it serves and
  reports its moves blocked in boot_work. Phase 2 Activation installs it on the
  host (conexus, on Sam's go) before the first registered move is expected to
  run there. Nothing falls back to a boot-time move.

### Failure Modes

- **pg_background missing**: the engine skips the dml_utils changelogs and
  serves; the move worker does not start the move. boot_work
  reports it as `blocked: pg_background is not installed`, and nx doctor shows
  a red line. Nothing falls back to a boot-time move.
- **A chunk fails** (a constraint, a bad template): dml_utils stops launching
  chunks, lets the ones in flight commit, records the failure in
  migration_error, and raises. Unless its SQLSTATE is a recorded transient
  one (40001, 40P01, 55P03, 57014, class 53, 57P01-57P03), the move worker logs
  the label and the error, sets `failed_at` and `failure`, and boot_work reports it as `failed:` with
  the error text. A failed move is not retried, at this boot or a later one.
  A fix is a new changeset that registers a new move with a new label
  (Technical Design, Changing a move).
- **The verification query returns rows**: an old-shape row exists (a writer
  still producing the old shape, a template that missed a case, or a tenant
  the list missed, caught by the table-wide run). The move is reported
  `failed:` with the count per tenant (`failed_at` set) and is not marked
  complete; the fix is in the writer or the template, then a new move that
  supersedes it.
- **A verification run times out**: transient; the move is `blocked:` and the
  verification runs again on the next pass.
- **`on_complete` fails**: on `statement_timeout_ms` or `lock_timeout`
  (55P03) it is transient, the transaction rolls back with `completed_at`
  and `failed_at` unset, and the move is `blocked:` and retried on the next
  pass; on any other error the move is `failed:` with the error, and its
  finishing step is fixed by a superseding move.
- **A library role is missing or a library changelog fails**: the engine
  skips both libraries, serves, and reports every open move `blocked:`
  with the reason.
- **The engine stops mid-move**: chunks already committed stay committed. The
  next boot resumes the label at the first unprocessed chunk.
- **No free background worker slot**, before or at launch: transient; boot_work
  reports `blocked: waiting for a background worker slot`, and the move worker
  retries on its next pass.
- **Diagnosis**: `SELECT * FROM dml_utils.migration_run_summary(...)`,
  `dml_utils.migration_errors`, and the engine log's
  `event=data_move_*` lines.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified
- [x] bytea keys merged in Hellblazer/dml_utils (PR #1, a6cd544, 2026-11-10). Sam ruled 2026-11-16 to pin merged commits rather than wait for releases; he may tag releases later. ddl_utils is pinned at 5f088e2
- [x] the committing procedure entry point merged in Hellblazer/dml_utils (Step 1's second PR, Hellblazer/dml_utils#2, merge commit 28b0b3aa9, 2026-11-16); nexus pins 28b0b3aa9, since a6cd544 has bytea keys but no procedure. Its bloat test (RunMigrationChunksBloatTest) is green in the PR's Maven Build run 43900555823 on a tree identical to 28b0b3aa9's
- [ ] a four-column primary key supported in Hellblazer/dml_utils (Step 1's third change) before any move over nexus.chunks, the Minimum Viable Validation's move included; nexus re-pins to the commit that carries it
- [x] pg_background in the bundle on all four platforms, in a released bundle that the engine substrate pins (engine-service-v0.1.194's bundles carry pg_background 2.0)
- [ ] ddl_utils installed ahead of dml_utils in the engine's schema setup (Step 4)
- [ ] the superuser bootstrap creates pg_background, the three library roles and their grants: `nexus.db.pg_provision` locally (Step 4) and the db-host configuration in the cloud (Activation Step 1)
- [ ] A Java test image with pg_background for the engine suite's data-move tests (Step 3c)

### Minimum Viable Validation

An upgrade from the previous release on a copy of a live-like store, with
one registered move over `nexus.chunks`: the engine serves within
schema-change time, the move runs as a background job with progress in
/v1/status, a kill mid-move resumes from the last committed chunk, and
f3yqd.1's write gate measures no boot-time row move. While the move runs, a
steady search load measures p99 request latency and the WAL rate against the
same load with no move; the cloud's `sleep_ms` is set so the move adds no
more than 20% to p99 (the first measured bound; the move's bead records the
numbers).

### Phase 1: Code Implementation

#### Step 1: bytea keys, a committing entry point and four-column keys in dml_utils

A reviewed PR to Hellblazer/dml_utils covering a single bytea key and a
composite (text, text, bytea) key. Literals do not depend on bytea_output or
standard_conforming_strings, and each row lands in exactly one chunk. The
PR also covers resume and states the partitioned-table finding. Merged as
PR #1 (a6cd544). A second PR, merged as PR #2 (28b0b3aa9), adds a PROCEDURE entry point that takes the
same parameters as `run_migration_chunks` and COMMITs after each chunk launch
and poll round. A third change lets dml_utils chunk on a primary key of four
columns, which nexus.chunks has; until it lands, a move over nexus.chunks is
refused when its run is created. The second is a restructured coordinator, not a wrapper: the
function's loop sits inside a block with an EXCEPTION handler, where
PL/pgSQL refuses a COMMIT. The PR comes with a test
that a mid-run VACUUM finds no dead row held back by the coordinator (Sam's
choice, 2026-11-16). Nexus pins dml_utils at 28b0b3aa9 (the merge of PR #2: bytea keys and the
procedure), then the commit that carries the four-column change, and
ddl_utils at 5f088e2 (Sam, 2026-11-16), until releases are tagged.

#### Step 2: pg_background in the PG bundle

Built into the bundle for Linux x86_64 and arm64, macOS arm64 and Windows x64,
with a pinned version. pg_background.control joins the contrib completeness
checks, and a smoke test runs `CREATE EXTENSION` plus one background worker on
a built bundle.

#### Step 3: Spike the open assumptions

Check two things: how a pg_background worker under the schema-walk role
reaches rows under FORCE RLS, and that dml_utils drives a table partitioned and
keyed like nexus.chunks. Done 2026-11-16 on a throwaway cluster from the
released bundle (T2 nexus_rdr/246-research-1, -2): a worker inherits the
caller's role and tenant setting, so a move runs once per tenant (Technical
Design, Row-level security), and dml_utils processed every row exactly once
across a killed coordinator.

#### Step 3b: Spike the Liquibase install

Before the engine side is built: apply the vendored ddl_utils and dml_utils
changelogs through Liquibase (not psql) as `nexus_admin` after `SET ROLE
dml_utils_owner` as a statement on the migration connection (not the JDBC
`options` startup parameter: `RESET ROLE` returns to a role given there, which
3B measured on PG 17.5, while `SET ROLE NONE` always returns to the session
user), then `SET ROLE NONE` and nexus's changelog, and check the owners and that vectors-029's policy binds
`nexus_admin`. The result closes or reopens the Liquibase critical assumption
before Step 4 starts.

Done 2026-11-16 (T2 nexus_rdr/246-research-7): the assumption holds. A
throwaway cluster from the engine-service-v0.1.194 bundle, the roles and
grants of Install sequence step 1 made by hand, and Liquibase 4.29.0 driven
from a throwaway Java main. Every library object and both libraries'
tracking tables (`liquibase.ddl_utils_databasechangelog`,
`liquibase.dml_utils_databasechangelog` and their lock tables) belong to
`dml_utils_owner`; nexus's tracking table, `nexus.chunks`, vectors-029's
three probe functions and the policy's `polroles` belong to or name
`nexus_admin`. A second boot on the same cluster was a clean no-op. The one
thing Step 4 must respect: closing a `Liquibase` object closes the shared
connection, so the walks share it and close it once, at the end.

#### Step 3c: A test image with pg_background

The engine suite's Testcontainers image (`pgvector/pgvector:pg17`) has no
pg_background. Build one from it with pg_background 2.0.4 at the bundle's
pinned commit, pin it the way the suite pins its image, and use it for every
data-move test.

#### Step 4: The engine side

- `nexus.db.pg_provision`'s bootstrap creates pg_background, the three
  library roles (NOLOGIN, NOBYPASSRLS), the grants and the `liquibase` schema
  in Technical Design, Install sequence, step 1; the storage daemon's
  `_backfill_provision_grants` does the same for an install provisioned
  before this change;
- ddl_utils' and dml_utils' changelogs vendored at the pinned commits, with a
  test that compares each copied file with that commit;
- the boot check and apply order (extension and roles present, ddl_utils then
  dml_utils as `nexus_admin` with the session role `dml_utils_owner`, then
  nexus's changelog), and the skip-and-serve path when anything is missing or
  fails;
- `nexus.data_moves`, with its columns, the foreign key and uniqueness on
  `supersedes`, NOT NULL on the verification query, and the CHECKs on the
  chunk size, `sleep_ms` and the retry and pause columns;
- `DataMoveWorker`: the per-pass grants for `dml_utils_owner` (USAGE on the
  schema, SELECT on the table and every existing leaf, only when the role
  exists), the install and slot checks, the dedicated per-tenant connection
  with its SET and RESET, the tenant list, the per-tenant and table-wide
  verification in READ ONLY transactions under `statement_timeout_ms`,
  `on_complete`, the per-leaf runs on a tenant-partitioned driving table with
  the leaf list from `nexus.partition_bound_value`, the failure classes (the
  recorded and raised SQLSTATE lists, told apart by the `error_id` mark taken
  before each CALL), the retry counter, backoff and cap, the pause read before
  each pass, each run's CALL, the verification and `on_complete`, the CALL's
  backend pid in its log event, the `cancelled_at` read from the active run
  after each CALL, the `NX_DATA_MOVE_SLEEP_MS` clamp, and a test of what
  `pg_cancel_backend` on the CALL does to a chunk in flight;
- for any driving table other than `nexus.chunks`, the owner-read SELECT
  policy for the migrating role in its own guarded changeset after the
  registering one, and `ChunksIsolationCheck` extended to every driving table
  in `nexus.data_moves`;
- for `on_complete` on a chunk-family table: `SET LOCAL lock_timeout` and the
  shared, extracted builder-termination query;
- boot_work entries and the nx doctor line;
- the engine log events.

#### Step 5: The gate

`DataMoveChangesetGateTest`, with the frozen list derived at its commit, the
table rule's list of tables, and the completion-precondition rule.

#### Step 6: The rule in writing

The engine release doc (`docs/agents/engine-service-release.md`) and the
changeset section of `tests/AGENTS.md` both carry the rule. AGENTS.md lines go
to Sam through halcyon.

### Phase 2: Operational Activation

#### Activation Step 1: pg_background on the cloud database host

This is a conexus host change on Sam's go, carried out by bob. The PGDG
pg_background package goes into the db-host golden image or db-configure,
because the host has no general egress. Then `CREATE EXTENSION pg_background`
runs as superuser in the nexus database, with the three library roles and
their grants (Technical Design, Install sequence, step 1). The package
version must match the bundle's pinned version. Sam ruled 2026-11-15 that it waits until
the first job that uses it lands in an engine, or until PGDG ships 2.0.4. An
engine without it serves and reports its moves blocked (Technical Design), so
this step lands before the first registered move is expected to run in the
cloud.

#### Activation Step 2: the first registered move

Step 1's second PR and four-column change, Steps 3b and 3c, and Step 4 come before this. The first
move to use this path ships in its own engine cut. That cut's
upgrade boot is measured by f3yqd.1's gate, and the move's progress is read
from /v1/status on the live engine.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| nexus.data_moves rows | In scope: boot_work, nx doctor | In scope: dml_utils.migration_run_summary | In scope: dml_utils.archive_migration_run and delete_archived_migration_runs once complete | In scope: completed_at set, every tenant's and the table-wide verification query empty, and no `migration_error` row for the final pass (older rows from retried chunks remain until the runs are archived and deleted) | Part of the database backup (pgBackRest in the cloud) |
| dml_utils schemas | N/A | N/A | N/A (part of the engine schema) | dml_utils' Liquibase tracking table | Part of the database backup |

Pausing a move is an operator's SQL statement as `nexus_admin`, with no CLI
or HTTP route: `UPDATE nexus.data_moves SET paused_at = now(), paused_by =
'<who and why>' WHERE label = '<label>';` and, to resume, `UPDATE
nexus.data_moves SET paused_at = NULL, paused_by = NULL WHERE label =
'<label>';`. The move worker reads the row before each pass, each run's CALL,
the verification and `on_complete`, so a pause takes effect at the next of
those points; a run in flight continues to its end unless the operator also
cancels its backend (Technical Design, Pause). Clearing a blocked move's retry
count after fixing its cause: `UPDATE nexus.data_moves SET transient_retries
= 0, last_transient_at = NULL, last_transient = NULL WHERE label =
'<label>';`, so the next pass runs at once.

### New Dependencies

- dml_utils (Hellblazer/dml_utils, a fork of eyupmiduck/dml_utils), MIT
  licence, vendored at a pinned commit.
- ddl_utils (Hellblazer/ddl_utils), MIT licence, vendored at 5f088e2; its
  licence notice ships with the vendored files.
- pg_background (vibhorkum/pg_background), PostgreSQL License, the same
  permissive terms as PostgreSQL itself (its LICENSE file). The bundle can
  redistribute it beside PostgreSQL with that notice included.

## Test Plan

- **Scenario**: a move hits a transient error 21 times. **Verify**: the waits follow 30 s doubling to 15 minutes, `blocked:` shows the count and, from the 20th, "needs an operator"; the move is never `failed:`, and once the cause is fixed the next pass runs it and resets the counter.
- **Scenario**: an operator pauses a move while a run's CALL is in flight. **Verify**: that run finishes, no further CALL starts (for that tenant's other leaves or any other tenant), boot_work shows `paused:`, and clearing `paused_at` resumes the move.
- **Scenario**: new `nexus.chunks` leaves appear (a new model, which creates a leaf for every tenant, and a new tenant) while a move is between passes. **Verify**: no existing run is disturbed; the new leaves hold only new-shape rows and the move completes with an empty verification and `done` equal to `total`; a new leaf seeded with an old-shape row makes the verification non-empty and the move `failed:`.
- **Scenario**: a tenant's session runs a move. **Verify**: every leaf it CALLs on is one of that tenant's leaves from the catalog, never another tenant's.
- **Scenario**: an operator cancels a run with `cancel_migration_run`. **Verify**: the move is `failed:` with "run cancelled at <time>", not completed.
- **Scenario**: a CALL raises 42501 with no new `migration_error` row, then a chunk records 42883 (a template naming a missing function), on a run that already holds an older 40001 row. **Verify**: the raised 42501 is transient, the recorded 42883 is permanent, and the old row changes neither.
- **Scenario**: a pause is set while the last run's CALL is in flight. **Verify**: the run finishes, and neither the verification nor `on_complete` runs until the pause is cleared.
- **Scenario**: a move over a table with a (text, text, bytea) key, 100k rows, and one over a list-partitioned table with nexus.chunks' four-column key. **Verify**: every row is processed exactly once; `done` never falls, and at the end `done` equals `total`.
- **Scenario**: the engine is killed after some chunks commit. **Verify**: the next boot serves at once and resumes at the next chunk, and the final state equals an uninterrupted run.
- **Scenario**: pg_background is absent. **Verify**: the engine skips the dml_utils changelogs and serves, the move is reported blocked, and no row changes; after the extension is created, the next boot applies the changelogs and the move runs.
- **Scenario**: a chunk fails. **Verify**: the move is reported `failed:` with the error, `failed_at` is set, the committed chunks stay, and neither the next pass nor a later boot retries it.
- **Scenario**: tenants under FORCE RLS. **Verify**: the move runs for every registered tenant (once per leaf of the tenant on `nexus.chunks`), rewrites every tenant's rows, and no run, whether as the move's role or through `dml_utils_owner`'s boundary scan, touches a row of a tenant other than its own; on `nexus.chunks` the per-tenant verification counts only its own tenant's rows although `nexus_admin` can read them all.
- **Scenario**: bloat. A move over 100k rows with autovacuum off, VACUUM run mid-move. **Verify**: the coordinator's backend_xmin is null between chunks and VACUUM reports no dead row "not yet removable" from chunks already committed.
- **Scenario**: writers during a move: insert new-shape rows (some into chunks not yet run), update and delete existing rows of both shapes while a move runs. **Verify**: every surviving pre-existing row ends in the new shape, every new row keeps the values its writer gave it, and the verification query is empty.
- **Scenario**: a writer inserts an old-shape row, with a key in a chunk already done, after the move starts. **Verify**: the verification query returns it, the move is reported `failed:` and `completed_at` stays NULL.
- **Scenario**: a tenant that holds rows in the driving table and no `service_tokens` row. **Verify**: the admin probe lists it, its rows are moved, and if it is left out of the per-tenant runs the table-wide verification fails the move.
- **Scenario**: a library role is missing, then a library changelog fails. **Verify**: each time the engine serves, the nexus walk completes, and every move is reported `blocked:` with the reason.
- **Scenario**: a failed move and a later move registered behind it. **Verify**: the later move runs; a new move whose INSERT names the failed one in `supersedes` closes it.
- **Scenario**: a move registered before the library roles exist (the cloud order); the roles are created later and the engine restarts (the libraries install at boot). **Verify**: the next pass grants `dml_utils_owner` its access on the parent and every leaf, and the move runs and completes.
- **Scenario**: a launch finds no free background worker slot (all slots held by a test). **Verify**: the move is `blocked:`, not `failed:`, and runs once a slot frees.
- **Scenario**: after a move over two tenants, the dedicated connection and the admin connection. **Verify**: `current_setting('nexus.tenant', true)` is empty on the admin connection when the table-wide verification runs, and the dedicated connection carries no tenant after the RESET.
- **Scenario**: a move over a table outside the chunk family with an `on_complete` VALIDATE CONSTRAINT, and a later changeset that drops the old column with the completion precondition. **Verify**: the VALIDATE runs only after verification; the later changeset is skipped at each boot until `completed_at` is set, then applies.
- **Scenario**: an install provisioned before this change. **Verify**: the storage daemon's grant backfill creates the extension, roles, grants and `liquibase` schema, and the next boot installs the libraries.
- **Scenario**: the gate over the shipped changelogs. **Verify**: it is green (the frozen list exempts every shipped changeset from every rule), and a new changeset with a validating ADD CONSTRAINT on `nexus.catalog_documents`, or a destructive statement on a registered move's table without the precondition, is red. The template and verification text inside a registering INSERT are not flagged.
- **Scenario**: a move over a chunk-family table whose `on_complete` drops a column, run while a per-collection index build is in flight on that table. **Verify**: the move worker ends the builder first, `on_complete` takes its lock within `lock_timeout` or reports `blocked:`, and no reader waits behind the move; a later-release CONTINUE changeset doing the same drop is red in the CONTINUE-precondition lint.
- **Scenario**: `on_complete` on a chunk-family table while another session holds a conflicting lock. **Verify**: it fails with 55P03 within `lock_timeout`, `completed_at` and `failed_at` stay unset, the move reports `blocked:`, and the next pass completes it.
- **Scenario**: the single-role posture, a move over a table outside the chunk family. **Verify**: the policy changeset is MARK_RAN, the move is registered and `blocked:`, the service role reads only its tenant's rows, and the boot does not refuse.
- **Scenario**: the two-role posture, a move over a table outside the chunk family, then the migrating role granted to the serving role. **Verify**: the owner-read policy exists, and the next boot refuses through the extended `ChunksIsolationCheck`.
- **Scenario**: a new changeset with an UPDATE on chunks. **Verify**: DataMoveChangesetGateTest is red. Registering the same move through nexus.data_moves is green.
- **Scenario**: an upgrade boot on a copy of a live-like store, measured by f3yqd.1's write gate. **Verify**: no row-move writes occur during the boot.

## Finalization Gate

Phase 1 Steps 1 to 3 have reported, with two open items: the bytea change is
merged in dml_utils and Sam chose to pin merged commits; the committing
procedure entry point (Step 1's second PR, Hellblazer/dml_utils#2) is open,
and the four-column-key change is not started. pg_background
2.0 is in engine-service-v0.1.194's released bundles, and the spikes are in T2
nexus_rdr/246-research-1 to -5. Gate round 2 passed with eight fix-check
residuals and six Significant findings; Sam chose on 2026-11-16 to revise for
all fourteen and gate a third round. This revision addresses each in the
Technical Design, Risks, Failure Modes, Phase 1 Steps 3b, 3c and 4, the
Minimum Viable Validation and the Test Plan.

### Contradiction Check

The spikes overturned one premise, that a pg_background worker inherits no
setting from its caller. The Row-level security design, the critical
assumption, Step 3 and the RLS test scenario now all say a move runs once per
tenant with the tenant set by the move worker (once per leaf of the tenant on
a table partitioned one tenant per leaf, a 2026-11-16 amendment). The install-order statements (Technical
Design, Risks, Failure Modes, Activation Step 1, the absent-extension test)
all say the engine applies dml_utils' changelog only when pg_background and
the library roles are present. The statements that dml_utils cannot chunk a bytea key predate its
PR #1 merge and now say so (Research Findings, Key Discoveries, the
dependency table). Round 3's revision changed four rules everywhere they
appear: grants come from the move worker's pass, not the registering
changeset; a failed move is recorded in `failed_at` and never retried;
progress reads dml_utils' chunk counters; and `nexus_admin` reads every
tenant on `nexus.chunks` through vectors-029's policy.

### Assumption Verification

Seven of the nine critical assumptions are verified by spike or by the
extension's own control file. The cloud host's pg_background is checked and
absent; installing it is Activation Step 1, on Sam's ruling of 2026-11-15.
That Liquibase, not only psql, applies the libraries with the session role
set was verified in Phase 1 Step 3b (T2 nexus_rdr/246-research-7).

### Scope Verification

The RDR changes how a new data move runs. It does not rewrite the moves that
already shipped (Consequences), and it names no first move: Activation Step 2
ships whichever move comes first in its own engine cut. Gap 1 closes for new
moves only; converting the shipped moves is left out by choice (Research
Findings).

### Cross-Cutting Concerns

Tenant isolation (per-tenant runs under FORCE RLS), roles and grants (the
library's fixed owner role and grants re-issued on every pass), background
worker slots on the cloud host (one move, one thread, transient on a full
host), load on the serving engine (`sleep_ms` and the measured p99 bound),
progress and stop answers (boot_work), finishing a move (`on_complete` and the
completion precondition), and deploy order (the extension and role checks at
boot, and the daemon's retrofit) are each covered above.

### Proportionality

The engine adopts Sam's library at pinned commits, at one thread, behind one
registry table and one worker. Adopting it added bytea keys and a committing
procedure entry point upstream, a ddl_utils dependency, three roles and their
bootstrap, vendored changelogs with a pin check, per-tenant runs and a
verification query; nexus owns the registry, the worker loop, the tenant
loop, the verification gate, `on_complete`, DataMoveChangesetGateTest and the
reporting. At threads=1 pg_background's parallelism is unused; its role here
is a commit per chunk, which Alternative 1 (the engine's own JDBC loop) would
also give. Sam chose the library and its launcher; that choice, not a
measured advantage, is why it stands. The mechanism has no consumer until
the first registered move.

## References

- Bead nexus-f3yqd.2, epic nexus-f3yqd
- Hellblazer/dml_utils README and functions at a6cd544 (PR #1 merged, 2026-11-10)
- Board ask linda/dml-utils-shape (answered 2026-11-10, option 2)

## Revision History

- 2026-11-16: Phase 1 Step 3 spikes reported (T2 nexus_rdr/246-research-1, -2) on engine-service-v0.1.194's released bundle. pg_background workers inherit the caller's role and tenant setting, so moves run once per tenant; dml_utils and ddl_utils install as a non-superuser `dml_utils_owner`, whose SELECT on every chunk leaf the partition grant loop now carries; partition-wise and plain runs over a FORCE-RLS (text, text, bytea) table processed every row exactly once across a killed coordinator. Step 2 is done. Step 1's bytea change is merged; a dml_utils release to pin is still open.
- 2026-11-16: dml_utils' changelogs are applied only when pg_background is installed, so an engine without it serves and reports its moves blocked; the cloud install lands before the first registered move runs. Finalization Gate responses written.
- 2026-11-16: gate round 1 fixes: writers during a move and a verification query that gates completion, the tenant source, progress in chunks, one source for a move's SQL, stale bytea text, the measured coordinator bloat and Sam's choice of a committing procedure entry point in dml_utils, and Sam's pin of the merged commits.
- 2026-11-16: fix check of round 1's fixes failed on one counted defect (T2 nexus_rdr/246-fix-check-e8f69a581170): the tenant list now includes tenants found by the admin probe and completion needs a table-wide verification; the counted implementation-level items (superseded moves, the bootstrap's plan steps and grants, pending-chunk writers, the dml_utils pin, the session-level tenant setting, the slot wording) and the observations are fixed with it.
- 2026-11-16: Gate round 2 — PASSED (0 Critical, 6 Significant, 0 ship-blocker(s)); blob `ee87998f1575`; critique `nexus_rdr/246-gate-critique-2026-11-16-r2`.
- 2026-11-16: round 3 revision on Sam's choice (ask rdr246-accept, "Revise, gate round 3"): the eight fix-check residuals (nexus_rdr/246-fix-check-ee87998f1575) and the six round 2 Significant findings (nexus_rdr/246-gate-critique-2026-11-16-r2), addressed in the Technical Design, Risks, Failure Modes, Steps 3b, 3c and 4, the Minimum Viable Validation and the Test Plan.
- 2026-11-16: before round 3, the fixes for the nine defects the third fix check counted (T2 nexus_rdr/246-fix-check-9ebbce21154d): a tenant bind parameter for the verification query, the admin visibility check in pass step 1, the gate's three rules and their exemptions, `on_complete` atomic with `completed_at`, failure classes by SQLSTATE and by whether dml_utils recorded the error, a per-environment `sleep_ms` override, a CHECK on `label`, vectors-029's guard on the owner-read policy, and SET ROLE NONE; plus nexus.chunks' four-column key (a third dml_utils change) and the observations.
- 2026-11-16: Gate round 3 — PASSED (0 Critical, 5 Significant, 0 ship-blocker(s)); blob `17045500b1f0`; critique `nexus_rdr/246-gate-critique-2026-11-16-r3`.
- 2026-11-16: accepted by Sam ("Accept, residuals to beads"; dml_utils with its pg_background launcher kept). Residual dispositions: round 3's five to beads nexus-f3yqd.2.1 (the BLOCKS-PLANNING one, chosen by Sam), nexus-f3yqd.2.2, nexus-f3yqd.2.3, nexus-f3yqd.2.4 and nexus-f3yqd.2.5; the eight carried from round 2 to commit e39d4fb28 (fix check `nexus_rdr/246-fix-check-9ebbce21154d`).
- 2026-11-16: residual beads nexus-f3yqd.2.1 (chunk-family tables finish only through `on_complete`, with builder termination and `lock_timeout`), .2.2 (gate rules 2 and 3), .2.4 (the four-column change listed as a prerequisite and an assumption) and .2.5 (the PCI-builder and `validCheckSum` citations) fixed in the text.
- 2026-11-16: nexus-f3yqd.2.1 completed after its review: an `on_complete` lock timeout is transient, the owner-read policy is its own guarded changeset with `ChunksIsolationCheck` extended to driving tables, and builder termination is best effort under `lock_timeout`.
- 2026-11-16: Install sequence step 1 scoped to the bundled cluster (BYO via docs/configuration.md, cloud via Activation Step 1), with the bootstrap's handling of a bundle without the library, drifted library roles and a foreign-owned `liquibase` schema; the boot check gains the schema owner (reviews of nexus-f3yqd.2.9).
- 2026-11-16: the grant of `dml_utils_owner` to `nexus_admin` is WITH INHERIT FALSE, SET TRUE, so the owner's CREATE on public does not leak to the engine's role by default (found by tests/db/test_pg_provision.py TestPublicSchemaGrantIsLoadBearing on nexus-f3yqd.2.9).
- 2026-11-16: residual nexus-f3yqd.2.3 settled (wren, agreed with linda, who builds DataMoveWorker): the registry gains the transient retry counter, an operator pause and bounds on the chunk size and `sleep_ms`; Technical Design gains "Retries, pause and cancelled runs" (backoff 30 s doubling to 15 min with no end to retries, a cap of 20 that adds "needs an operator", the pause read before each tenant's CALL, resume of an interrupted run, archive and re-create on a changed partition tree, `failed:` only for a run dml_utils records as cancelled, one SQLSTATE list for recorded and raised errors with a chunk error counted only if recorded since the pass started, the order rule); Step 4 and the Test Plan name them; Day 2 Operations gains the pause statement.
- 2026-11-16: per-leaf runs for partitioned driving tables (wren, with linda; fix check `nexus_rdr/246-fix-check-839e922d1` recorded the earlier text's residuals): `nexus.chunks` moves run one plain-table run per tenant leaf from that tenant's catalog, so no tree check applies and a new leaf cannot restart a run; the move worker never runs a move partition-wise and any 55000 is permanent, with no archive and re-create; a recorded chunk error is transient only for 40001, 40P01, 55P03 and 57014, and 42501 and 42883 are transient only when raised outside a recorded chunk error, told apart by an `error_id` mark taken before the CALL; the pause is also read before the verification and `on_complete`, and a run in flight is stopped by cancelling its backend; `cancelled_at` is read from the active run; the hand-set columns get CHECKs; progress sums active runs; the Day 2 verify allows older retried-chunk rows.
- 2026-11-16: per-leaf fix-check round 1 (wren): the failure list and Step 4 no longer carry the archive-and-re-create design; recorded class 53 and 57P01-57P03 chunk errors are transient (dml_utils re-claims an uncompleted boundary); the leaf list is read with `nexus.partition_bound_value`; the CALL's pid is in its log event and a backend cancel is transient, not `cancel_migration_run`; the per-leaf run is a Critical Assumption to spike.
