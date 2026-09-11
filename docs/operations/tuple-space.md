# Tuple space runbook

Operator's guide to `nx doctor`'s three RDR-205 tuple-space rows, the sweep's
own log record, dead-letter triage, and what survives a restore. Background
and the full operational reference: [Tuple Space](../tuple-space.md);
scenario diagrams: [Tuple Space Walkthroughs](../tuple-space-walkthroughs.md).

All three doctor rows below are gated the same way against the engine
floor that first serves `/v1/tuples` (`engine-service-v0.1.114`): an
install pinned below that floor reports the row as informational, never a
defect; an install at or above the floor that still 404s or reports no
tables reports UNKNOWN and asks you to investigate the engine install.

## `tuples.oldest_unclaimed`

**Symptom**: `N subspace(s) with an unclaimed tuple older than 1h: <subspace> (<age> old, id=<prefix>)`.

**Cause**: a stuck or absent consumer. The row reads the oldest **claimable**
tuple per subspace — live, unclaimed, not dead-lettered — over the first
page (`QUOTAS.MAX_QUERY_RESULTS`, 300 rows) of each subspace; a subspace
whose oldest unclaimed tuple falls outside that first page is a known,
documented limitation of a census check, not a silent miss. Subspaces whose
template disables `take` (`ledger/<session_id>`) are skipped outright —
"oldest unclaimed" carries no signal on a read-only template.

**Action**: `nx tuple rd <subspace>` to see what's sitting there, then check
whether the expected consumer is running. For `mailbox/<address>`, confirm
the addressed agent or instance is still alive and draining with `tuple_in`.

## `tuples.dead_tuple_ratio`

**Symptom**: `<table> dead-tuple ratio <pct> exceeds 20%: ...`.

**Cause**: claim/ack/nack/expire churn on `nexus.tuples` or
`nexus.tuple_claim_log` outpacing autovacuum. Both tables carry
`autovacuum_vacuum_scale_factor = 0.01` (the first per-table storage
parameter in the changelog, ten times more aggressive than the 20%
default) specifically for this workload; a sustained red past that tuning
means autovacuum genuinely cannot keep up, not that the setting is
missing.

**Action**: the fix suggestion is literal — `VACUUM ANALYZE nexus.<table>;`.
This check reads `pg_stat_user_tables` directly (local-only admin-psql
path, the same one `nx doctor`'s migration-state and RLS checks use); it
does not run on a managed deployment, which vacuums server-side.

## `tuples.sweep_freshness`

**Symptom**: an unexpectedly old `last_swept_at`, or `pg_credentials
missing PG_PORT` / `psql binary not found` (local setup, not a sweep
problem).

**Cause**: this row reads `nexus.tuple_tenants.last_swept_at` alone. The
sweep itself runs every `SWEEP_INTERVAL_HOURS` (six hours) as a second task
on the same scheduler as the T1 crash-safety sweep, and this row's
threshold (18 hours, three times the interval) is `NexusService.java`'s own
slack budget before a single transient miss is worth reporting.

**Reading the sweep's own counted record.** The row above can tell you the
sweep is stale; it cannot tell you *why*, because the sweep's own per-run
cause is a structured log line, never a persisted row:

```
event=tuple_sweep_run tenants_visited=<n> oldest_last_swept_at=<ts>
  scanned=<n> released=<n> dead_lettered=<n> purged=<n> purge_examined=<n>
  log_rows_purged=<n> log_purge_examined=<n> incomplete_cause=<NONE|TENANT_CAP|TENANT_ERROR|WALL_CLOCK>
```

Grep the engine's log for `event=tuple_sweep_run` around the stale window.
`incomplete_cause` is the worst cause seen across every tenant that visit
touched (severity order `NONE < TENANT_CAP < TENANT_ERROR < WALL_CLOCK`):
`TENANT_CAP` means one or more tenants hit their own per-arm share of
`NX_TUPLE_SWEEP_MAX_BATCHES_PER_TENANT` and will be revisited first next
run (order lives in the table via `last_swept_at`, not a cursor in the
JVM); `TENANT_ERROR` means a tenant's arms threw; `WALL_CLOCK` means
`NX_TUPLE_SWEEP_WALL_CLOCK_BUDGET_SECONDS` ran out at a tenant boundary. A
run with `scanned=0` and `incomplete_cause=NONE` is the healthy steady
state (nothing was expired or lapsed) — it is a run that never logs at all,
or a run whose `oldest_last_swept_at` keeps growing across several checks,
that is the actual failure.

## Dead-letter triage

A row reaches `claim_state='dead'` at the template's `max_attempts` (3 for
`mailbox/<address>`), whether the cap was reached by `nack`s, lapsed
leases, or a mix. It leaves every claimant's view but stays readable:

1. `nx tuple rd <subspace> --pattern <KEY>=<VALUE>...` (or an empty pattern
   to read the whole subspace) — dead-lettered rows come back with their
   state, never with a `claim_id` (that field is rendered only by
   `in`/`inp`'s own response, so reading a dead row never leaks a means to
   ack or nack it).
2. `nx tuple stats <subspace>` to see the `dead` count against `total`.
3. Decide: a dead-lettered message usually means the intended recipient
   never processed it (crashed, was never dispatched, or the address was
   wrong). There is no client verb to revive or delete a dead-lettered row
   — `HttpTupleStore` exposes no delete operation, by design (the row is
   evidence).
4. If the row must be purged before its retention window (`out`'s
   `ttl_seconds`, default the template's `retention_seconds`; 7 days for
   `mailbox/<address>`), that is an admin SQL operation against the
   engine's Postgres, the same local-only admin path the doctor rows use:

   ```sql
   DELETE FROM nexus.tuples WHERE id = '<tuple id, lowercase hex>';
   ```

   `nexus.tuple_claim_log.tuple_id` is a nullable FK `ON DELETE SET NULL` —
   deleting the tuple row does not delete its claim-log history; the log
   rows survive with `tuple_id` nulled and `subspace`/`template`
   denormalized onto each row so they still read standalone. There is no
   other cleanup step.

## The claim log as the only history after a restore

`nexus.tuple_claim_log` is the append-only `claim`/`ack`/`nack`/`expire`/`dead`
audit trail — one row per transition, surviving the tuple row's own purge
(the FK above). After a point-in-time restore, `nexus.tuples` reflects
whatever state existed at the restore point, but leases and claims do not
resume where they left off: a restored row's `lease_until` is whatever it
was at the restore point, so it is claimable again the moment that time
passes (the availability predicate has no special case for "restored" —
see [Tuple Space § A row's life](../tuple-space.md#a-rows-life)). The claim
log is the only record of what happened to a tuple **between** the restore
point and the live cutover; there is no other audit trail to reconstruct
it from. Query it directly for forensics:

```sql
SELECT * FROM nexus.tuple_claim_log
WHERE tuple_id = '<tuple id>' OR (tuple_id IS NULL AND subspace = '<subspace>')
ORDER BY created_at;
```

## Templates change with an engine release, not with a data changeset

The two v1 templates (`ledger/<session_id>`, `mailbox/<address>`) ship as
YAML in engine resources (`service/src/main/resources/tuples/templates/`),
loaded and validated at boot; a breach fails boot with the file and field
named. Changing a template's shape is an engine-release event — the same
cadence as any other engine change (see AGENTS.md § Engine-service
release) — never a Liquibase data changeset. Removing a template that has
live rows needs an explicit data changeset with a migration note; adding
or widening one does not.

## `NX_TUPLE_TEMPLATE_DIR` must be unset in production

One test-only path exists beside the resource files: the engine also loads
templates from the directory named by `NX_TUPLE_TEMPLATE_DIR` when it is
set, logs that it did at boot, and lists both sources in `registry()`. A
directory set in production is a second, undeclared registry a client has
no way to distinguish from the real one — the `registry()` route's
`sources` field is how a caller (or a gate) can tell. `tests/e2e/cloud-
client-path-gate.sh` (RDR-205 Phase 3 Step 2) asserts this directly:
`registry()` against the deployed engine must report the resources source
only, so a stray `NX_TUPLE_TEMPLATE_DIR` in the production environment is a
red gate rather than a silent second registry. If that gate reds on this
assertion, the fix is environment hygiene on the engine deployment, never a
client-side workaround.
