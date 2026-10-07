# Removing a tenant (RDR-225)

Removes one tenant's vector storage and access from an engine on the RDR-225 layout (`vectors-030`): its leaves in
`nexus.chunks` and `nexus.taxonomy_centroids`, the rows that reference its chunks, and its tokens. It does not
touch the tenant's catalog documents, collections or topics; those are ordinary tenant-scoped rows and are out of
this runbook's scope.

Exercised by `DropTenantPartitionsIntegrationTest` (`theRunbook_removesTheTenantsLeavesReferencingRowsAndTokens_andLeavesAnotherTenantUntouched`
and `aDirectDropOfAReferencedChunksLeaf_isRefused_andChangesNothing`).

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

## Steps

```sql
-- 1 and 2: delete the tenant's manifest, topic-assignment and orphaned-at rows, then DETACH and DROP its leaf
-- under every model partition of chunks and taxonomy_centroids. Returns the number of leaves dropped
-- (two parents x the number of model partitions; 0 on a repeat or for a tenant with no leaves).
BEGIN;
SELECT nexus.drop_tenant_partitions('<tenant>');
COMMIT;

-- 3: revoke access. A separate, visible statement: removing credentials is an authorization decision.
BEGIN;
DELETE FROM nexus.session_tokens WHERE tenant_id = '<tenant>';
DELETE FROM nexus.service_tokens WHERE tenant_id = '<tenant>';
COMMIT;
```

The function takes the same per-tenant advisory lock as `create_tenant_partitions`, so a concurrent leaf creation
for that tenant waits for it. It sets `lock_timeout = 10s`: if a reader holds one of the leaves, it fails rather
than queueing writers behind it. Retry when the reader is gone.

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
     + (SELECT count(*) FROM nexus.session_tokens WHERE tenant_id = '<tenant>');
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
