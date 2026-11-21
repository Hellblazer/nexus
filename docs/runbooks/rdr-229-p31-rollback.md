# RDR-229 P3.1: rolling the engine back after the id columns (conexus runbook section)

This section is handed to conexus by RDR-229 P3.1 (nexus-p2m1x.17; residual R5, nexus-p2m1x.44). Nexus does not run it: the cloud
engine revert is relay-mediated and conexus-owned (`docs/contributing.md`, "Engine-deploy revert"). It sits beside
`docs/runbooks/rdr-225-cloud-deploy.md` section 6, whose "a tag flip is safe" (6.2) is **not true after this engine**.

## The rule

**Pause the open stamp moves, then run the down-step, then flip the tag. Never the tag flip alone, and never the down-step
without the pause.**

RDR-229 P3.1's engine adds a CHECK to every table that names a collection, `NOT VALID`. A `NOT VALID` CHECK is enforced on
every new row version, so an engine from before P3.1 (it knows no id column) can no longer write: its chunk INSERT, its
supersede UPDATE and its carrier INSERTs fail with SQLSTATE 23514 (what it looks like: "If the tag was flipped without the
down-step", below). The previous engine's tag is therefore flipped back only after the CHECKs are dropped. The id columns and
the registry's identity column stay: an old engine ignores a column it does not know.

**Why the pause.** The previous engine has a move worker (RDR-246). It works every open row of `nexus.data_moves`, including P3.1's
stamp moves, which it did not register. With the CHECKs gone, a stamp move still runs its UPDATEs and then fails in its
`on_complete`, because `VALIDATE CONSTRAINT` names a constraint that no longer exists. The result is a failed move that the next
P3.1 engine must supersede, and rows the previous engine stamped for nothing. Paused moves are not started by any worker, so
pausing first leaves the registry as the P3.1 engine wrote it.

**The pause is a data write.** It updates rows of `nexus.data_moves`. On the cloud it is a data write under the same go as the
down-step: whoever authorises the down-step authorises the pause with it, and conexus runs both in the one change window. It
needs no separate approval, and it must not be run without the down-step's go.

The 20 constraints are one list, `service/src/main/resources/db/rdr229/p31-checks.tsv`. The SQL below, the down-step and its
re-add, is generated from it (`scripts/rdr229_p31_downstep.py`, with a drift check over both) and the same files ship as
`service/src/main/resources/db/rdr229/p31-downstep.sql` and `docs/runbooks/rdr-229-p31-readd.sql`.

## The window (Sam, 2026-11-20: "Only before a stamp completes")

The down-step is supported only while

1. no P3.1 stamp move on the install has `completed_at` set, and
2. in any case before P3.7's name-key drop move completes (that move removes the name key an old engine writes through).

Outside that window a downgrade is **PITR only** (`docs/runbooks/rdr-225-cloud-deploy.md` section 6.4); there is no
re-attach procedure. Check the window first, as `nexus_admin`:

```sql
SELECT label, completed_at FROM nexus.data_moves WHERE label LIKE 'rdr229-stamp-%' AND completed_at IS NOT NULL;
```

Zero rows means the first condition holds. Also confirm P3.7's name-key drop move has not completed (its label is in the P3.7
release notes; it does not exist before that release).

**How long the window is in practice.** The registry stamp (`rdr229-stamp-catalog_collections`) is registered first, and the
registry is small: one chunk per tenant (a chunk is up to 1000 rows). So wherever the moves run, "before a stamp completes"
closes about a minute after this engine's first boot, and the down-step is a first-minute procedure; after that the rollback
is PITR. The window stays open only while the moves cannot run:

- On a bring-your-own PostgreSQL (or a bundle without `pg_background`) every move stays blocked until the DBA installs
  `pg_background`.
- On the cloud, the window lasts until pg_background is installed on the cloud's PostgreSQL (nexus-f3yqd.2.23).

The query above counts any stamp move, the registry's included. Whether Sam's "a stamp" means that one, or only the moves of
the large tables, is an open question for him (critique S3 of nexus-p2m1x.17); until he answers, the registry's completion
closes the window.

## Procedure

1. **Check the window** (above). Zero completed stamp moves.
2. **Pause the open stamp moves.** As `nexus_admin`. This holds every stamp move that is not complete, so no worker (this engine's
   or the previous one's) starts one. The worker reads `paused_at` at the start of a move's turn and before each run (one CALL, one tenant and leaf),
   so a run already in flight finishes first and the move stops at the run boundary; it keeps the rows it has stamped. The statement skips a move someone else has already paused, so a hold of the operator's own
   survives the unpause in step 8.

<!-- p31-pause:begin -->
```sql
UPDATE nexus.data_moves SET paused_at = now(), paused_by = 'rdr229 down-step'
 WHERE label LIKE 'rdr229-stamp-%' AND completed_at IS NULL AND paused_at IS NULL;
```
<!-- p31-pause:end -->

3. **Run the down-step.** As `nexus_admin`, in one transaction. The engine may keep serving while it runs: each statement waits
   at most 5 s for its lock and the whole transaction fails if one cannot get it, so re-run in a quieter moment rather than
   raising the timeout. `chunks` and `taxonomy_centroids` are partitioned; dropping a constraint on the parent drops its copies
   on every leaf, and the test for this file asserts it. `psql -X -v ON_ERROR_STOP=1 --single-transaction -f p31-downstep.sql`
   runs this text, or paste it:

<!-- p31-downstep:begin -->
```sql
-- RDR-229 P3.1 operator down-step (residual R5, nexus-p2m1x.44). GENERATED by scripts/rdr229_p31_downstep.py from
-- service/src/main/resources/db/rdr229/p31-checks.tsv: edit the list and run that script with --write, never this file.
--
-- Drops every CHECK RDR-229 P3.1 added, NOT VALID or validated, so a pre-P3.1 engine can write again. Run it as
-- nexus_admin (the table owner), in one transaction, AFTER the open stamp moves are paused and BEFORE the previous
-- engine's tag is flipped back in
-- (docs/runbooks/rdr-229-p31-rollback.md):
--   psql -X -v ON_ERROR_STOP=1 --single-transaction -f p31-downstep.sql
-- It leaves the id columns, the registry's identity column and every row of the registry of moves alone, and it is
-- safe to run twice. A table that is absent (the retired queue table on most installs) is skipped.
SET LOCAL lock_timeout = '5s';
ALTER TABLE IF EXISTS nexus.catalog_collections DROP CONSTRAINT IF EXISTS catalog_collections_superseded_by_id_check;
ALTER TABLE IF EXISTS nexus.chunks DROP CONSTRAINT IF EXISTS chunks_collection_id_check;
ALTER TABLE IF EXISTS nexus.chunks DROP CONSTRAINT IF EXISTS chunks_collection_id_not_null;
ALTER TABLE IF EXISTS nexus.taxonomy_centroids DROP CONSTRAINT IF EXISTS taxonomy_centroids_collection_id_check;
ALTER TABLE IF EXISTS nexus.taxonomy_centroids DROP CONSTRAINT IF EXISTS taxonomy_centroids_collection_id_not_null;
ALTER TABLE IF EXISTS nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS catalog_document_chunks_collection_id_check;
ALTER TABLE IF EXISTS nexus.chunk_orphaned_at DROP CONSTRAINT IF EXISTS chunk_orphaned_at_collection_id_check;
ALTER TABLE IF EXISTS nexus.topic_assignments DROP CONSTRAINT IF EXISTS topic_assignments_source_collection_id_check;
ALTER TABLE IF EXISTS nexus.document_aspects DROP CONSTRAINT IF EXISTS document_aspects_collection_id_check;
ALTER TABLE IF EXISTS nexus.topics DROP CONSTRAINT IF EXISTS topics_collection_id_check;
ALTER TABLE IF EXISTS nexus.taxonomy_meta DROP CONSTRAINT IF EXISTS taxonomy_meta_collection_id_check;
ALTER TABLE IF EXISTS nexus.document_highlights DROP CONSTRAINT IF EXISTS document_highlights_collection_id_check;
ALTER TABLE IF EXISTS nexus.canary_query_ring DROP CONSTRAINT IF EXISTS canary_query_ring_collection_id_check;
ALTER TABLE IF EXISTS nexus.aspect_extraction_queue DROP CONSTRAINT IF EXISTS aspect_extraction_queue_collection_id_check;
ALTER TABLE IF EXISTS nexus.catalog_documents DROP CONSTRAINT IF EXISTS catalog_documents_physical_collection_id_check;
ALTER TABLE IF EXISTS nexus.chash_remap DROP CONSTRAINT IF EXISTS chash_remap_source_collection_id_check;
ALTER TABLE IF EXISTS nexus.chash_remap DROP CONSTRAINT IF EXISTS chash_remap_target_collection_id_check;
ALTER TABLE IF EXISTS nexus.pdf_pipeline DROP CONSTRAINT IF EXISTS pdf_pipeline_collection_id_check;
ALTER TABLE IF EXISTS nexus.reembed_jobs DROP CONSTRAINT IF EXISTS reembed_jobs_source_collection_id_check;
ALTER TABLE IF EXISTS nexus.reembed_jobs DROP CONSTRAINT IF EXISTS reembed_jobs_target_collection_id_check;
```
<!-- p31-downstep:end -->

4. **Verify.** The query returns no row when every listed constraint is gone, on the parents and the leaves:

<!-- p31-verify:begin -->
```sql
SELECT conrelid::regclass AS relation, conname FROM pg_catalog.pg_constraint
 WHERE conname IN ('catalog_collections_superseded_by_id_check', 'chunks_collection_id_check', 'chunks_collection_id_not_null', 'taxonomy_centroids_collection_id_check', 'taxonomy_centroids_collection_id_not_null', 'catalog_document_chunks_collection_id_check', 'chunk_orphaned_at_collection_id_check', 'topic_assignments_source_collection_id_check', 'document_aspects_collection_id_check', 'topics_collection_id_check', 'taxonomy_meta_collection_id_check', 'document_highlights_collection_id_check', 'canary_query_ring_collection_id_check', 'aspect_extraction_queue_collection_id_check', 'catalog_documents_physical_collection_id_check', 'chash_remap_source_collection_id_check', 'chash_remap_target_collection_id_check', 'pdf_pipeline_collection_id_check', 'reembed_jobs_source_collection_id_check', 'reembed_jobs_target_collection_id_check')
 ORDER BY conname, relation::text;
```
<!-- p31-verify:end -->

5. **Flip the tag.** Relay the previous `engine-service-v*` identity for redeploy
   (`conexus-dev-engine-redeploy`: `docker stop -t 30 conexus-engine`, `docker rm -f`, `docker run -d` of the new tag on the one
   `Role=db-engine` host; stop-start, one replica, no overlap, reconfirmed by bob after conexus RDR-007's database move,
   2026-11-21). Only now.
6. **Confirm.** `nx service probe` (reads `/version` live), then a write: index one document, or watch the engine log for
   SQLSTATE 23514, which must not appear.
7. **Re-add the CHECKs, after the next P3.1 deploy and before the pause is released.** The down-step dropped the CHECKs, but
   `DATABASECHANGELOG` still records vectors-068-3 to -19 as run, so the redeploy does not add them again. Without them every
   stamp move that completes fails in its `on_complete` with SQLSTATE `42704` (the constraint it validates does not exist; a
   permanent failure), and gate 1 of the completion check never holds. This step adds every listed CHECK back, `NOT VALID` with
   the expression the changeset used, in one transaction as `nexus_admin`. A `NOT VALID` constraint scans no row, so the rows the
   previous engine wrote meanwhile cannot fail it; the stamp moves fill their ids. It skips a table or id column the install lacks and a
   constraint that is already there, so it is safe to run twice. `psql -X -v ON_ERROR_STOP=1 --single-transaction -f
   rdr-229-p31-readd.sql` (the file is `docs/runbooks/rdr-229-p31-readd.sql`) runs this text, or paste it:

<!-- p31-readd:begin -->
```sql
-- RDR-229 P3.1 operator re-add (fix round C, nexus-p2m1x.17). GENERATED by scripts/rdr229_p31_downstep.py from
-- service/src/main/resources/db/rdr229/p31-checks.tsv: edit the list and run that script with --write, never this file.
--
-- Adds back every CHECK the down-step removed, each NOT VALID with the expression vectors-068 added it with. The
-- changelog records those changesets as run, so a redeploy of the P3.1 engine does not add them again. Run it as
-- nexus_admin (the table owner), in one transaction, AFTER the P3.1 engine is serving again and BEFORE the pause on
-- the stamp moves is released (docs/runbooks/rdr-229-p31-rollback.md):
--   psql -X -v ON_ERROR_STOP=1 --single-transaction -f rdr-229-p31-readd.sql
-- A NOT VALID constraint scans no row, so rows the previous engine wrote meanwhile cannot fail it; the stamp moves
-- fill their ids. It is safe to run twice. A table or id column that is absent (the retired queue) is skipped.
SET LOCAL lock_timeout = '5s';
DO $q$
BEGIN
    IF to_regclass('nexus.catalog_collections') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.catalog_collections') AND attname = 'superseded_by_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.catalog_collections') AND conname = 'catalog_collections_superseded_by_id_check') THEN
        ALTER TABLE nexus.catalog_collections ADD CONSTRAINT catalog_collections_superseded_by_id_check CHECK ((superseded_by = '') = (superseded_by_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.chunks') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.chunks') AND attname = 'collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.chunks') AND conname = 'chunks_collection_id_check') THEN
        ALTER TABLE nexus.chunks ADD CONSTRAINT chunks_collection_id_check CHECK ((NULLIF(collection, '') IS NULL) = (collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.chunks') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.chunks') AND attname = 'collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.chunks') AND conname = 'chunks_collection_id_not_null') THEN
        ALTER TABLE nexus.chunks ADD CONSTRAINT chunks_collection_id_not_null CHECK (collection_id IS NOT NULL) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.taxonomy_centroids') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.taxonomy_centroids') AND attname = 'collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.taxonomy_centroids') AND conname = 'taxonomy_centroids_collection_id_check') THEN
        ALTER TABLE nexus.taxonomy_centroids ADD CONSTRAINT taxonomy_centroids_collection_id_check CHECK ((NULLIF(collection, '') IS NULL) = (collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.taxonomy_centroids') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.taxonomy_centroids') AND attname = 'collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.taxonomy_centroids') AND conname = 'taxonomy_centroids_collection_id_not_null') THEN
        ALTER TABLE nexus.taxonomy_centroids ADD CONSTRAINT taxonomy_centroids_collection_id_not_null CHECK (collection_id IS NOT NULL) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.catalog_document_chunks') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.catalog_document_chunks') AND attname = 'collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.catalog_document_chunks') AND conname = 'catalog_document_chunks_collection_id_check') THEN
        ALTER TABLE nexus.catalog_document_chunks ADD CONSTRAINT catalog_document_chunks_collection_id_check CHECK ((NULLIF(collection, '') IS NULL) = (collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.chunk_orphaned_at') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.chunk_orphaned_at') AND attname = 'collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.chunk_orphaned_at') AND conname = 'chunk_orphaned_at_collection_id_check') THEN
        ALTER TABLE nexus.chunk_orphaned_at ADD CONSTRAINT chunk_orphaned_at_collection_id_check CHECK ((NULLIF(collection, '') IS NULL) = (collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.topic_assignments') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.topic_assignments') AND attname = 'source_collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.topic_assignments') AND conname = 'topic_assignments_source_collection_id_check') THEN
        ALTER TABLE nexus.topic_assignments ADD CONSTRAINT topic_assignments_source_collection_id_check CHECK ((NULLIF(source_collection, '') IS NULL) = (source_collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.document_aspects') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.document_aspects') AND attname = 'collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.document_aspects') AND conname = 'document_aspects_collection_id_check') THEN
        ALTER TABLE nexus.document_aspects ADD CONSTRAINT document_aspects_collection_id_check CHECK ((NULLIF(collection, '') IS NULL) = (collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.topics') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.topics') AND attname = 'collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.topics') AND conname = 'topics_collection_id_check') THEN
        ALTER TABLE nexus.topics ADD CONSTRAINT topics_collection_id_check CHECK ((NULLIF(collection, '') IS NULL) = (collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.taxonomy_meta') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.taxonomy_meta') AND attname = 'collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.taxonomy_meta') AND conname = 'taxonomy_meta_collection_id_check') THEN
        ALTER TABLE nexus.taxonomy_meta ADD CONSTRAINT taxonomy_meta_collection_id_check CHECK ((NULLIF(collection, '') IS NULL) = (collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.document_highlights') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.document_highlights') AND attname = 'collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.document_highlights') AND conname = 'document_highlights_collection_id_check') THEN
        ALTER TABLE nexus.document_highlights ADD CONSTRAINT document_highlights_collection_id_check CHECK ((NULLIF(collection, '') IS NULL) = (collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.canary_query_ring') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.canary_query_ring') AND attname = 'collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.canary_query_ring') AND conname = 'canary_query_ring_collection_id_check') THEN
        ALTER TABLE nexus.canary_query_ring ADD CONSTRAINT canary_query_ring_collection_id_check CHECK ((NULLIF(collection, '') IS NULL) = (collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.aspect_extraction_queue') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.aspect_extraction_queue') AND attname = 'collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.aspect_extraction_queue') AND conname = 'aspect_extraction_queue_collection_id_check') THEN
        ALTER TABLE nexus.aspect_extraction_queue ADD CONSTRAINT aspect_extraction_queue_collection_id_check CHECK ((NULLIF(collection, '') IS NULL) = (collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.catalog_documents') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.catalog_documents') AND attname = 'physical_collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.catalog_documents') AND conname = 'catalog_documents_physical_collection_id_check') THEN
        ALTER TABLE nexus.catalog_documents ADD CONSTRAINT catalog_documents_physical_collection_id_check CHECK ((NULLIF(physical_collection, '') IS NULL) = (physical_collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.chash_remap') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.chash_remap') AND attname = 'source_collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.chash_remap') AND conname = 'chash_remap_source_collection_id_check') THEN
        ALTER TABLE nexus.chash_remap ADD CONSTRAINT chash_remap_source_collection_id_check CHECK ((NULLIF(source_collection, '') IS NULL) = (source_collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.chash_remap') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.chash_remap') AND attname = 'target_collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.chash_remap') AND conname = 'chash_remap_target_collection_id_check') THEN
        ALTER TABLE nexus.chash_remap ADD CONSTRAINT chash_remap_target_collection_id_check CHECK ((NULLIF(target_collection, '') IS NULL) = (target_collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.pdf_pipeline') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.pdf_pipeline') AND attname = 'collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.pdf_pipeline') AND conname = 'pdf_pipeline_collection_id_check') THEN
        ALTER TABLE nexus.pdf_pipeline ADD CONSTRAINT pdf_pipeline_collection_id_check CHECK ((NULLIF(collection, '') IS NULL) = (collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.reembed_jobs') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.reembed_jobs') AND attname = 'source_collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.reembed_jobs') AND conname = 'reembed_jobs_source_collection_id_check') THEN
        ALTER TABLE nexus.reembed_jobs ADD CONSTRAINT reembed_jobs_source_collection_id_check CHECK ((NULLIF(source_collection, '') IS NULL) = (source_collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
DO $q$
BEGIN
    IF to_regclass('nexus.reembed_jobs') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass('nexus.reembed_jobs') AND attname = 'target_collection_id' AND NOT attisdropped)
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = to_regclass('nexus.reembed_jobs') AND conname = 'reembed_jobs_target_collection_id_check') THEN
        ALTER TABLE nexus.reembed_jobs ADD CONSTRAINT reembed_jobs_target_collection_id_check CHECK ((NULLIF(target_collection, '') IS NULL) = (target_collection_id IS NULL)) NOT VALID;
    END IF;
END
$q$;
```
<!-- p31-readd:end -->

   Then run the verification query of step 4 again: it must return one row per listed constraint (more where `chunks` and
   `taxonomy_centroids` have leaves; the retired queue's only where that table is kept).
8. **After the re-add, release the pause.** The stamp moves then run under that engine. This releases only the rows step 2 paused:

<!-- p31-unpause:begin -->
```sql
UPDATE nexus.data_moves SET paused_at = NULL, paused_by = NULL
 WHERE label LIKE 'rdr229-stamp-%' AND paused_by = 'rdr229 down-step';
```
<!-- p31-unpause:end -->

## If the tag was flipped without the down-step

The previous engine starts, serves reads, and fails writes. What an operator sees: **SQLSTATE `23514`** in the engine log and
in the failed request's error, naming a constraint from `p31-checks.tsv`:

```
ERROR: new row for relation "chunks_m1024_..." violates check constraint "chunks_collection_id_check"
ERROR: new row for relation "catalog_collections" violates check constraint "catalog_collections_superseded_by_id_check"
ERROR: new row for relation "topics" violates check constraint "topics_collection_id_check"
```

The first is a chunk write (index, upsert, quarantine move), the second a collection supersede or re-embed cutover, the third
a taxonomy write; every `*_collection_id_check` and `*_collection_id_not_null` name on the list means the same thing. Reads and
searches are unaffected. The fix is steps 3 and 4 (and the pause, step 2, if a stamp move is open); no restart is needed, because the dropped constraint no longer applies to
the engine's next write. A later deploy of the P3.1 engine then needs step 7. Nothing is lost: a refused write wrote nothing.

## What the down-step leaves, and one hazard

- The id columns, the registry's `collection_id` identity column and its sequence stay. The rows written while the CHECKs
  existed keep their ids.
- The registry of moves (`nexus.data_moves`) is not touched: within the window no stamp has completed, so nothing has to
  re-attach.
- vectors-069 redefined 24 function bodies (the writers of the guarded tables, so that each sets the id). The down-step
  leaves all 24 function bodies in place: it drops constraints, and no changeset rollback runs. Two of them behave differently
  for the previous engine. `canary_query_ring_upsert` now raises SQLSTATE `23503` for a collection name with no registry row,
  where it used to skip that entry; `catalog_reconcile_commit` now refuses an unregistered `physical_collection`. The
  previous engine's canary ring writer (`PciQueryRing`, before P3.1) drops the whole batch when one entry raises and has no
  per-entry retry, so after the rollback a batch of ring entries is lost whenever one of them names an unregistered
  collection. Nothing in this procedure undoes that, and the re-add does not touch the functions.
- A pause outlives the down-step until step 8 releases it: while it is held, the open stamp moves do not run, and the completion
  check reports them incomplete (that is accurate).

## Orphan rows (a VALIDATE that cannot finish)

A stamp move that finishes while some row names a collection with no registry row in its tenant (an orphan) completes, and its
table's CHECK stays `NOT VALID`; `nx doctor` shows a "RDR-229 VALIDATE <table>" warning, and the engine's `/v1/status`
`rdr229_completion.validate_waits` carries the same text. The move is complete, so it does not run again: registering the
missing collection alone stamps nothing, and `VALIDATE CONSTRAINT` by hand would still fail with SQLSTATE `23514`. An orphan row
also cannot be updated by any writer while its id is NULL, for the same CHECK.

1. **Remove the orphans.** Either register each missing collection, or delete the rows that name it.
2. **Run the restamp.** As the migrating role (`nexus_admin`):

   ```sql
   SELECT nexus.rdr229_restamp('<table>');
   ```

   It registers the table's stamp move once more, as `<label>-rerun-N`, which supersedes the completed move. That move stamps the
   rows that have become resolvable and validates the CHECK. It answers `registered: ...` or `not registered: <reason>` (for
   example, an orphan row is still there) and writes nothing else; run it again after fixing what the reason names. The move is
   throttled like any stamp move and shows under the Data moves rows while it runs.

The listings `nx doctor` prints for the dangling-tombstone and self-id counts are read-only queries, also run as `nexus_admin`.

## Local and bring-your-own installs

**A local install has no rollback** (`docs/runbooks/rdr-225-cloud-deploy.md` section 8): walking back needs a client release
that moves the engine pin backward, and the previous engine then fails on the new constraints exactly as above. There is nothing
for conexus to run; the same SQL works on a local database as the table owner for an operator who does it by hand, inside the
same window. A bring-your-own PostgreSQL is the operator's own.
