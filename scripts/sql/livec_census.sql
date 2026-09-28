-- SPDX-License-Identifier: AGPL-3.0-or-later
-- livec_census.sql (RDR-192, bead nexus-wbfpw.32) -- the v0.1.136 deploy
-- gate for live(c), the read-visibility predicate engine-service-v0.1.136
-- ships (vectors-019, superseding the v0.1.135 tag that never deploys --
-- see the bead's retarget comment).
--
-- STANDALONE by necessity: the deployed engine (v0.1.134) predates
-- vectors-018, so nexus.chunk_live_owners(tenant, collection, chash) does
-- not exist yet. This file INLINES that function's exact body (vectors-018-
-- chunk-live-owners-function.xml):
--
--     live(c) := EXISTS (
--         SELECT 1 FROM nexus.catalog_document_chunks m
--         JOIN nexus.catalog_documents d
--           ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id
--        WHERE m.tenant_id = c.tenant_id AND m.collection = c.collection
--          AND m.chash = c.chash AND d.deleted_at IS NULL
--     )
--
-- and classifies every OTHER chunk (every chunk nexus.chunks holds for
-- this tenant, across every collection -- RDR-191 Phase 4 unified this
-- into one table with one embedding_<dim> column per row, so a single
-- scan of it already covers 384/768/1024 with no per-dim table to miss)
-- into exactly one of four non-live causes, evaluated in this order:
--
--   quarantine            the chunk sits in a quarantine-* sibling
--                         collection (catalog-023). GC moved it there as an
--                         orphan; its manifest rows, if any, still name the
--                         origin collection, so the tests below would
--                         misread it. No search reads quarantine-*, so it is
--                         reported for reconciliation only, not as a loss.
--   tombstoned-owner      not live, but an own-collection manifest row
--                         exists. By construction that owner MUST be
--                         tombstoned (fk_catalog_chunks_catalog_doc,
--                         fk-001, VALIDATED, rules out a dangling manifest
--                         row; a live own-collection owner would already
--                         have matched "live" above) -- Sam's ruling
--                         2026-09-27 (REVERSED comment): an accepted loss,
--                         no re-own, no restore.
--   other-collection-only no own-collection manifest row, but this chash
--                         is manifested in a different collection of the
--                         same tenant.
--   no-manifest           no manifest row for this chash anywhere in the
--                         tenant.
--
-- DEPLOY CONDITION: every non-live chunk is tombstoned-owner, or is on the
-- 2026-09-27 manifest-less census's disposition list (T2 nexus/rdr-192-
-- census-2026-09-27) -- other-collection-only/no-manifest here that are
-- NOT on that list are a regression, not an accepted loss -- and
-- gate-xr789 reads 100% live after its re-seed with owners.
--
-- GUARD: the file raises, rather than printing an all-zero grid, when
-- the tenant GUC is unset or names a tenant that holds no chunks. For this
-- gate an empty result would read as a clean pass. Run psql with
-- -v ON_ERROR_STOP=1 so the raise stops the run.
--
-- USAGE: read-only (BEGIN READ ONLY), one call per tenant -- set the RLS
-- GUC first, in the SAME transaction (the `true` third argument scopes it
-- to this BEGIN..COMMIT, unlike manifest_less_census.sql's session-wide
-- hand-run form). Role nexus_svc (NOSUPERUSER NOBYPASSRLS): every joined
-- table carries FORCE ROW LEVEL SECURITY, so no tenant_id predicate is
-- written below -- the policy (`tenant_id = current_setting('nexus.tenant',
-- true)`) supplies it, and texteq/byteaeq are both leakproof so it still
-- drives an index scan (same barrier analysis as manifest_less_census.sql's
-- header; nexus_diag cannot run this, its grants exclude catalog_documents).
-- Per-tenant loop over an EXPLICIT tenant list, which must include nexus
-- and gate-xr789. `SELECT DISTINCT tenant_id FROM nexus.service_tokens`
-- (no RLS on that table) can help find others, but a tenant seeded without
-- ever being issued a token is absent from it, so it is not the list:
--
--     for t in nexus gate-xr789; do
--       psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -v tenant="$t" \
--            -f scripts/sql/livec_census.sql
--     done
--
-- RESULT SHAPE: row_kind='collection' -- one row per (collection, cause)
-- this tenant holds chunks in, EVERY cause present even at count 0 (a
-- reconciliation grid, not a sparse list), plus cause='live' so the
-- per-collection and grand totals below reconcile against the fork's own
-- live_chunks figure. row_kind='total' -- one row per cause, collection
-- NULL, summed across every collection.
--
-- INDEXES: idx_catalog_chunks_chash (tenant_id, chash) -- catalog-001-
-- baseline.xml -- serves all three EXISTS probes below (chash is a content
-- hash, so an index-condition equality on it returns very few rows;
-- own-collection checks add `collection = ...` as a cheap post-scan filter
-- on those few rows, `other-collection-only`'s probe needs no filter at
-- all). catalog_documents_pk (tenant_id, tumbler) serves the join to the
-- owning document by direct PK lookup.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '120s';
SELECT set_config('nexus.tenant', :'tenant', true);

DO $guard$
BEGIN
    IF COALESCE(current_setting('nexus.tenant', true), '') = '' THEN
        RAISE EXCEPTION 'livec_census: nexus.tenant is unset; pass -v tenant=<tenant>';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM nexus.chunks) THEN
        RAISE EXCEPTION 'livec_census: tenant % holds no chunks; wrong tenant name?',
            current_setting('nexus.tenant', true);
    END IF;
END
$guard$;

WITH scoped AS (
    SELECT c.collection, c.chash
      FROM nexus.chunks c
),
classified AS (
    SELECT
        s.collection,
        CASE
            WHEN s.collection LIKE 'quarantine-%' THEN 'quarantine'
            WHEN EXISTS (
                     SELECT 1
                       FROM nexus.catalog_document_chunks m
                       JOIN nexus.catalog_documents d
                         ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id
                      WHERE m.collection = s.collection
                        AND m.chash      = s.chash
                        AND d.deleted_at IS NULL
                 ) THEN 'live'
            WHEN EXISTS (
                     SELECT 1
                       FROM nexus.catalog_document_chunks m
                      WHERE m.collection = s.collection
                        AND m.chash      = s.chash
                 ) THEN 'tombstoned-owner'
            WHEN EXISTS (
                     SELECT 1
                       FROM nexus.catalog_document_chunks m
                      WHERE m.chash = s.chash
                 ) THEN 'other-collection-only'
            ELSE 'no-manifest'
        END AS cause
      FROM scoped s
),
causes (cause) AS (
    SELECT unnest(ARRAY['live', 'quarantine', 'tombstoned-owner', 'other-collection-only', 'no-manifest'])
),
collections AS (SELECT DISTINCT collection FROM scoped),
per_collection AS (
    SELECT collection, cause, count(*) AS chunk_count
      FROM classified
     GROUP BY collection, cause
),
grid AS (
    SELECT co.collection, ca.cause
      FROM collections co CROSS JOIN causes ca
)
SELECT * FROM (
    SELECT 'collection' AS row_kind,
           g.collection,
           g.cause AS cause,
           COALESCE(pc.chunk_count, 0) AS chunk_count
      FROM grid g
      LEFT JOIN per_collection pc
             ON pc.collection = g.collection AND pc.cause = g.cause
    UNION ALL
    SELECT 'total' AS row_kind,
           NULL::text AS collection,
           ca.cause AS cause,
           COALESCE(SUM(pc.chunk_count), 0) AS chunk_count
      FROM causes ca
      LEFT JOIN per_collection pc ON pc.cause = ca.cause
     GROUP BY ca.cause
) results
ORDER BY row_kind, collection NULLS LAST,
         CASE cause
             WHEN 'live'                   THEN 1
             WHEN 'quarantine'              THEN 2
             WHEN 'tombstoned-owner'        THEN 3
             WHEN 'other-collection-only'   THEN 4
             WHEN 'no-manifest'             THEN 5
         END;

COMMIT;
