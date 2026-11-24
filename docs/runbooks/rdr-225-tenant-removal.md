# Removing a tenant (RDR-225)

Removes one tenant's vector storage and access from an engine on the RDR-225 layout (`vectors-030`): its leaves in
`nexus.chunks` and `nexus.taxonomy_centroids`, the rows that reference its chunks, and its tokens (service, session and identity tokens). It does not
touch the tenant's catalog documents, collections or topics; those are ordinary tenant-scoped rows and are out of
this runbook's scope.

Exercised by `DropTenantPartitionsIntegrationTest` (`theRunbook_removesTheTenantsLeavesReferencingRowsAndTokens_andLeavesAnotherTenantUntouched`
and `aDirectDropOfAReferencedChunksLeaf_isRefused_andChangesNothing`). The test covers the leaf drop and the session and service token deletes; the `identity_tokens` delete and its Verify count were added later and no test runs them.

## Who runs it

The schema owner (production: `nexus_admin`). `nexus.drop_tenant_partitions` is not SECURITY DEFINER and no
engine role may execute it, so `nexus_svc` cannot run this. Do not run it as a superuser: it would work, but the
owner is the role the function and its tests are written for.

## Before you start

1. Confirm the tenant id exactly. The function matches leaves by partition bound, so a typo drops nothing and
   returns 0, but step 3 deletes tokens by the same string.
2. Decide whether you need a copy of the tenant's data. The leaves are dropped, not retained; recovery is from
   cluster backups (PITR) only.
3. The `default` tenant is refused by the function. Do not remove it.
4. Run `SHOW deadlock_timeout;` (default `1s`). The function's lock budget is half of it, at most 500 ms; the
   defaults give 500 ms. A value below 1 s shrinks the budget (and the time it will wait for locks) in proportion; a
   value of 2 ms or less leaves no safe budget and the drop should not be run until it is raised.

## Steps

```sql
-- 1 and 2: delete the tenant's manifest, topic-assignment and orphaned-at rows, then DETACH and DROP its leaf
-- under every model partition of chunks and taxonomy_centroids. Returns the number of leaves dropped
-- (two parents x the number of model partitions; 0 on a repeat or for a tenant with no leaves).
-- Run it as ONE autocommit statement, exactly as written: the locks it takes are held until the transaction
-- commits, so a BEGIN ... COMMIT typed around it by hand keeps every search and manifest write waiting for as long
-- as you take to type the COMMIT.
SELECT nexus.drop_tenant_partitions('<tenant>');

-- 3: revoke access. A separate, visible statement: removing credentials is an authorization decision.
BEGIN;
DELETE FROM nexus.session_tokens WHERE tenant_id = '<tenant>';
DELETE FROM nexus.service_tokens WHERE tenant_id = '<tenant>';
DELETE FROM nexus.identity_tokens WHERE tenant_id = '<tenant>';
COMMIT;
```

The function takes the same per-tenant advisory lock as `create_tenant_partitions`, so a concurrent leaf creation
for that tenant waits for it, and one global advisory lock that serializes drops (two drops at once queue, they do
not fail). It reads the tenant's leaves first (none: it returns 0 and locks nothing). After its deletes it takes
ACCESS EXCLUSIVE locks on the manifest, topic-assignment, orphaned-at and collection tables, the two roots
(`nexus.chunks`, `nexus.taxonomy_centroids`) and the tenant's own partitions, and never on another tenant's
partitions. While it holds them, every search and every manifest write waits; uncontended that is milliseconds (the
`NOTICE` it raises reports the hold, up to function return; the locks last until the statement commits).

All the locks of one attempt share a single deadline, the budget (500 ms at the default `deadlock_timeout`), and it
tries five times with a short backoff. So a search or a write that arrives while the drop holds some of the locks
waits at most one budget per attempt, and the drop never waits longer than the budget on any of those locks: it gives up and
releases before PostgreSQL's deadlock detector runs, so the detector does not pick a search or a writer as its
victim. Waiting for another drop, or for a row lock in the deletes, is not part of the budget: that waits up to 10 s and then fails with 55P03 the same way. Giving up on the table locks takes about 4.5 s at the default budget (5 x 500 ms plus 0.2 + 0.4 + 0.6 + 0.8 s of backoff).

If something holds one of those locks through all five attempts (a search, a manifest write, an index build or a
vacuum on the tenant's partitions) it gives up with SQLSTATE `55P03` (`lock_not_available`) naming the tenant, and
changes nothing. That error is retryable: run the statement again, after any index build on the tenant's partitions
has finished (check `pg_stat_progress_create_index`). Each retry redoes the deletes, which on a populated tenant can
take minutes, so do not retry in a tight loop. Teardown code retries on `55P03` and on `40P01`.

Order matters. A token left in place while the leaves are dropped cannot write (no leaf for its tenant), and the
leaf-creating trigger fires on token INSERT only, so the leaves do not come back. Deleting tokens first is also
safe; the order above keeps access until storage is gone so a failure in step 1-2 leaves a working tenant.

## Verify

```sql
-- No leaves left for the tenant (expect zero rows).
SELECT c.relname
  FROM pg_catalog.pg_inherits i
  JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid
 WHERE pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'FOR VALUES IN (''<tenant>'')';

-- No tokens left (expect 0).
SELECT (SELECT count(*) FROM nexus.service_tokens WHERE tenant_id = '<tenant>')
     + (SELECT count(*) FROM nexus.session_tokens WHERE tenant_id = '<tenant>')
     + (SELECT count(*) FROM nexus.identity_tokens WHERE tenant_id = '<tenant>');
```

`nx doctor` compares token tenants against leaves; after this runbook the tenant appears in neither.

## Why not DROP TABLE on the leaves

A chunks leaf whose chunks the manifest references cannot be dropped directly: its partition-level clone of
`fk_catalog_chunks_chunk` depends on it, and PostgreSQL refuses with SQLSTATE `2BP01` (dependent objects still
exist). The refusal changes nothing. DETACH first, then DROP, is the path that works (observed on PG 17.5 in the
RDR-225 gate, pinned by the test above), and it is what the function does after clearing the referencing rows.

## Undo

There is none short of restoring from backup. A removed tenant can be given empty leaves again: inserting a token
for it fires the trigger, or the schema owner calls `nexus.create_tenant_partitions('nexus.chunks', '<tenant>')`
and the same for `nexus.taxonomy_centroids`.
