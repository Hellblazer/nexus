---
title: "Data Moves Run as Batched, Resumable Background Jobs, Never at Engine Boot"
id: RDR-246
type: Architecture
status: draft
priority: high
author: linda (session), for Sam
reviewed-by: pending (code-review-expert and substantive-critic at the gate)
created: 2026-11-10
accepted_date:
related_issues: [nexus-f3yqd, nexus-f3yqd.1, nexus-f3yqd.2, nexus-f3yqd.3]
---

# RDR-246: Data Moves Run as Batched, Resumable Background Jobs, Never at Engine Boot

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Two upgrades in a row rewrote user data on the first boot of the new engine:
the 8.0.0 repartition of the chunk tables and, it is suspected, a rebuild run by
a startup reconciler. Each took about 20 minutes of writes at about 65 MB/s.
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
Research Findings lists every such site.

#### Gap 2: No rule stops the next data move from landing at boot

Nothing in review, CI or the release process says a data move may not be a
boot step, and nothing measures what a boot writes. The second rewrite shipped
the release after the first. nexus-f3yqd.1 (wren) adds the measurement, an
upgrade-boot write gate in the engine release. This RDR adds the rule the gate
checks and the mechanism a data move uses instead.

#### Gap 3: A long move gives no progress and no safe-stop answer

A user watching a busy disk cannot tell whether the engine is migrating,
stuck, or broken, how far it has got, or whether a restart loses the work.
The background job reports its progress in rows, says it is safe to stop, and
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
- The largest table, nexus.chunks, is partitioned. Its primary key is
  (tenant_id text, collection text, chash bytea), and chash is a 32-byte sha256.

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
  CONCURRENTLY` at a time (`PciReconciler.java:453`, `PciBuilderSession.java:103`).
  Its cost still grows with chunk volume. Every boot ends any build in flight
  (`SchemaMigrator.terminatePciBuilders`, `SchemaMigrator.java:587`), and the
  reconciler then rebuilds that index from scratch. Its first pass also
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

- Shipped changesets cannot be converted. Liquibase checksums every
  changeset, and an install that already ran one fails validation if its text
  changes. The rule therefore governs new changesets.
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
- `dml_utils_lib.process_migration_chunk(run_id, boundary_no, sql_text)` claims one
  boundary with `UPDATE ... RETURNING` and runs its SQL in the caller's
  transaction. Only the launcher needs pg_background. **Verified** (source
  read, functions/dml_utils_lib/process_migration_chunk.sql).
- Supported key column types as of dml_utils PR #142 (main, 2026-11-10): smallint, integer, bigint, text,
  uuid, timestamptz (the migration_key composite type, 004-create-domains.sql).
  bytea is not supported, so nexus.chunks cannot be a driving table today.
  **Verified** (source read).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| dml_utils (Hellblazer/dml_utils main as of PR #142) | Yes | Launcher needs pg_background; per-chunk routine does not; no bytea keys |
| pg_background (vibhorkum/pg_background) | Yes, v2.0.4 (tag and pinned commit) | Runs SQL in a background worker with its own transaction. No shared_preload_libraries needed (verified on four platforms). Calls POSIX random() (one site, polling jitter). PostgreSQL License |
| nexus PG bundle (scripts/build_pg_bundle.sh, build_pg_bundle_windows.py) | Yes | Before this RDR: PG, pgvector and contrib, with no pg_background. With Phase 1 Step 2, pg_background v2.0.4 is in every bundle, and the contrib checks fail closed without it |

### Key Discoveries

- **Verified**: before this RDR, no local bundle or nexus changeset provided pg_background. Phase 1 Step 2 adds it to the bundle.
- **Verified** (bundle code review): the engine substrate in CI downloads the pinned *released* bundle, and the Java suite boots the pgvector/pgvector:pg17 image. Neither has pg_background until a bundle release carries it. So the first changeset that runs `CREATE EXTENSION pg_background` must wait for that bundle release (the pin and floor move), and Java tests of a data move need an image with the extension.
- **Documented**: the cloud host is self-managed, so installing pg_background
  there is a nexus host-configuration step with no vendor allow-list (bob, 2026-11-10).
- **Verified**: dml_utils cannot chunk on a bytea key.

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
  `pg_catalog.decode('<hex>','hex')`, a constant the planner folds. Review
  round 1 is in progress. — **Method**: Spike
- [x] dml_utils works with a partitioned driving table (PK on the parent) — **Status**:
  Verified for bytea, non-partition-wise and partition-wise (dml_utils
  supports partition-wise runs since 1.16), in the same change. Repeated on a
  FORCE-RLS, list-partitioned (text, text, bytea) table as the engine's role,
  partition-wise and over the parent, with resume after a killed coordinator:
  every row processed exactly once (T2 nexus_rdr/246-research-2) — **Method**: Spike
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

1. A changeset creates the new shape (empty table, nullable column, new index
   built CONCURRENTLY where the engine allows it) and registers the move: a
   row naming the label, the DML template, the driving table and the chunk
   size. Registration writes one row, whatever the data size.
2. After boot, the engine's data-move worker finds every registered move that
   is not complete and calls `dml_utils.run_migration_chunks` for it. Each
   chunk commits on its own; a restart resumes from the last committed batch.
3. While a move runs, /v1/status `boot_work` reports it in nexus-f3yqd.3's
   shape: done and total in rows, `safe_to_stop: true`, `on_stop: "resumes
   from the last committed batch"`. nx doctor shows the same.
4. Code that reads the moved data copes with a move in progress (it reads
   both shapes, or the feature waits for the move's completion row). A
   changeset that would need the move finished first is a design error the
   review catches.

### Technical Design

**dml_utils in the engine.** The engine depends on a pinned dml_utils
release, which carries the bytea key support. At boot, before nexus's own
changelog runs, it applies dml_utils' bundled changelog. That changelog keeps
its own tracking tables in the `liquibase` schema, separate from nexus's.
Installing it is schema only and costs the same whatever the data size.
It cannot be created without pg_background: dml_utils' plpgsql functions
declare variables of pg_background's types (`pg_background_handle`,
`pg_background_run_result`), and dml_utils' own test base installs the
extension first because the routines "declare its types, so the extension must
exist when Liquibase creates them" (PostgresTestBase.java). So the engine
applies the dml_utils and ddl_utils changelogs only when `pg_extension` holds
pg_background. When it does not, the engine skips them, serves, and reports
every registered move as blocked; the next boot that finds the extension
applies them. A database without the extension therefore never fails a boot,
and the cloud install only has to land before the first registered move is
expected to run.

**The registry.** One table, `nexus.data_moves`, has one row per registered
move:

- the label (primary key, also the dml_utils run label);
- the DML template;
- the driving schema and table;
- the chunk size;
- the changeset id that registered it;
- `registered_at` and `completed_at`.

A changeset registers a move with one INSERT. The worker finds the rows where
`completed_at` is NULL.

**The worker.** `DataMoveWorker` is an engine background thread. It starts
after the HTTP listener and handles one move at a time, oldest first. For
each move it:

1. checks that pg_background is installed and that one
   `max_worker_processes` slot is free, and waits otherwise;
2. calls `dml_utils.run_migration_chunks(template, schema, table, label,
   chunk_size, threads => 1)` on its own connection;
3. sets `completed_at` once dml_utils reports the run complete.

A stopped engine leaves committed chunks committed. The next boot's worker
calls the same label, which resumes at the first unprocessed chunk.

**Row-level security.** The chunk tables use FORCE RLS keyed on the
`nexus.tenant` setting, and the engine's roles are all NOBYPASSRLS, so a role
with no tenant set sees no rows. pg_background workers run as the caller and
copy its settings (T2 nexus_rdr/246-research-1). A move over a tenant-scoped
table therefore runs once per registered tenant: the worker sets
`nexus.tenant` on its own connection, then calls `run_migration_chunks` with the
label `<move>/<tenant>`, so each tenant resumes on its own. A move over a table
with no tenant column runs once. It does not run as nexus_svc.

**Roles and grants.** dml_utils fixes its owner role's name, so the engine
creates `dml_utils_owner`, `dml_utils_caller` and `ddl_utils_caller` (NOLOGIN
except the owner, all NOBYPASSRLS) in its role changesets and applies both
libraries' changelogs as `dml_utils_owner`. The move's role holds
`dml_utils_caller` and `pgbackground_role`. `dml_utils_owner` needs USAGE on the
driving table's schema and SELECT on the table and on every leaf, because its
SECURITY DEFINER boundary scan reads the table and checks each leaf
(246-research-2). New chunk leaves are created at run time, so the engine's
partition grant loop grants SELECT to `dml_utils_owner` on each leaf it creates.

**Progress.** `/v1/status` `boot_work` lists each incomplete move in
nexus-f3yqd.3's shape: label, done and total in rows from
`dml_utils.migration_run_summary`, `safe_to_stop: true`, and `on_stop:
"resumes from the last committed batch"`. nx doctor prints the same as one
line per move.

**Readers during a move.** Each move's RDR or bead states how readers behave
while it runs: read both shapes, or keep the feature off until `completed_at`
is set. A move whose readers cannot cope is redesigned, not run at boot.

**The check that stops a new boot move.** `DataMoveChangesetGateTest` is an
engine test that reads every changelog. It refuses a changeset with row DML
(UPDATE, DELETE, INSERT ... SELECT, ALTER COLUMN TYPE, a generated column
re-added, CREATE TABLE AS) unless one of these holds:

- its id is in the frozen list of changesets that existed when the gate
  landed; that list is derived from the changelogs at that commit and stored
  as a resource;
- its only DML is an INSERT into nexus.data_moves;
- it carries an exemption line naming a table whose size does not grow with
  user data, with the reason.

nexus-f3yqd.1's write gate measures a real upgrade boot and catches what a
static scan misses.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Chunked, resumable data move | dml_utils | Reuse (Sam's decision) |
| Background job registry and status | service/.../http/EngineJobs.java | Not reused: its jobs are memory only. DataMoveWorker keeps its state in nexus.data_moves and dml_utils' tables, so it survives a restart |
| Progress reporting | /v1/status boot_work (nexus-f3yqd.3) | Reuse its shape |
| Boot write measurement | upgrade-boot write gate (nexus-f3yqd.1) | Reuse: it is the check that no boot-time move remains |

### Decision Rationale

Sam chose dml_utils with its launcher over the engine's own JDBC loop (option
1 of the ask), so the library is used as its author designed it, and nexus
builds no chunking code of its own.

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

- **Risk**: pg_background does not build on Windows. **Mitigation**: the bundle spike reports it first; the RDR does not lock until it builds.
- **Risk**: a worker under FORCE RLS sees no rows. **Mitigation**: a critical assumption above, spiked before the design locks.
- **Risk**: workers exhaust max_worker_processes. The cloud host has 8 slots,
  and parallel query and autoprewarm share them. **Mitigation**: one move at a time with threads=1,
  so one pg_background worker at a time. The move worker reads
  max_worker_processes and the slots in use before it launches, and backs off
  when none are free.
- **Risk**: the cloud host lacks pg_background until its image is rebuilt.
  **Mitigation**: the engine applies dml_utils' changelog only when the
  extension is present (Technical Design), so an engine without it serves and
  reports its moves blocked in boot_work. Phase 2 Activation installs it on the
  host (conexus, on Sam's go) before the first registered move is expected to
  run there. Nothing falls back to a boot-time move.

### Failure Modes

- **pg_background missing**: the engine skips the dml_utils changelogs and
  serves; the worker does not start the move. boot_work
  reports it as `blocked: pg_background is not installed`, and nx doctor shows
  a red line. Nothing falls back to a boot-time move.
- **A chunk fails** (a constraint, a bad template): dml_utils stops launching
  chunks, lets the ones in flight commit, records the failure in
  migration_error, and raises. The worker logs the label and the error,
  leaves the move incomplete, and boot_work reports it as `failed` with the
  error text. A later boot retries it. A fix is a new template set through
  `dml_utils.set_migration_run_sql_text`, in a new changeset.
- **The engine stops mid-move**: chunks already committed stay committed. The
  next boot resumes the label at the first unprocessed chunk.
- **No free worker slot**: the worker waits and boot_work reports `waiting
  for a background worker slot`.
- **Diagnosis**: `SELECT * FROM dml_utils.migration_run_summary(...)`,
  `dml_utils.migration_errors`, and the engine log's
  `event=data_move_*` lines.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified
- [ ] bytea keys merged in Hellblazer/dml_utils (done: PR #1, a6cd544, 2026-11-10), and a dml_utils release that nexus pins (none yet: the repository has no tag or release)
- [x] pg_background in the bundle on all four platforms, in a released bundle that the engine substrate pins (engine-service-v0.1.194's bundles carry pg_background 2.0)
- [ ] ddl_utils installed ahead of dml_utils in the engine's schema setup
- [ ] A Java test image with pg_background for the engine suite's data-move tests

### Minimum Viable Validation

An upgrade from the previous release on a copy of a live-like store: the
engine serves within schema-change time, the inventoried moves run as
background jobs with progress in /v1/status, a kill mid-move resumes from the
last committed chunk, and f3yqd.1's write gate measures no boot-time row move.

### Phase 1: Code Implementation

#### Step 1: bytea keys in dml_utils

A reviewed PR to Hellblazer/dml_utils covering a single bytea key and a
composite (text, text, bytea) key. Literals do not depend on bytea_output or
standard_conforming_strings, and each row lands in exactly one chunk. The
PR also covers resume and states the partitioned-table finding. Nexus pins
the release that carries it.

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

#### Step 4: The engine side

- dml_utils' changelog applied at boot;
- `nexus.data_moves`;
- `DataMoveWorker`;
- boot_work entries and the nx doctor line;
- the engine log events.

#### Step 5: The gate

`DataMoveChangesetGateTest`, with the frozen list derived at its commit.

#### Step 6: The rule in writing

The engine release doc (`docs/agents/engine-service-release.md`) and the
changeset section of `tests/AGENTS.md` both carry the rule. AGENTS.md lines go
to Sam through halcyon.

### Phase 2: Operational Activation

#### Activation Step 1: pg_background on the cloud database host

This is a conexus host change on Sam's go, carried out by bob. The PGDG
pg_background package goes into the db-host golden image or db-configure,
because the host has no general egress. Then `CREATE EXTENSION pg_background`
runs as superuser in the nexus and conexus databases. The package version must
match the bundle's pinned version. Sam ruled 2026-11-15 that it waits until
the first job that uses it lands in an engine, or until PGDG ships 2.0.4. An
engine without it serves and reports its moves blocked (Technical Design), so
this step lands before the first registered move is expected to run in the
cloud.

#### Activation Step 2: the first registered move

The first move to use this path ships in its own engine cut. That cut's
upgrade boot is measured by f3yqd.1's gate, and the move's progress is read
from /v1/status on the live engine.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| nexus.data_moves rows | In scope: boot_work, nx doctor | In scope: dml_utils.migration_run_summary | In scope: dml_utils.archive_migration_run and delete_archived_migration_runs once complete | In scope: completed_at set, and no migration_error rows | Part of the database backup (pgBackRest in the cloud) |
| dml_utils schemas | N/A | N/A | N/A (part of the engine schema) | dml_utils' Liquibase tracking table | Part of the database backup |

### New Dependencies

- dml_utils (Hellblazer/dml_utils, a fork of eyupmiduck/dml_utils), MIT
  licence, pinned release.
- pg_background (vibhorkum/pg_background), PostgreSQL License, the same
  permissive terms as PostgreSQL itself (its LICENSE file). The bundle can
  redistribute it beside PostgreSQL with that notice included.

## Test Plan

- **Scenario**: a move over a table with a (text, text, bytea) key, 100k rows. **Verify**: every row is processed exactly once, and progress rises monotonically to the total.
- **Scenario**: the engine is killed after some chunks commit. **Verify**: the next boot serves at once and resumes at the next chunk, and the final state equals an uninterrupted run.
- **Scenario**: pg_background is absent. **Verify**: the engine skips the dml_utils changelogs and serves, the move is reported blocked, and no row changes; after the extension is created, the next boot applies the changelogs and the move runs.
- **Scenario**: a chunk fails. **Verify**: the move is reported failed with the error, the committed chunks stay, and a later boot retries.
- **Scenario**: tenants under FORCE RLS. **Verify**: the move runs once per registered tenant, rewrites every tenant's rows, and no run touches a row of a tenant other than its own; no nexus_svc session ever sees another tenant's rows.
- **Scenario**: a new changeset with an UPDATE on chunks. **Verify**: DataMoveChangesetGateTest is red. Registering the same move through nexus.data_moves is green.
- **Scenario**: an upgrade boot on a copy of a live-like store, measured by f3yqd.1's write gate. **Verify**: no row-move writes occur during the boot.

## Finalization Gate

Phase 1 Steps 1 to 3 have reported: the bytea change is merged in dml_utils
(a release to pin is the open prerequisite, ask dml-utils-pin), pg_background
2.0 is in engine-service-v0.1.194's released bundles, and the spikes are in T2
nexus_rdr/246-research-1 and -2.

### Contradiction Check

The spikes overturned one premise, that a pg_background worker inherits no
setting from its caller. The Row-level security design, the critical
assumption, Step 3 and the RLS test scenario now all say a move runs once per
tenant with the tenant set by the worker. The install-order statements (Technical
Design, Risks, Failure Modes, Activation Step 1, the absent-extension test)
all say the engine applies dml_utils' changelog only when pg_background is
present.

### Assumption Verification

Five of the six critical assumptions are verified by spike or by the
extension's own control file. The cloud host's pg_background is checked and
absent; installing it is Activation Step 1, on Sam's ruling of 2026-11-15.

### Scope Verification

The RDR changes how a new data move runs. It does not rewrite the moves that
already shipped (Consequences), and it names no first move: Activation Step 2
ships whichever move comes first in its own engine cut.

### Cross-Cutting Concerns

Tenant isolation (per-tenant runs under FORCE RLS), roles and grants (the
library's fixed owner role and its leaf grants), worker slots on the cloud host
(one move, one thread), progress and stop answers (boot_work), and deploy order
(the extension check at boot) are each covered above.

### Proportionality

The engine adopts Sam's library as is, at one thread, behind one registry table
and one worker; the only new mechanism nexus owns is the worker loop and its
reporting.

## References

- Bead nexus-f3yqd.2, epic nexus-f3yqd
- Hellblazer/dml_utils README and functions on main as of PR #142, 2026-11-10
- Board ask linda/dml-utils-shape (answered 2026-11-10, option 2)

## Revision History

- 2026-11-16: Phase 1 Step 3 spikes reported (T2 nexus_rdr/246-research-1, -2) on engine-service-v0.1.194's released bundle. pg_background workers inherit the caller's role and tenant setting, so moves run once per tenant; dml_utils and ddl_utils install as a non-superuser `dml_utils_owner`, whose SELECT on every chunk leaf the partition grant loop now carries; partition-wise and plain runs over a FORCE-RLS (text, text, bytea) table processed every row exactly once across a killed coordinator. Step 2 is done. Step 1's bytea change is merged; a dml_utils release to pin is still open.
- 2026-11-16: dml_utils' changelogs are applied only when pg_background is installed, so an engine without it serves and reports its moves blocked; the cloud install lands before the first registered move runs. Finalization Gate responses written.
