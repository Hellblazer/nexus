-- SPDX-License-Identifier: AGPL-3.0-or-later
-- manifest_less_census.sql (RDR-192 Step 2, bead nexus-wbfpw.4)
--
-- Classifies every chunk in one collection that carries NO own-collection
-- catalog_document_chunks manifest row into exactly one bucket, by
-- resolving its owning document from the chunk's own metadata field
-- catalog_doc_id (falling back to doc_id when catalog_doc_id is absent
-- or empty) and treating that value as a catalog_documents.tumbler. A
-- chunk with no forward key at all is ALSO checked in reverse: whether a
-- live, note-shaped (file_path empty) catalog_documents row in this same
-- collection carries this chunk's own chash under ITS OWN metadata.doc_id.
-- That is the identical predicate nexus.catalog_document_chunks's sweep
-- ("nl3fn NOTES GUARD", CatalogRepository.java sweepChunksQuery) and the
-- client's live_note_chashes (src/nexus/indexer_utils.py) already use to
-- recognize a live manifest-less note; this census must agree with them.
-- The forward lookup wins when both resolve (see the bucket CASE below):
-- the forward pointer is stamped at write time to name the chunk's TRUE
-- current owner, so a reverse match against a different, coincidentally
-- content-identical document must never override it.
--
-- SOLE COPY of this statement (Sam's ruling 2026-09-26, nexus-wbfpw.4):
-- the engine route (POST /v1/vectors/manifest-less-census,
-- PgVectorRepository.MANIFEST_LESS_CENSUS_SQL) and this file execute the
-- IDENTICAL text -- ManifestLessCensusSqlIdentityTest pins them equal, so
-- there is no second copy that can drift. No engine tag carries the route
-- until the rest of RDR-192 ships (Sam, 2026-09-26); until then, run this
-- file directly against production (psql), substituting each positional
-- placeholder below with its literal value in this exact order:
--   1. tenant_id   (text)
--   2. collection  (text)
--   3. limit       (integer, <= 300)
--   4. offset      (integer, >= 0)
--
-- HAND-RUN PREREQUISITES (code-review finding, round 1 fix):
--   - Role: nexus_svc. nexus_diag is the only BYPASSRLS role in this
--     schema, but its grants deliberately EXCLUDE catalog_documents, so
--     it cannot even execute this join (permission denied). nexus_svc has
--     SELECT on all three joined tables but is NOSUPERUSER NOBYPASSRLS,
--     so it is fully subject to the FORCE ROW LEVEL SECURITY below.
--   - EVERY joined table (nexus.chunks, nexus.catalog_document_chunks,
--     nexus.catalog_documents) carries ENABLE + FORCE ROW LEVEL SECURITY
--     with policy tenant_id = current_setting('nexus.tenant', true). FORCE
--     RLS applies even to nexus_svc. Before running the SELECT below, in
--     THE SAME psql session, run:
--       SELECT set_config('nexus.tenant', '<tenant_id>', false);
--     The trailing false is deliberate: it sets the GUC for the whole
--     SESSION, not just the current transaction (the engine's own
--     TenantScope.stampAndRun uses the transaction-local true form,
--     which does not apply outside a single multi-statement transaction).
--     Skipping this step is SILENT, not an error: every joined table's
--     RLS policy evaluates tenant_id = NULL, which is never true, so
--     every bucket -- including unclassified -- reads 0. This is the
--     same false-zero trap this changelog tree has hit and documented
--     repeatedly (taxonomy-007-unify-centroids.xml, migration-002-
--     tenant-pk.xml, catalog-014-manifest-collection-stamp.xml, and
--     others); it is not distinguishable from a genuinely clean census by
--     this statement's output alone -- cross-check scope_chunk_total
--     below against a known-nonzero expectation before trusting a result.
--   - Finding tenant_id: nexus.service_tokens carries NO RLS (it must be
--     readable before a tenant context exists, to authenticate the
--     request that establishes one) and maps token_hash -> tenant_id, so
--     `SELECT DISTINCT tenant_id FROM nexus.service_tokens;` (as
--     nexus_svc, no set_config needed for this one query) lists every
--     tenant this deployment has ever issued a token for.
--
-- Buckets:
--   superseded            owning document is live, has manifest rows in
--                         THIS collection, none naming this chash (a
--                         lost reap).
--   legacy-unmanifested   owning document is live, has NO manifest row
--                         in any collection (a note stored before
--                         nexus-b6enc), found by the forward pointer OR
--                         the reverse notes-guard match described above.
--   dead-owner            owning document is tombstoned, OR live but
--                         every manifest row it has is in another
--                         collection (the rename-COPY leftover,
--                         CatalogRepository.java ~8101-8125).
--   no-owner              catalog_doc_id/doc_id is empty or names no
--                         catalog document, AND no live note-shaped
--                         document's own metadata.doc_id names this
--                         chash either (a .nxexp import).
--   unclassified          anything else -- reported, never dropped.
--
-- Quarantine collections are refused by the caller before this text ever
-- runs (they are out of the census by construction); this statement does
-- not itself check the collection name.
--
-- RESPONSE SHAPE (round 1 fix): every row carries a row_kind discriminator
-- so this statement always returns bucket TOTALS (over the WHOLE
-- collection, computed before LIMIT/OFFSET) and scope_chunk_total (every
-- chunk this tenant+collection holds, any manifest state) even when the
-- current PAGE of itemized 'item' rows is empty -- a page-only count
-- cannot distinguish "this page is empty because the collection is
-- clean" from "this page is empty because the scope itself is wrong or
-- the RLS GUC was never set" (code-review + critic finding, round 1).
--   row_kind = 'item'  -- one row per manifest-less chunk on THIS PAGE:
--                         chash and bucket are set; bucket_total and
--                         scope_chunk_total are NULL.
--   row_kind = 'total' -- exactly 5 rows, one per bucket, ALWAYS present
--                         regardless of paging: bucket and bucket_total
--                         are set (bucket_total is the count over the
--                         WHOLE collection, not this page); chash and
--                         scope_chunk_total are NULL.
--   row_kind = 'scope' -- exactly 1 row, ALWAYS present: scope_chunk_total
--                         is the count of every chunk nexus.chunks holds
--                         for this tenant+collection, any manifest state;
--                         chash, bucket, and bucket_total are NULL.
WITH base AS (
    SELECT
        encode(c.chash, 'hex') AS chash,
        (own_manifest.chash IS NULL) AS is_manifest_less,
        CASE
            WHEN owner.tumbler IS NULL THEN 'no-owner'
            WHEN owner.deleted_at IS NOT NULL THEN 'dead-owner'
            WHEN manifest_counts.total_count = 0 THEN 'legacy-unmanifested'
            WHEN manifest_counts.own_count = 0 THEN 'dead-owner'
            WHEN manifest_counts.own_count > 0 THEN 'superseded'
            ELSE 'unclassified'
        END AS bucket
    FROM nexus.chunks c
    LEFT JOIN nexus.catalog_document_chunks own_manifest
           ON own_manifest.tenant_id = c.tenant_id
          AND own_manifest.collection = c.collection
          AND own_manifest.chash = c.chash
    LEFT JOIN nexus.catalog_documents fwd_owner
           ON fwd_owner.tenant_id = c.tenant_id
          AND fwd_owner.tumbler = COALESCE(
                  NULLIF(c.metadata ->> 'catalog_doc_id', ''),
                  NULLIF(c.metadata ->> 'doc_id', ''))
    LEFT JOIN LATERAL (
           SELECT d2.tumbler
           FROM nexus.catalog_documents d2
           WHERE d2.tenant_id = c.tenant_id
             AND d2.physical_collection = c.collection
             AND d2.deleted_at IS NULL
             AND (d2.file_path IS NULL OR d2.file_path = '')
             AND (d2.metadata ->> 'doc_id') = encode(c.chash, 'hex')
           ORDER BY d2.tumbler
           LIMIT 1
    ) rev_owner ON fwd_owner.tumbler IS NULL
    CROSS JOIN LATERAL (
           SELECT
               COALESCE(fwd_owner.tumbler, rev_owner.tumbler) AS tumbler,
               CASE WHEN fwd_owner.tumbler IS NOT NULL
                    THEN fwd_owner.deleted_at
                    ELSE NULL
               END AS deleted_at
    ) owner
    LEFT JOIN LATERAL (
           SELECT
               count(*) AS total_count,
               count(*) FILTER (WHERE m.collection = c.collection) AS own_count
           FROM nexus.catalog_document_chunks m
           WHERE m.tenant_id = c.tenant_id
             AND m.doc_id = owner.tumbler
    ) manifest_counts ON owner.tumbler IS NOT NULL
    WHERE c.tenant_id = ?
      AND c.collection = ?
),
scope AS (
    SELECT count(*) AS scope_chunk_total FROM base
),
candidates AS (
    SELECT chash, bucket FROM base WHERE is_manifest_less
),
all_buckets AS (
    SELECT unnest(ARRAY['superseded', 'legacy-unmanifested', 'dead-owner',
                         'no-owner', 'unclassified']) AS bucket
),
bucket_totals AS (
    SELECT ab.bucket, COALESCE(t.bucket_total, 0) AS bucket_total
    FROM all_buckets ab
    LEFT JOIN (SELECT bucket, count(*) AS bucket_total FROM candidates GROUP BY bucket) t
           ON t.bucket = ab.bucket
),
page AS (
    SELECT chash, bucket FROM candidates ORDER BY chash LIMIT ? OFFSET ?
)
SELECT 'item' AS row_kind, p.chash AS chash, p.bucket AS bucket,
       NULL::bigint AS bucket_total, NULL::bigint AS scope_chunk_total
FROM page p
UNION ALL
SELECT 'total' AS row_kind, NULL::text AS chash, bt.bucket AS bucket,
       bt.bucket_total, NULL::bigint AS scope_chunk_total
FROM bucket_totals bt
UNION ALL
SELECT 'scope' AS row_kind, NULL::text AS chash, NULL::text AS bucket,
       NULL::bigint AS bucket_total, s.scope_chunk_total AS scope_chunk_total
FROM scope s
