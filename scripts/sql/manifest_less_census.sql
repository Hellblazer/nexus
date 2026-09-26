-- SPDX-License-Identifier: AGPL-3.0-or-later
-- manifest_less_census.sql (RDR-192 Step 2, bead nexus-wbfpw.4)
--
-- Classifies every chunk in one collection that carries NO own-collection
-- catalog_document_chunks manifest row into exactly one bucket, by
-- resolving its owning document from the chunk's own metadata field
-- catalog_doc_id (falling back to doc_id when catalog_doc_id is absent
-- or empty) and treating that value as a catalog_documents.tumbler.
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
-- Buckets:
--   superseded            owning document is live, has manifest rows in
--                         THIS collection, none naming this chash (a
--                         lost reap).
--   legacy-unmanifested   owning document is live, has NO manifest row
--                         in any collection (a note stored before
--                         nexus-b6enc).
--   dead-owner            owning document is tombstoned, OR live but
--                         every manifest row it has is in another
--                         collection (the rename-COPY leftover,
--                         CatalogRepository.java ~8101-8125).
--   no-owner              catalog_doc_id/doc_id is empty, or names no
--                         catalog document (a .nxexp import).
--   unclassified          anything else -- reported, never dropped.
--
-- Quarantine collections are refused by the caller before this text ever
-- runs (they are out of the census by construction); this statement does
-- not itself check the collection name.
SELECT encode(c.chash, 'hex') AS chash,
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
LEFT JOIN nexus.catalog_documents owner
       ON owner.tenant_id = c.tenant_id
      AND owner.tumbler = COALESCE(
              NULLIF(c.metadata ->> 'catalog_doc_id', ''),
              NULLIF(c.metadata ->> 'doc_id', ''))
LEFT JOIN LATERAL (
       SELECT count(*) AS total_count,
              count(*) FILTER (WHERE m.collection = c.collection) AS own_count
       FROM nexus.catalog_document_chunks m
       WHERE m.tenant_id = owner.tenant_id
         AND m.doc_id = owner.tumbler
) manifest_counts ON owner.tumbler IS NOT NULL
WHERE c.tenant_id = ?
  AND c.collection = ?
  AND own_manifest.chash IS NULL
ORDER BY c.chash
LIMIT ? OFFSET ?
