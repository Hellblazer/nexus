# RDR-225 cloud deploy and abort runbook (vectors-030-1 and vectors-031)

Operator runbook for the engine tag that carries changeset `vectors-030-1`, which partitions `nexus.chunks` and
`nexus.taxonomy_centroids` by embedding model, then by tenant, and `vectors-031-1` and `-2`, which give every read
function a model and a tenant predicate so each search reads one leaf. Written for nexus-3wh8d.26 from the deploy-shape
critique (T2 `nexus_rdr/225-deploy-shape-review-by-eae`, Critical 1) and the cloud topology answer (T2
`nexus_rdr/225-cloud-topology`). Shape follows `rdr-191-phase5-cloud-fk.md`.

**This walk is IRREVERSIBLE in place once it commits.** The previous engine cannot run against the new layout, so
the only ways back are a restore (PITR) or a fix-forward engine. Read § Abort and rollback before the window opens.

**Who does what.** nexus prepares this runbook and the evidence. conexus runs every production read and write, and
every flip runs on Sam's go typed in conexus's own session; a go relayed through nexus is refused there. Steps that
name conexus's tooling (the PITR fork, the restore, the SSM redeploy document) are conexus-owned: this runbook states
what must be true before and after them and does not give their commands.

**How facts are marked.** A statement without a marker was read from the source named beside it, at develop
`c4e3dc15a` (the commit that pinned `vectors-030-1` as one transaction); the `vectors-031` statements (§ 1, § 4, § 6.1)
were read at `4fe5c20c1` plus the fix round of nexus-3wh8d.17 and .18. **Unverified:** marks a fact nobody here has
measured or seen. Section 10 collects them. **Placeholder `.27:`** marks a number the PITR-fork rehearsal
(nexus-3wh8d.27) measures and fills in; none was invented here.

## 1. What the deploy does

- The engine runs Liquibase before its HTTP server binds (`Main.java`: `SchemaMigrator.migrate`, then
  `ChunksIsolationCheck.verifyAtStartup`, then token seeding). On a migration failure it logs
  `event=schema_migration_failed` and calls `System.exit(1)`; the isolation check failure does the same
  (`event=chunks_isolation_check_failed`).
- `vectors-030-1` is one transactional changeset. It rewrites every row of `chunks` and `taxonomy_centroids` into
  partitioned replacements, keeps the old tables as `chunks_retired_225` and `taxonomy_centroids_retired_225` for 14
  days, and backfills `embedding_model` on `catalog_document_chunks`, `topic_assignments` and `chunk_orphaned_at`
  (an UPDATE of every row of each).
- The changeset sits after `vectors-029` and before the `runAlways` grants (`grants-nexus-svc.xml`,
  `grants-nexus-diag.xml`), which run in the same walk, each as its own changeset.
- `vectors-031-1` and `vectors-031-2` run after `vectors-030-1`, each in its own transaction, so each commits (or
  fails) after the irreversible step. `vectors-031-1` drops the 30 old read-function signatures and creates the
  new ones (two trailing parameters, `p_embedding_model` and `p_tenant`), then fails the walk unless each of the 30
  names has exactly one signature. `vectors-031-2` redefines the three `text_gate_probe_<dim>` functions: definer
  plpgsql when `vectors-029`'s guard still holds (the owner policy `chunks_gate_probe_owner_read` names exactly the
  migrating role and no RLS-subject login role has that role's privileges), invoker SQL otherwise; its definer outcome
  ends with `vectors-029`'s post-condition. Function definitions and grants only: no data effect.
- After the walk, `SchemaMigrator.migrate` reads both partition trees once and, only if a relation does not mirror its
  parent, runs `partition_sync_access` for both parents as the schema owner (§ 6.1). After boot the engine runs
  `ChunksIsolationCheck` over both trees (every parent, model partition and leaf: RLS enabled and forced, the root's
  permissive policies present). A gap that survives the repair is a boot refusal (§ 6.1).
- The cloud deploy is stop-start: `docker stop -t 30 conexus-engine`, `docker rm -f`, `docker run` of the new tag,
  one replica, no blue-green. Nothing serves `/v1/*` during the walk (T2 `nexus_rdr/225-cloud-topology`).
- Downtime budget: about 15 minutes when the fork rehearsal measured it; longer needs only Sam's go, no tenant notice
  (Sam is the only tenant, 2026-10-06).

## 2. Preconditions

All must hold before conexus starts the window. Record the evidence for each in T2
`nexus_rdr/225-walk-rehearsal` (the bead .20 record).

1. The PITR-fork walk rehearsal of the real changeset passed (bead .20 and .27): the fork walk's
   `schema_migration_complete` line matches § 4, reconciliation is exact, and the frozen query set held. Placeholder
   `.27:` FORK_WALL_TIME, FORK_TENANT_COUNT, FORK_RELATION_LOCKS, FORK_PEAK_EXTRA_DISK, FORK_ROW_COUNTS,
   FORK_ORPHAN_CENTROIDS (§ 9).
2. The fork was taken as close to the flip as conexus can manage, and the live `public.databasechangelog` has the same
   set of ids as the fork's. Live data drifts after the fork; the census in § 3 is how the drift is measured.
3. Sam has given the go for THIS window, in conexus's session, having read the IRREVERSIBLE statement.
4. conexus's answers in § 9.2 are in hand (T2 `nexus_rdr/225-conexus-answers` [29505], 2026-10-07). Three live reads
   are still open and belong to the `.27` rehearsal pass: the owner of `diag_chash_conformance` and of the `nexus`
   schema, `pg_stat_archiver`, and the lock and connection settings.
5. The engine tag is cut, signed and its release is published (not a draft); `scripts/list_data_effects.py
   engine-service-v0.1.149 <tag> --record-relay-attestation` was run and the table was pasted into the relay. The
   table must list `vectors-030-1` and must carry the centroid statement of § 3 probe C2 (the DATA EFFECT line in the
   changeset comment names the three UPDATE targets and the table rewrite; it does not say that centroids with no
   registry row are left behind, so state it in the relay).
6. The live ownerless-write mode is read and left alone. `NX_OWNERLESS_WRITE_MODE` is unset or `log-only` for this
   deploy and is not flipped in the same window (`docs/operations/ownerless-write-cutover.md`). conexus reads
   `/v1/status` `ownerless_write_mode` and reports the value; the gate in § 7 asserts that value.
7. Order against the client release. Recommendation of the critique, pending Sam's choice (the wire-ledger entries
   for the 409 `collection_model_mismatch`, 422 `unregistered_collection`, 503 `tenant_creation_busy`, the read-path
   behavior changes and the `chunks_tenant_isolation_intact` semantics are in `docs/wire-contract-pending.md`): deploy the engine first, soak, verify (§ 7), and only then push the client tag that
   bumps `REQUIRED_ENGINE_VERSION`. A PITR restore after the floor-bumped client has shipped leaves local installs
   pinned to a tag the cloud abandoned; engine tags are immutable, so the fix would be a new engine tag and a new
   client.
8. No other engine tag is cut from develop between now and the window unless it carries a plan for `vectors-030-1`:
   any tag cut from develop tip carries the walk, to the cloud and, through the pin, to every local install (critique,
   Significant 6). Unverified: nothing mechanical stops this.

Read the live engine's `release_version` first (`nx service probe`, which reads `/version`; the T2 deploy tracker is
gone). The rest of this runbook assumes it reads `engine-service-v0.1.149`, the current `REQUIRED_ENGINE_VERSION`.
Call the live value PREV_TAG. If it is older, the walk is cumulative and § 4's numbers do not apply: recount.

## 3. Pre-walk census (read-only, run by conexus just before the flip)

Run every statement in a `BEGIN READ ONLY` transaction. Each probe has an abort threshold. **ABORT** means the window
does not open (or stops before the engine stops); the fix is made, the probes re-run, and Sam is told. **HOLD** means
stop and ask Sam before continuing.

**Visibility.** `chunks`, `catalog_collections`, `catalog_document_chunks`, `topic_assignments`,
`chunk_orphaned_at`, `taxonomy_centroids` carry row-level security with FORCE. The schema owner (production
`nexus_admin`) is not BYPASSRLS, so a bare `SELECT count(*)` there can return 0 for a table full of rows, and a probe
that finds nothing then proves nothing (the lesson of RDR-191 runbook § 5). The tenant GUC is `nexus.tenant`
(`SET nexus.tenant = '<t>'`, or `set_config('nexus.tenant', '<t>', false)`). Unverified: `chunks` alone has the
`chunks_gate_probe_owner_read` policy (vectors-029) that lets the owner read every tenant, and that changeset can
MARK_RAN. conexus picks the role: either a role that bypasses RLS, or a per-tenant loop under the GUC with the counts
summed. Probe V is the control that tells you which one you got.

**Probe V (control).** `SELECT count(*) FROM nexus.chunks;` and the same for the other five tables, in the same session
as the probes. Abort if any count is 0 where the fork's count is nonzero, or differs from FORK_ROW_COUNTS by more than
the drift `.27:` DRIFT_TOLERANCE records. A zero here means the session cannot see the rows and every probe below is
vacuous.

**Probe T (tenants and locks).**

```sql
SELECT count(*) AS tenants FROM (
  SELECT tenant_id FROM nexus.service_tokens
  UNION SELECT tenant_id FROM nexus.chunks
  UNION SELECT tenant_id FROM nexus.taxonomy_centroids
  UNION SELECT 'default') t;                                   -- the walk's own tenant set
SELECT count(*) AS models FROM nexus.embedding_models WHERE dimension IN (384, 768, 1024);
SHOW max_locks_per_transaction; SHOW max_connections; SHOW max_prepared_transactions;
```

The walk makes one leaf per (parent, model, tenant): `leaves = 2 x models x tenants`. The RDR measured 1,899 relation
locks for 70 leaves, about 27 per leaf, in a test layout; all of them are held until the one commit. The shared lock
table holds `max_locks_per_transaction x (max_connections + max_prepared_transactions)` slots (PostgreSQL
documentation; the default 64 x 100 gives 6,400).

- ABORT if `tenants` is greater than FORK_TENANT_COUNT (`.27:`): the fork's wall time, lock count and disk peak
  describe a smaller walk.
- ABORT if `leaves x 27` is at or above the slot count less LOCK_HEADROOM (`.27:`; the fork's logged
  `rdr225 walk: N relation lock(s)` line, read from the engine log (§ 4), replaces the 27 and fixes the headroom). The failure it prevents is
  `out of shared memory` in the middle of the walk, after the copy.
- Unverified (NEEDS-LIVE-READ, no recorded measurement; conexus answer 7): the live `max_locks_per_transaction`,
  `max_connections` and `max_prepared_transactions` on the cloud cluster. The critique's figure of
  about 216 locks per tenant is 8 leaves x 27.

**Probe C1 (chunk vectors against the collection's model).** The walk fails, and rolls back after the copy, on any
chunk whose set vector column disagrees with its collection's model dimension. Catching it here costs seconds.

```sql
SELECT count(*) AS dimension_disagrees
  FROM nexus.chunks c
  JOIN nexus.catalog_collections cc ON cc.tenant_id = c.tenant_id AND cc.name = c.collection
  LEFT JOIN nexus.embedding_models m ON m.embedding_model = cc.embedding_model
 WHERE m.embedding_model IS NULL
    OR m.dimension NOT IN (384, 768, 1024)
    OR num_nonnulls(c.embedding_384, c.embedding_768, c.embedding_1024) <> 1
    OR CASE WHEN c.embedding_384  IS NOT NULL THEN 384
            WHEN c.embedding_768  IS NOT NULL THEN 768
            WHEN c.embedding_1024 IS NOT NULL THEN 1024 END <> m.dimension;
```

ABORT if the count is not 0. No rule is written for legacy shapes (Sam, 2026-10-06): the remedy is to fix or delete the
rows through the normal tools, never to edit the changeset. Run the same join for `nexus.taxonomy_centroids` as C1b;
a centroid that has a registry row and disagrees fails the walk the same way. ABORT if not 0.

**Probe C2 (centroids with no registry row).** These are not copied. They stay in `taxonomy_centroids_retired_225`
and taxonomy rebuilds them; the walk does not log them (step 6 counts them and subtracts).

```sql
SELECT count(*) AS orphan_centroids FROM nexus.taxonomy_centroids ct
 WHERE NOT EXISTS (SELECT 1 FROM nexus.catalog_collections cc
                    WHERE cc.tenant_id = ct.tenant_id AND cc.name = ct.collection);
```

Not an abort on its own. HOLD if it exceeds FORK_ORPHAN_CENTROIDS (`.27:`) by more than DRIFT_TOLERANCE, and tell Sam
the number: it is the count of derived rows that will be left behind.

**Probe C3 (referencing rows with no registry row).** Step 4 backfills `embedding_model` from the registry row, then
sets NOT NULL. A row whose collection has no registry row stays NULL and the walk rolls back.

```sql
SELECT (SELECT count(*) FROM nexus.catalog_document_chunks m
         WHERE NOT EXISTS (SELECT 1 FROM nexus.catalog_collections cc WHERE cc.tenant_id = m.tenant_id AND cc.name = m.collection)) AS manifest,
       (SELECT count(*) FROM nexus.topic_assignments ta
         WHERE NOT EXISTS (SELECT 1 FROM nexus.catalog_collections cc WHERE cc.tenant_id = ta.tenant_id AND cc.name = ta.source_collection)) AS topics,
       (SELECT count(*) FROM nexus.chunk_orphaned_at q
         WHERE NOT EXISTS (SELECT 1 FROM nexus.catalog_collections cc WHERE cc.tenant_id = q.tenant_id AND cc.name = q.collection)) AS orphaned;
```

ABORT if any is not 0.

**Probe M (collection names against registry models).** The released client groups a search by the model token in
the collection NAME; the engine now reads the REGISTRY model and refuses a combined call whose collections disagree
(400 `mixed embedding models in one combined-query call`). A conformant name whose token differs from
`catalog_collections.embedding_model` is how a released client would reach that refusal.

```sql
SELECT count(*) AS name_model_disagrees FROM nexus.catalog_collections
 WHERE name ~ '^[a-z]+__.+__.+__v[0-9]+$' AND split_part(name, '__', 3) <> embedding_model;
```

Expect 0. HOLD if not, and tell Sam which collections. Unverified: the name grammar here is the RDR-103 shape read
from AGENTS.md; legacy names that do not match it are not counted.

**Probe O (owner of `diag_chash_conformance`).** The walk drops this view and recreates it. A superuser-owned copy is
recreated owned by the migrating role. If the migrating role owns neither the view nor the schema, the walk logs a
WARNING and carries on, and the view stays bound to the retired table until its owner runs `DROP VIEW`. The changeset
header says that branch has no test.

```sql
SELECT pg_get_userbyid(c.relowner) AS view_owner, pg_get_userbyid(n.nspowner) AS schema_owner, current_user AS this_role
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'nexus' AND c.relname = 'diag_chash_conformance';
```

Run it as the migration role (`NX_DB_ADMIN_USER`). `view_owner` equal to the migration role: pass. `view_owner`
different and `schema_owner` equal to the migration role: pass, the walk replaces it. Both different: ABORT until
conexus has run `DROP VIEW nexus.diag_chash_conformance` through its owner (the next boot's `taxonomy-011-8` recreates
it). Unverified: the owner on Crunchy.

**Probe L (lock holders).** The walk sets `lock_timeout = 120s`. A holder past that fails the walk, and one that
shows up after the copy wastes the copy.

```sql
SELECT a.pid, a.usename, a.application_name, a.state, a.xact_start, c.relname, l.mode, l.granted
  FROM pg_locks l JOIN pg_class c ON c.oid = l.relation JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_stat_activity a ON a.pid = l.pid
 WHERE n.nspname = 'nexus' AND a.pid <> pg_backend_pid()
   AND c.relname IN ('chunks','taxonomy_centroids','catalog_collections','catalog_document_chunks','topic_assignments',
                     'chunk_orphaned_at','service_tokens','live_chunks','collection_vector_stats','diag_chash_conformance');
SELECT pid, usename, application_name, state, xact_start FROM pg_stat_activity
 WHERE state IN ('idle in transaction', 'idle in transaction (aborted)') AND pid <> pg_backend_pid();
```

Run it twice: once with the engine up (to name who holds what), once after the engine container has stopped and its
backends are gone. After the stop, ABORT on any row from the first query and on any `idle in transaction` row. Also
require zero `nexus_svc`, `conexus_svc` and `conexus_cp` backends (the RDR's freeze check). `nexus_diag` and monitoring
sessions count: conexus ends them or waits. Engine backends carry `application_name = nexus-service/<release>/<8
hex>`; the engine's shutdown hook terminates only those. The migration connection does not set an application name
(`Main.buildMigrationDataSource`), so identify a walk backend by its role (`NX_DB_ADMIN_USER`), never by name.

**Probe D (free disk floor).** The engine's own local preflight (`LocalDiskPreflight`) requires free disk of at least
2.2 x `pg_total_relation_size(chunks) + pg_total_relation_size(taxonomy_centroids)`: 1x copy, 1x WAL, 0.2x headroom,
inferred and never measured. It is skipped in the cloud (`NX_PG_DATA_DIR` is unset), so this is the only cloud check.
conexus's own disk gate is regressed: `deploy/gate/disk_preflight.py` on conexus main reads host CloudWatch metrics
only and exits 2 against Crunchy (P0 fix in progress, conexus-kwlv.35). Until it lands, conexus runs the pre-kwlv.9
version (commit 15701c2 of the conexus repo, which reads the Crunchy API's `disk_available_mb`) or reads the Crunchy
dashboard. The last recorded figure is 41.9 GB free of 52 GB on 2026-10-05 (T2 conexus [29187]); read it again at
the window (T2 `nexus_rdr/225-conexus-answers` [29505], answer 6).
Step 4's UPDATE of three referencing tables rewrites every row and is not in the engine's rule; this runbook adds it.

```sql
SELECT pg_total_relation_size('nexus.chunks') + pg_total_relation_size('nexus.taxonomy_centroids') AS vector_bytes,
       pg_total_relation_size('nexus.catalog_document_chunks') + pg_total_relation_size('nexus.topic_assignments')
         + pg_total_relation_size('nexus.chunk_orphaned_at') AS referencing_bytes,
       pg_size_pretty(pg_database_size(current_database())) AS database_size;
```

`floor = 2.2 x vector_bytes + 2.0 x referencing_bytes + DISK_MARGIN`. The 2.0 is this runbook's reading of step 4 (new
tuple versions plus their WAL), not an engine rule. ABORT if free disk on the database volume, read by conexus in the
minutes before the flip, is below the floor. Placeholder `.27:` replaces both factors and DISK_MARGIN with the fork's
measured peak extra disk and WAL. Arithmetic on the RDR's own figures: 41.9 GB free (2026-10-05) against a "15 GB
copy estimate" is 8.9 GB of margin if that 15 GB is `vector_bytes`, because 2.2 x 15 = 33 GB; the RDR records its size
figures as unreconciled, so measure. After the walk the retired tables stay for 14 days: free space settles at about
`free_before - vector_bytes - (new column bytes)`, and conexus's disk alert must not fire on it. Recorded 2026-10-05
on memory-16: `max_wal_size` 5 GB (T2 conexus [29187]). Unverified: the volume's autoscaling.

**Probe W (WAL).** Inactive replication slots and a failing archiver stop WAL from recycling, so the copy's WAL piles
up on the volume.

```sql
SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained FROM pg_replication_slots;
SELECT last_archived_time, last_failed_time, failed_count FROM pg_stat_archiver;
SHOW max_wal_size;
```

ABORT if any slot is inactive and retains WAL, or if `last_failed_time` is later than `last_archived_time`. Placeholder
`.27:` WAL_PEAK is the fork's peak WAL; ABORT if `max_wal_size` is under it and the volume cannot hold it.
`pg_replication_slots` is readable by a non-superuser (measured as `nexus_svc` on 2026-07-11 and 2026-08-13, so
`nexus_admin` can read it too); the 2026-08-13 reading was 0 slots, `archive_mode=always`, `max_slot_wal_keep_size` 2 GB.
Unverified (NEEDS-LIVE-READ): `pg_stat_archiver` for `nexus_admin` (it needs `pg_monitor`, which `nexus_admin` holds
`WITH ADMIN OPTION` per the engine-redeploy skill; no recorded read), and the WAL settings after the 2026-10-05 resize.

**Probe P (pending changesets match the prediction).**

```sql
SELECT count(*) FROM public.databasechangelog WHERE id = 'vectors-030-1';          -- expect 0
SELECT relkind FROM pg_class WHERE oid = 'nexus.chunks'::regclass;                   -- expect 'r'
SELECT to_regclass('nexus.chunks_new'), to_regclass('nexus.chunks_retired_225'),
       to_regclass('nexus.taxonomy_centroids_new'), to_regclass('nexus.taxonomy_centroids_retired_225');  -- expect all NULL
```

ABORT on anything else: step 0 of the walk refuses a leftover and the layout is not what the fork saw.

Record every probe's output, with the UTC time it ran, in T2 `nexus_rdr/225-walk-rehearsal`.

## 4. The prediction to check against the fork walk (and then the live walk)

Both walks end with one engine log line (`SchemaMigrator`):

```
event=schema_migration_complete new_changesets=<n> reexecuted_changesets=<r> pending_at_start=<p> mark_ran_changesets=<m>
```

The three counts partition `pending_at_start` exactly: `new + reexecuted + mark_ran = pending`. The counts are
de-duplicated by changeset identity, so the duplicate `databasechangelog` rows production carries do not inflate them.

With the cloud at `engine-service-v0.1.149` and the tag cut from develop at or after `86896d534` (the commit that added `vectors-031`):

| Field | Predicted | Why |
| --- | --- | --- |
| `new_changesets` | 3 | `vectors-030-1`, `vectors-031-1` and `vectors-031-2`. The other changes since v0.1.149 are `db.changelog-master.xml` (the includes) and the content of the `runAlways` `grants-nexus-svc.xml`. |
| `reexecuted_changesets` | RUNALWAYS | The count of `runAlways` changesets at the tag. 12 at `engine-service-v0.1.149` and at `4fe5c20c1` (recounted for this fix round; no `runAlways` changeset was added since v0.1.149): counted by a script over those trees; 5 in `grants-nexus-diag.xml`, 5 in `grants-nexus-svc.xml`, 1 each in `staging-001-landing-tables.xml` and `taxonomy-011-doc-id-bytea.xml`. |
| `mark_ran_changesets` | 0 | A `runAlways` changeset whose precondition fails is MARK_RAN; any non-zero value names a skipped grant and must be explained. |
| `pending_at_start` | 3 + RUNALWAYS = 15 | Liquibase counts every `runAlways` changeset as unrun on every walk. |

Count RUNALWAYS at the tag, never from this table:

```bash
git grep -n -E '<changeSet [^>]*runAlways="true"' <engine-tag> -- service/src/main/resources/db/changelog/ | wc -l
```

This matches the changeset element only (comments that mention the attribute are not matched); it returned 12 at
`engine-service-v0.1.149` and at `4fe5c20c1`. A changeset whose attributes wrap onto a second line would be missed: if the number
differs from 12 at the release tag, recount by script, not by eye. Also confirm no changeset was added or removed
since `4fe5c20c1` with `git diff --stat 4fe5c20c1 <tag> -- service/src/main/resources/db/changelog/`; the expected
changeset ids at the tag are `vectors-030-1`, `vectors-031-1`, `vectors-031-2` and nothing else new.

Also expected, not anomalies:

- `event=schema_migration_pending changesets=15` before the walk (the same `pending` figure).
- `event=schema_changelog_duplicate_rows`: production carried 13 extra rows across `grants-nexus-diag-1` and `-2` on
  2026-09-14 (AGENTS.md § Engine-service release). The count may have changed; record it.
- `event=disk_preflight_skipped` (cloud has no `NX_PG_DATA_DIR`).

Any other result means the cloud is behind PREV_TAG (recount from the live `release_version`), the `runAlways` set
moved, or a second writer ran: `event=schema_migration_count_anomaly` must not appear. Stop and ask before the live
window. `counts_unavailable=true` on the complete line is not a failure of the walk (the diagnostic query failed after
the update returned), but the identity cannot be checked then; read `databasechangelog` instead.

Where the walk's own lines land. They are `RAISE NOTICE` (nexus-3wh8d.30), so they reach the engine log and not
only the PostgreSQL server log, which on Crunchy has no sink. A `NOTICE` arrives at the engine as a JDBC `SQLWarning`;
Liquibase 4.29's `JdbcExecutor` logs every warning of a statement through its own logger (`liquibase.executor`) at
level WARNING, under `liquibase.sql.showSqlWarnings`, which defaults to true and which the engine does not change.
Liquibase logs through `java.util.logging`, so the line is printed by the JUL console handler, not by the engine's
logback format, and it carries the WARNING label whatever the PostgreSQL severity was. Grep the engine log (CloudWatch
`/conexus/dev/engine`) for `rdr225`:

- `WARNING: rdr225 step 3: N chunk row(s) copied` and `WARNING: rdr225 step 3: N centroid row(s) copied`
- `WARNING: rdr225 step 6: reconciled`
- `WARNING: rdr225 walk: N relation lock(s) held at the end of the walk`

`RAISE WARNING` lines (`rdr225 step 2`, `step 7.1`) take the same path. Liquibase's per-changeset lines with their
durations also reach the engine log and CloudWatch (T2 `nexus_rdr/225-conexus-answers` [29505], answer 3). The
engine-side capture is pinned by `P225MigrationWalkIntegrationTest.theWalksCountsReachTheEngineLog`. A `NOTICE` is
below the server's default `log_min_messages`, so these lines are no longer written to the PostgreSQL server log;
do not look for them there. The server log carries `pgaudit` output and verbatim `ALTER ROLE ... PASSWORD`
statements, so never paste it raw (answer 6).

## 5. Deploy steps

conexus runs these; nexus watches and answers.

1. Pre-window: § 2 holds, § 3 passes, PREV_TAG read, and the previous image tag is recorded as the rollback target.
2. Confirm the env the new container gets (conexus owns the values; none is printed here):
   - `NX_DB_ADMIN_URL`, `NX_DB_ADMIN_USER`, `NX_DB_ADMIN_PASS`: all three or none (`Main` refuses a partial set). They
     name the migration role, which must be the schema owner. The fork must have used the same role and path.
   - `NX_OWNERLESS_WRITE_MODE`: unchanged from § 2.6.
   - `NX_PG_DATA_DIR`: unset (cloud).
   - Liquibase connects direct as `nexus_admin` (`NX_DB_ADMIN_URL` equals `NX_DB_URL`, `sslmode=verify-full`, no
     pooler: RDR-003 is deferred); the runtime role is `nexus_svc`. The fork walk reads the same secret and uses the
     same direct path, but is reached from the operator's Mac over the public host, so its wall time carries that
     round trip (T2 `nexus_rdr/225-conexus-answers` [29505], answer 3).
   - The redeploy document runs the engine with `--restart unless-stopped` and has no `HEALTHCHECK` and no automatic
     rollback. Its wait is about 90 s on `/version`, which binds only after the walk, so a legitimate walk makes the
     document report Failed while the walk continues, and nothing kills it. A failing walk exits 1, `unless-stopped`
     restarts the container, and every restart re-runs the copy and its WAL. See step 3 and step 5 for the guard.
3. Stop the engine AND the control plane before the new tag runs. The conexus database shares the Crunchy cluster
   (database `conexus` beside `nexus`), and conexus's `RESTORE.md` repoints both secrets, so a restore to T0 rewinds
   control-plane writes made after T0 (usage and billing rows, token mints and revocations) unless the control plane
   is stopped with the engine (T2 `nexus_rdr/225-conexus-answers` [29505], answer 1). The redeploy document cannot be
   paused between its stop and its run: one script removes the sidecar, runs `docker stop -t 30`, removes the
   container and runs the new one, back to back (answer 2). So the window stops them by hand first, through
   conexus-RunShell, with Sam's go as for every other step of this runbook:
   `docker rm -f conexus-engine-tls; docker stop -t 30 conexus-engine` (stop, do not remove: the document's own stop
   is then a no-op, and its image prune keeps the stopped container's image), plus `docker stop conexus-controlplane`.
   The cost is that the document's image pull and signature check now fall inside the outage. Confirm no
   `nexus-service/...`, `conexus_svc` or `conexus_cp` backends remain, then run Probe L's second pass.
4. **Capture the restore point from the database AFTER both stops return, as an RFC3339 UTC timestamp.** Run
   `SELECT now() AT TIME ZONE 'utc';` on the primary and keep it as `YYYY-MM-DDTHH:MM:SSZ`. Crunchy's fork recipe takes
   `target_time` as RFC3339 and has no LSN option (`RESTORE.md`, answer 5), so an LSN is not a restore point here. T0 is
   this value. Put it in the window log and in T2 `nexus_rdr/225-walk-rehearsal` before the new container starts.
   Nothing writes to either database between the stops and the walk, so a target at T0 loses nothing on either side.
5. Send the redeploy document for the new tag; it runs `docker run`. Start the clock. **Immediately run
   `docker update --restart=no conexus-engine`** through conexus-RunShell (Sam's go, as above), so a failing walk
   stays stopped instead of looping through restarts; restore `unless-stopped` only after a clean boot. The document
   will report Failed after its 90 s wait while a legitimate walk is still running: that is expected, not an abort.
   The proper guard, a healthcheck and rollback in the document itself, is conexus bead conexus-6d2n; until it lands
   this step is manual.
6. Watch the engine log for, in order: `schema_migration_start`, `schema_migration_session`,
   `schema_migration_pending changesets=15`, then either `schema_migration_complete` (§ 4) or
   `schema_migration_failed`. After complete: `partition_access_drift_repaired` (WARN) means the owner repair ran;
   `chunks_isolation_check_failed` or `root_token_seed_*` are exits too.
7. Walk time cap: the rehearsed time, FORK_WALL_TIME (`.27:`), times CAP_FACTOR (`.27:`), and never past the budget
   Sam set for the window (default about 15 minutes). At the cap: do not kill the container. Ask Sam to extend or abort.
   A kill before commit rolls back, so it is safe for data, but the migration backend can keep running and holding locks
   after its client is gone (the server notices a closed socket only when it next reads or writes). Find it by role,
   confirm it is the walk (`state = 'active'`, a `chunks_new` statement, `xact_start` at the walk's start), and have
   conexus terminate it with `pg_terminate_backend` before any restart. Unverified: this behaviour on Crunchy.
8. After `schema_migration_complete` and a bound HTTP port, and before § 7.3's frozen-query run: step 9, then § 7.
9. **Post-boot VACUUM, run by conexus right after the engine is up** (nexus-3wh8d.17 code review S1, .18 critique 2).
   The walk is one transaction and cannot VACUUM, and its step 5 ends with ANALYZE only, so every new leaf is loaded,
   analyzed and never vacuumed: its visibility map is empty. Run, as the table owner (`nexus_admin`), one statement
   per parent (VACUUM cannot run inside a transaction block, and on a partitioned parent it recurses into every
   model partition and leaf):

   ```sql
   VACUUM (ANALYZE) nexus.chunks;
   VACUUM (ANALYZE) nexus.taxonomy_centroids;
   ```

   Expected duration: placeholder `.27:` VACUUM_WALL_TIME (the fork measures it on the walked layout, per parent, with
   VACUUM_PEAK_EXTRA_IO if the volume's throughput is metered). It runs while the engine serves and takes no lock that
   blocks reads or writes (`VACUUM` takes SHARE UPDATE EXCLUSIVE); it competes for I/O, so conexus starts it in the
   first minutes and reads `pg_stat_progress_vacuum` rather than assuming. `nexus_svc` holds MAINTAIN on the parents
   and on the leaves it was granted (`grants-005`, `partition_sync_access`), so a role that is not the owner may also
   run it; Unverified: whether PostgreSQL 17 on Crunchy accepts MAINTAIN on the parent alone for the recursion (it
   checks each partition), so default to the owner. Confirm with
   `SELECT relname, last_vacuum FROM pg_stat_user_tables WHERE schemaname = 'nexus' AND relname LIKE 'chunks%'`: every
   leaf shows a `last_vacuum` after the walk. Autovacuum would do the same, cost-throttled, across all tenants, minutes
   to hours later; this step removes the wait.

   **What searches do until it has run.** The cardinality router (`NX_SEARCH_EXACT_MAX_ROWS`, default 10,000, on by
   default) runs a probe before every plain search: it counts up to 10,001 rows of the (model, tenant, collections)
   selection in that one leaf (`PgVectorRepository.probeSelectedRowsQuery`). On a vacuumed leaf the planner reads those
   rows from the primary-key prefix as an Index Only Scan. On an unvacuumed leaf the same statement took a Seq Scan at
   a 26 percent share (measured in the P0.2 prototype and in the Phase 2 pin work; the cause is the empty visibility
   map, an inference from those runs and not verified on a production-shaped leaf). So until VACUUM runs, each plain
   search over a collection that is more than about a quarter of its leaf pays that scan before the HNSW or exact plan
   starts. Arithmetic, not measured: at a 26 percent share the scan reads about 40,000 rows (10,001 divided by 0.26)
   when the matches are spread through the heap. The worst case is a collection indexed in one batch, whose rows sit
   together late in the heap: the scan then reads every earlier page of the leaf before it finds its 10,001 rows, up
   to most of a multi-gigabyte leaf per search, until the search hits `NX_SEARCH_STATEMENT_TIMEOUT_MS` (default
   30,000 ms; SQLSTATE 57014). Results stay correct; the cost is latency and, at the worst, timeouts, on the largest
   collections, in the window the deploy is being watched.

   **Soak option.** If the VACUUM cannot start at once, set `NX_SEARCH_EXACT_MAX_ROWS=0` in the engine's environment
   before the first boot (or restart the container with it; the value is read once at start-up and logged as
   `event=search_exact_router max_rows=0`). Threshold 0 turns the router off: no probe runs and every plain search
   takes the HNSW path, which is how the engine behaved before the router existed (with the exact fallback on an
   empty or short result). Remove the variable and restart once
   the VACUUM has finished, or leave it for the soak and record that in T2. Unverified: HNSW-only latency on the
   walked layout; the rehearsal measures it.

## 6. Abort and rollback

### 6.1 Decide from the database, not from the absence of a log line

The tempting rule is "no `schema_migration_complete` means the walk rolled back". It is wrong. That line prints only
after `liquibase.update()` returns, that is, after every changeset in the changelog ran. Liquibase commits each
changeset in its own transaction (no `runInTransaction` override in `db.changelog-master.xml`,
`vectors-030-model-tenant-partition-functions.xml` or `grants-nexus-svc.xml`), and `vectors-030-1` runs before the
`runAlways` grants. So `vectors-030-1` can commit, a later changeset can fail, the engine exits 1 with
`schema_migration_failed`, and there is no `schema_migration_complete`: a committed, irreversible walk with no success
line. A tag flip then puts the previous engine on the new layout. Boot can also exit 1 after the complete line
(`chunks_isolation_check_failed`, `root_token_seed_*`).

The authoritative state is one read-only query, run by conexus:

```sql
SELECT (SELECT count(*) FROM public.databasechangelog WHERE id = 'vectors-030-1') AS walk_row,
       (SELECT relkind FROM pg_class WHERE oid = 'nexus.chunks'::regclass) AS chunks_kind,
       to_regclass('nexus.chunks_retired_225') AS retired_chunks;
```

| `walk_row` | `chunks_kind` | `retired_chunks` | State |
| --- | --- | --- | --- |
| 0 | `r` | NULL | Not committed. The walk rolled back, or never started. |
| 1 | `p` | `chunks_retired_225` | Committed. |
| anything else | | | Stop. Do not touch anything. Ask Sam and conexus. |

Read the engine's own error text beside it: `schema_migration_failed error="..."` carries Liquibase's message, which
names the failing changeset id. That tells you which branch of § 6.2 or § 6.3 you are in; it does not decide between
them.

**Failure points after `vectors-030-1` has committed.** Each of these leaves the walk committed (§ 6.3, no tag flip)
and the engine exiting 1. A failed changeset rolls back its own transaction only, so the next boot resumes at it. The
remedy is fix-forward.

| Where | Message | What it means and the remedy |
| --- | --- | --- |
| `vectors-031-1` post-condition | `vectors-031-1: expected exactly 30 read-path functions (one signature each), found N: ...` | A read-function name still has a second signature after the drops: a hand-made overload the changeset does not know. The message lists every signature found. As the owner, `DROP FUNCTION` the stale one (the old ones carry no `p_embedding_model`), then restart the engine. Before the window: `SELECT proname, pg_get_function_identity_arguments(oid) FROM pg_proc WHERE pronamespace = 'nexus'::regnamespace AND proname ~ '_[0-9]{3,4}$'` should show one row per name. |
| `vectors-031-2` post-condition, signatures | `vectors-031: expected exactly 3 read-path functions (one signature each), found N: ...` | The same, for `text_gate_probe_384`, `_768` and `_1024`. Same remedy. |
| `vectors-031-2` owner assertion | `vectors-031-2: probe(s) ... are SECURITY DEFINER but not owned by the migrating role` | A definer probe is owned by a different role than the one migrating. The changeset recreates the probes as the migrating role, so this fires only when that did not happen. Remedy: `ALTER FUNCTION nexus.text_gate_probe_<dim>(text, text[], jsonb, text, int, text, text) OWNER TO <migrating role>`, or drop the probes and restart. Before the window: `SELECT proname, proowner::regrole, prosecdef FROM pg_proc WHERE proname LIKE 'text_gate_probe_%'`. |
| `vectors-031-2` guard assertions | `vectors-031-2: policy chunks_gate_probe_owner_read must name exactly the migrating role ...` or `vectors-031-2: role(s) ... have the privileges of the migrating role ...` | The copy of `vectors-029`'s post-condition. The changeset chooses the invoker branch whenever the policy does not name the migrating role or a login role subject to RLS inherits that role, so these fire only when the state moves during the walk. Remedy: put the policy or the membership right (the second message names the roles), restart. Do not weaken the check. |
| After the complete line | `event=chunks_isolation_check_failed` (a policy applies to the service role) or `event=chunks_isolation_structure_gaps` (a parent, model partition or leaf does not mirror its parent) | The engine refuses to serve and exits 1; under `--restart unless-stopped` the container loops until someone repairs it (§ 5.5 sets `--restart=no` for the first boot). **Drift on a model partition or leaf no longer loops here.** The migration step re-mirrors both parents onto their trees at every boot as the schema owner and logs `event=partition_access_drift_repaired count=N relations=[...]` at WARN when it had work to do (a healthy boot logs `event=partition_access_checked` at INFO and changes nothing). Read that WARN: it names the relations that drifted, and a repaired relation is a finding to explain (who created or edited it), not noise. The manual remedy remains only for drift the copy cannot fix, which is logged as `event=partition_access_drift_unrepaired` (ERROR, naming the relations and the problems) just before the refusal: a wrong policy or flag on the PARENT, or a migrating role that does not own the tables or the function. **Remedy, as the table owner (`nexus_admin`; the engine's runtime role cannot do this):** put any wrong policy on the PARENT right first (the function copies from it), then `SELECT nexus.partition_sync_access('nexus.chunks'::regclass);` and `SELECT nexus.partition_sync_access('nexus.taxonomy_centroids'::regclass);`. For a policy violation (`chunks_gate_probe_owner_read` applying to the service role): `DROP POLICY chunks_gate_probe_owner_read ON nexus.chunks` and then `partition_sync_access` for both parents (dropping it on the parent alone leaves its copy on every leaf, and the next boot refuses on each), or `REVOKE` the role membership. Then `docker start conexus-engine`. |
| After the complete line | `root_token_seed_*` | Token seeding failed; unrelated to the layout. Read the line. |

Rehearsal assertion (nexus-3wh8d.27): the engine on the fork, migrated by the production-shaped non-superuser role
(`nexus_admin`), boots through `ChunksIsolationCheck` and logs `event=chunks_isolation_check_ok`. A rehearsal that
migrated as a superuser proves nothing about this (a superuser holds USAGE on every role, so `vectors-029`'s
precondition would MARK_RAN there and the owner policy would never exist).

### 6.2 Not committed: a tag flip is safe

The walk is one transaction, so a failure inside it leaves the old layout intact.

1. Make sure no backend of the failed or killed engine is still running (§ 5.7). A leftover holding the walk's locks
   would block the next boot.
2. conexus redeploys PREV_TAG (the `release_version` read in § 2; expected `engine-service-v0.1.149`) with the previous
   image. Flip the SSM image tag `/conexus/dev/engine/image-tag` back to PREV_TAG first, so no redeploy and no fresh
   boot picks the new tag up. The document's 90 s wait does not kill a legitimate walk, but a failing walk under
   `--restart unless-stopped` loops, and each loop re-runs the copy and its WAL, which is why § 5.5 sets
   `--restart=no` (answer 2).
3. Verify with `nx service probe` (live `/version`), then `NX_EXPECTED_OWNERLESS_WRITE_MODE=<the live mode>
   tests/e2e/cloud-client-path-gate.sh`.
4. Record the failure text and the probes in T2, find the cause, and cut a new tag. Nothing about the data changed.

### 6.3 Committed: a tag flip is FORBIDDEN

Why. The previous engine's write sites use the three-column key: `ON CONFLICT (tenant_id, collection, chash)` and
inserts that omit `embedding_model`. After the walk the primary key of `chunks` is `(tenant_id, collection, chash,
embedding_model)` and `embedding_model` is NOT NULL, so those writes fail. Reads still work, which hides it: the
engine boots, `/version` and `/health` look healthy, and every write errors. The previous engine's `runAlways`
changesets would also run against the new layout; that is not rehearsed. The Liquibase `<rollback>` of `vectors-030-1`
is not a path either: it restores table shape only, leaves the redefined function bodies in their post-walk form,
discards every row written since the walk, and the engine never calls it.

The choices, both on Sam's go typed in conexus's session:

- **Fix forward.** Use it when the walk committed and the failure is later and small (a `runAlways` grants changeset,
  the isolation check, token seeding). A `runAlways` changeset's content may be corrected in a new tag; any other
  change needs a new changeset (released changesets are checksum-pinned). It needs an engine cut, the engine-release
  skill, a human push of the tag and a conexus deploy while the cloud is down. Sam sets the time he will wait.
- **PITR restore to T0** (the timestamp captured in § 5.4), then PREV_TAG. Use it when the committed walk is wrong
  (reconciliation passed but results fail, an unfixable post-condition), or when fix-forward would run past Sam's
  patience. Writes made after T0 are lost: with the engine and the control plane both stopped before T0 (§ 5.3), there
  are none. The control plane's database shares the cluster, so a restore that skipped that stop would rewind its
  writes too (§ 9.2 answer 1).

### 6.4 PITR sequence (conexus-owned; the shape, not commands)

Production is Crunchy Bridge on the deploy date (`conexus-dev`, memory-16, 50 GB since 2026-10-05); the pgBackRest
path of conexus RDR-007 has no production runbook yet. The recipe is conexus's `deploy/RESTORE.md`, as relayed in T2
`nexus_rdr/225-conexus-answers` [29505] (answer 5; the file itself is not read here). A Crunchy PITR is a FORK into a
new cluster, not an in-place rewind: `POST /clusters/$CID/forks` with the plan, storage, `network_id` and `target_time`
as RFC3339, ready in about 6 minutes, then both Secrets Manager secrets are repointed (`conexus/dev/engine-db` and
`conexus/dev/controlplane-db`) to the fork's host and the engine and edge are replaced with `terraform apply -replace`
(run terraform-apply-safety first). The old cluster survives until conexus deletes it, so writes after T0 stay
recoverable from it.

1. Sam's go for the restore, in conexus's session. Record the time of the go.
2. Keep the engine and the control plane stopped (§ 5.3), so nothing writes to either database while the fork is made.
3. Fork to T0, the RFC3339 UTC timestamp from § 5.4. The fork replaces the database; the retired tables and the
   committed walk are not in it. Confirm the fork's `network_id` equals the live one.
4. **Flip the SSM image tag `/conexus/dev/engine/image-tag` back to PREV_TAG BEFORE any engine replacement.** A fresh
   boot reads that parameter, so replacing the engine first would start the new tag on the restored database. Then
   repoint both secrets at the fork, which moves the engine and the control plane together.
5. Deploy PREV_TAG with its previous image, and re-run the redeploy document after the replacement so
   `NX_INSTALL_PING_TRUSTED_PROXIES` is rewritten. **Not the new tag.** The restored database is on the old layout, and
   the new tag would walk it again. The previous tag is PREV_TAG as read in § 2, expected `engine-service-v0.1.149`.
6. Verify: `nx service probe` shows PREV_TAG; `public.databasechangelog` has no `vectors-030-1`; `nexus.chunks` is
   `relkind = 'r'`; `NX_EXPECTED_OWNERLESS_WRITE_MODE=<the live mode> tests/e2e/cloud-client-path-gate.sh` is green.
7. If the floor-bumped client has already shipped, local installs are pinned to the abandoned tag. A tag cannot be
   moved (`docs/contributing.md` § Break-glass: tag retraction): a new engine tag and a new client are the way out. This
   is why § 2.7 recommends engine first, soak, then client.
8. Record in T2 what was lost, the times, and the cause. Open a bead for the fix before the next attempt.

## 7. Post-deploy verification

Run from a cloud-mode box, a few minutes after the engine reports bound (leg J of the gate fails inside the first
minute after boot). This is the engine-release Step 5b.4 bar: a matching `/version` proves the binary shipped, not
that the data survived.

1. **Row reconciliation, per table, not an aggregate.** conexus runs, for `chunks` and `taxonomy_centroids`:

   ```sql
   SELECT embedding_model, tenant_id, count(*) FROM nexus.chunks GROUP BY 1, 2 ORDER BY 1, 2;
   ```

   and compares with the same grouping taken before the flip, for the old table
   (`JOIN nexus.catalog_collections` for the model). Centroids: old count minus Probe C2's count equals new count. The
   per-table row counts of `catalog_document_chunks`, `topic_assignments` and `chunk_orphaned_at` equal their pre-flip
   counts. Zero tolerance: the walk's own step 6 reconciled the copy; this checks the live state after boot. Take the
   "before" counts in the same window as Probe V. Under RLS the same per-tenant caveat as § 3 applies.
2. **VACUUM ran, and the probe on a leaf before and after.** § 5.9's VACUUM finished and every leaf shows a
   `last_vacuum` after the walk. Record, from the fork rehearsal (nexus-3wh8d.27), the router probe's latency and its
   plan on one large leaf BEFORE the VACUUM and AFTER it (`EXPLAIN (ANALYZE, BUFFERS)` of the statement
   `probeSelectedRowsQuery` builds: `SELECT count(*) FROM (SELECT 1 FROM nexus.chunks WHERE embedding_model = <model>
   AND tenant_id = <tenant> AND collection = ANY(<names>) LIMIT 10001) probe`): placeholders `.27:`
   PROBE_MS_BEFORE_VACUUM, PROBE_PLAN_BEFORE_VACUUM, PROBE_MS_AFTER_VACUUM, PROBE_PLAN_AFTER_VACUUM, on the
   collection with the largest share of its leaf. If the unvacuumed probe is not bounded well under
   `NX_SEARCH_STATEMENT_TIMEOUT_MS`, the soak runs with `NX_SEARCH_EXACT_MAX_ROWS=0` (§ 5.9). Then the ANALYZE check:
   every leaf, both parents and the three referencing tables were analyzed in step 5.
   `SELECT relname, last_analyze FROM pg_stat_user_tables WHERE schemaname = 'nexus' AND (relname LIKE 'chunks%'
   OR relname LIKE 'taxonomy_centroids%')` shows a `last_analyze` inside the walk's window for every leaf. A leaf without
   statistics turns the planner off HNSW (vectors-004 Step 5b, BUG-0148). Unverified: the view shows partitioned
   parents' statistics as expected.
3. **The frozen query set on the live engine.** Placeholder `.27:` FROZEN_QUERY_SET names the set (real logged
   queries) and its runner; the same set ran on the fork before and after. The pass rule is fixed before the fork run,
   not here: top-k overlap floor against the pre-walk results, a latency ratio bound, and a cache protocol (restart
   and flush before each run) (critique, Significant 3). Placeholder `.27:` OVERLAP_FLOOR, LATENCY_RATIO. Each plan
   touches one (model, tenant) leaf.
4. **The client-path gate**, with the live mode named:

   ```bash
   NX_EXPECTED_OWNERLESS_WRITE_MODE=log-only tests/e2e/cloud-client-path-gate.sh
   ```

   `log-only` is right only if § 2.6 read `log-only`; use the mode conexus reported. Run plain, it fails leg B3 on any
   engine that reports a mode. Must end with its pass sentinel and `violations=0` with B3 asserted.
5. **The deploy gate's parity and recall legs** (engine-release Step 5b.4), run by conexus.
6. **Doctor rows, and what the cloud has instead.** The tenant, model and leaf rows of `nx doctor` (nexus-3wh8d.16) read
   the database through local admin credentials, so on a cloud-mode box they report "not applicable"; the engine runs
   no equivalent. Its boot check (`ChunksIsolationCheck`) covers the RLS structure of both trees and nothing about
   token tenants or registered models, and the engine's `/v1/status` field `chunks_tenant_isolation_intact` mirrors
   that. A tenant with a token and no leaf is otherwise found by a 500 `tenant_partition_missing` on its first write.
   So conexus runs this read-only query after the walk, and after any tenant mint, and expects NO `T` and NO `M` rows:

   ```sql
   WITH parents AS (
       SELECT c.oid, c.relname::text AS parent
         FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'nexus' AND c.relkind = 'p' AND c.relname IN ('chunks', 'taxonomy_centroids')
   ), mp AS (
       SELECT p.parent, i.inhrelid AS oid,
              replace((regexp_match(pg_get_expr(mc.relpartbound, mc.oid), '^FOR VALUES IN \(''(.*)''\)$'))[1], '''''', '''') AS model
         FROM parents p JOIN pg_inherits i ON i.inhparent = p.oid JOIN pg_class mc ON mc.oid = i.inhrelid
   ), lf AS (
       SELECT mp.parent, mp.model,
              replace((regexp_match(pg_get_expr(lc.relpartbound, lc.oid), '^FOR VALUES IN \(''(.*)''\)$'))[1], '''''', '''') AS tenant
         FROM mp JOIN pg_inherits l ON l.inhparent = mp.oid JOIN pg_class lc ON lc.oid = l.inhrelid
   ), tt AS (SELECT DISTINCT tenant_id FROM nexus.service_tokens)
   SELECT 'T' AS kind, tt.tenant_id AS subject, mp.parent, string_agg(mp.model, ',' ORDER BY mp.model) AS detail
     FROM tt CROSS JOIN mp
    WHERE NOT EXISTS (SELECT 1 FROM lf WHERE lf.parent = mp.parent AND lf.model = mp.model AND lf.tenant = tt.tenant_id)
    GROUP BY tt.tenant_id, mp.parent
   UNION ALL
   SELECT 'M', em.embedding_model, p.parent, ''
     FROM nexus.embedding_models em CROSS JOIN parents p
    WHERE NOT EXISTS (SELECT 1 FROM mp WHERE mp.parent = p.parent AND mp.model = em.embedding_model);
   ```

   `T` is a token tenant missing leaves under those model partitions (recovery: `nexus.create_tenant_partitions`, as
   the table owner); `M` is a registered model without a partition. It is the doctor rows' own SQL
   (`_PARTITION_COMPARE_SQL` in `src/nexus/health.py`) without the two count rows; `service_tokens` carries
   row-level security, so read it as a role that bypasses it or under each tenant, as in § 3. The leaf count is
   `2 x models x tenants` (`pg_inherits` children of the model partitions of both parents).
7. **Engine logs clean.** No `chunks_isolation_check_failed`, no `partition_access_drift_repaired` or
   `partition_access_drift_unrepaired`, no `schema_migration_count_anomaly`, no `root_token_seed_*` after the
   complete line.
8. Record the actual walk time, the observed lock count, the peak disk and WAL, and every output above in T2
   `nexus_rdr/225-walk-rehearsal`, with the deploy date (Phase 3 Step 3's 14-day gate reads it). Then, and only then,
   the paired client release may go (§ 2.7), and `scripts/check_engine_release_floor.py` must pass.

## 8. Local installs

**There is no rollback for a local install.** The engine a local install runs is the one `REQUIRED_ENGINE_VERSION`
names. A local walk that fails leaves the old layout (one transaction) and the install down until an engine that works
is installed; the client pin converges on whatever the installed client names. Walking installs back to an earlier
engine needs a new client that moves the pin backward (`docs/contributing.md` § Break-glass, engine-deploy revert), and
a committed local walk is not undone by that: the previous engine fails on the new layout exactly as in § 6.3, and
the retired tables hold the only copy of the old layout.

- The engine refuses to start when free disk is below 2.2 x the vector tables (`LocalDiskPreflight`, local installs
  that set `NX_PG_DATA_DIR`), naming the shortfall. That is the local abort criterion; it is inferred, not measured
  (`.27:` local-install walk at a stated seed count replaces it, or says it is toy scale).
- A chunk whose vector dimension disagrees with its collection's model fails the walk at boot. No legacy shape is
  supported (Sam, 2026-10-06); the user must reindex or delete the collection. Unverified: whether the SQL error
  reaches the user through the upgrade finish pass.
- `chunks_retired_225` and `taxonomy_centroids_retired_225` are kept 14 days as a data-recovery source, then a later
  changeset drops them (Phase 3 Step 3). **That window does not exist for every install**: an install that skips the
  intermediate release walks `vectors-030-1` and the later drop changeset in one boot, and the retired tables are
  gone before anyone could use them. State this in the P3.3 changeset comment and in the CHANGELOG of the release that
  carries the drop; the drop changeset needs its own `DATA EFFECT:` line and a fork walk.

## 9. Placeholders and open questions

### 9.1 Placeholders the rehearsal (nexus-3wh8d.27) fills

Filled 2026-10-07 from the PITR-fork jar walk of develop `93562b96a` (conexus, Sam's go; record T2
`nexus_rdr/225-rehearsal-2026-10-07` [29540], disk floor [29529]). Every `.27:` marker in this runbook refers to the
Value column below. CAP_FACTOR, DRIFT_TOLERANCE, DISK_MARGIN and the frozen-query pass rule are decisions, not
measurements; they are marked as such.

| Name | Used in | Filled from | Value (fork, 2026-10-07) |
| --- | --- | --- | --- |
| FORK_WALL_TIME, CAP_FACTOR | § 5.7 | Fork walk wall time; the cap is a multiple chosen before the live run | 449 s JVM start to `schema_migration_complete` (vectors-030-1 435.7 s), run over the public host. Decision: CAP_FACTOR 2, so the cap is 15 min, inside the window budget. |
| FORK_TENANT_COUNT | § 3 Probe T | Tenant count on the fork | 4 tenants, 4 models, 32 leaves (16 per parent), as predicted. |
| FORK_RELATION_LOCKS, LOCK_HEADROOM | § 3 Probe T | The fork's `rdr225 walk: N relation lock(s)` line (engine log, § 4) | 979 locks (about 30.6 per leaf) against 32,000 slots (`max_locks_per_transaction` 64 x `max_connections` 500, live). |
| FORK_ROW_COUNTS, DRIFT_TOLERANCE | § 3 Probe V, C2 | Fork census; the drift the live estate is allowed since the fork | chunks 434,617; catalog_collections 163; catalog_document_chunks 467,471; topic_assignments 433,293; chunk_orphaned_at 4,308; taxonomy_centroids 804. Decision: DRIFT_TOLERANCE 10% per count. |
| FORK_ORPHAN_CENTROIDS | § 3 Probe C2 | Probe C2 run on the fork | 0. |
| FORK_PEAK_EXTRA_DISK, DISK_MARGIN, the factors 2.2 and 2.0 | § 3 Probe D | Peak extra disk on the fork; replaces the inferred factors | Database 9.92 GB to 17.34 GB (+7.43 GB, kept for the 14-day window), plus up to 6.39 GB WAL: 13.8 GB worst case. Decision: keep the live gate at 33,792 MB free (2.45x the worst case); live read 42,242 MB free at 11:54Z. Read the gate BEFORE stopping the engine: the Crunchy API's `disk_used` froze mid-walk on the fork and cannot monitor during it. |
| WAL_PEAK | § 3 Probe W | Peak WAL on the fork against `max_wal_size` | 6.39 GB to complete, 7.05 GB to the harness end; above `max_wal_size` 5 GB, so checkpoints and archiving carry it (archiver healthy, no slots). |
| VACUUM_WALL_TIME, VACUUM_PEAK_EXTRA_IO | § 5.9 | `VACUUM (ANALYZE)` of both parents on the fork, after the walk | chunks 2.8 s, taxonomy_centroids 0.1 s, measured after autovacuum had already run (a lower bound). Not metered. |
| PROBE_MS_BEFORE_VACUUM, PROBE_PLAN_BEFORE_VACUUM, PROBE_MS_AFTER_VACUUM, PROBE_PLAN_AFTER_VACUUM | § 7.2 | The router probe on the largest-share leaf of the fork, before and after the VACUUM | Before: not observable. Autovacuum processed every populated leaf 43 to 49 s after commit, before the first probe. After: Index Only Scan on the leaf primary key, Heap Fetches 0, 2.56 to 2.58 ms server-side, at 25.8% and 32.9% scope shares. The § 5.9 exposure is about 45 s on this estate. |
| FROZEN_QUERY_SET, OVERLAP_FLOOR, LATENCY_RATIO | § 7.3 | The set, its runner, and the pass rule, all fixed before the fork run | The set conexus ran: 3 queries x {voyage-code-3, voyage-context-3} x {nexus, gate-xr789}, all 200 with 10 results, 0.9 to 2.5 s client-side including Voyage, identical before and after VACUUM. Decision: the live pass rule is the same 12 searches, all 200 with 10 results, client latency at most 2x the fork's. A minilm-384 collection answers 422 in cloud mode by design. |
| maintenance_work_mem, parallel workers | § 7 record | The changeset sets neither; record the live values and the index build time | `maintenance_work_mem` 655 MB, 2 parallel maintenance workers (fork). The HNSW build logged `hnsw graph no longer fits into maintenance_work_mem after 142947 tuples`: harmless, a timing factor already inside FORK_WALL_TIME. |
| local-install seed count | § 8 | A local walk at a stated seed count | Not measured on a real local estate. The walk is exercised at test-fixture scale (P225MigrationWalkIntegrationTest, 12 chunks, 5 tenants) and by the local preflight; a local install's estate is far smaller than the cloud's. |

Operator notes from the rehearsal: conexus's `walk.sh` exits 1 on this walk because its relfilenode compare flags the
rebuilt `chunks`; that is expected for vectors-030-1 and not a failure. Judge the walk by § 4 and § 6.1, not by that exit
code. `nexus_diag` reading nothing from `catalog_collections`, `taxonomy_centroids`, `chunk_orphaned_at`,
`service_tokens` or `chunks_retired_225` is the RDR-182 content boundary (`grants-nexus-diag.xml`), not a lost grant.

### 9.2 Questions for conexus

Answered by conexus on 2026-10-07 (T2 `nexus_rdr/225-conexus-answers` [29505], read from conexus main and T2 only; no
live reads):

1. The control-plane database shares the cluster a PITR forks, and `RESTORE.md` repoints both secrets. The window stops
   `conexus-controlplane` with the engine and T0 is taken after both stops (§ 5.3, § 5.4, § 6.4).
2. The redeploy document: `--restart unless-stopped`, no `HEALTHCHECK`, no automatic rollback, a 90 s `/version` wait
   that reports Failed while the walk continues, and stop and run back to back with no pause. The window guard is
   manual (§ 5.3, § 5.5); the proper guard is conexus bead conexus-6d2n.
3. Liquibase connects direct as `nexus_admin`, `verify-full`, no pooler (§ 5.2).
5. Production is Crunchy; the fork recipe takes an RFC3339 `target_time` and no LSN (§ 5.4, § 6.4).
6. Disk: conexus's gate exits 2 against Crunchy until conexus-kwlv.35 lands (§ 3 Probe D). The Crunchy server log has
   no sink; `pg_replication_slots` is readable without superuser (§ 3 Probe W, § 4).
8. `release_version` 0.1.149 and `ownerless_write_mode` `log-only`, recorded 2026-10-06 (T2 conexus [29413]). No later
   deploy or flip is recorded; re-read both at the window (§ 2 requires it).

Still open, each a NEEDS-LIVE-READ that needs Sam's go and belongs to the `.27` rehearsal pass:

4. Whether `diag_chash_conformance` is owned by a superuser on Crunchy, and whether the migration role owns the `nexus`
   schema. Expected `nexus_admin` for both (taxonomy-011 recreates the view Liquibase-owned), never read on production
   since v0.1.77. Do not run `provision_diag_path.py` against production for this: it assumes a superuser-owned view.
6. (part) `pg_stat_archiver` for `nexus_admin`, and the WAL settings after the 2026-10-05 resize.
7. The live `max_locks_per_transaction`, `max_connections` and `max_prepared_transactions`.

## 10. Facts not verified

- The live release_version and ownerless-write mode (recorded 2026-10-06, to be re-read at the window), lock settings,
  disk, WAL and archiver state, and the owner of `diag_chash_conformance` and of the `nexus` schema (§ 9.2, still open).
- That the `rdr225` lines appear in CloudWatch `/conexus/dev/engine` on Crunchy. The test pins the engine-side
  capture against a local PostgreSQL; conexus's relay is that the engine log ships there. Confirm on the fork run.
- That a killed engine leaves its migration backend running until the socket read fails (PostgreSQL behaviour, not
  tested here).
- That the previous engine on the new layout fails the way § 6.3 says (read from the write sites and the new primary
  key, per the critique; not run).
- The `runAlways` count at the release tag (12 at v0.1.149 and at `4fe5c20c1`, counted by a script over those trees).
- That an unvacuumed leaf is the cause of the Seq Scan the router probe took at a 26 percent share (inferred from
  the prototype and the Phase 2 pin; the 40,000-row and worst-case figures in § 5.9 are arithmetic, not measurements).
- The 27 locks per leaf (the RDR's test layout) and the 2.0 factor for the referencing tables (this runbook's reading).
- The conexus restore recipe and the redeploy document (conexus repo; known here only through the 2026-10-07 answers
  in T2 `nexus_rdr/225-conexus-answers`, read by conexus from its repo, not read here).

## References

- RDR: `docs/rdr/rdr-225-vector-tables-per-embedding-model.md` (Failure and rollback; Phase 3 Step 1).
- Changeset: `service/src/main/resources/db/changelog/vectors-030-model-tenant-partition-functions.xml`.
- Boot order: `service/src/main/java/dev/nexus/service/Main.java`; log events: `.../db/SchemaMigrator.java`;
  local disk rule: `.../db/LocalDiskPreflight.java`.
- Skill: `.claude/skills/engine-release/SKILL.md` Steps 5b, 6, 6.1. Rationale: `docs/contributing.md` § Schema/data-migration releases.
- T2: `nexus_rdr/225-deploy-shape-review-by-eae`, `nexus_rdr/225-cloud-topology`, `nexus_rdr/225-conexus-answers`.
- Beads: nexus-3wh8d.26 (this runbook), .27 (rehearsal measurements), .20 (rehearsal, tag, deploy), .16 (doctor rows).
- Sibling runbooks: `rdr-191-phase5-cloud-fk.md` (shape), `rdr-225-tenant-removal.md`.
