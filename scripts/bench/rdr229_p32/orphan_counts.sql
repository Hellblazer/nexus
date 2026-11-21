-- SPDX-License-Identifier: AGPL-3.0-or-later
-- The RDR-229 Phase 3 cut preflight's orphan count. bob ran it on production on Sam's go (2026-11-21, T2
-- nexus/rdr229-orphan-count-prod-read-2026-11-21; the run's copy was fb8439c009's, this file less these three lines).
--
-- RDR-229 P3.1 (nexus-p2m1x.17) input, from P3.2 (nexus-p2m1x.18): per Expand-list name column, the rows whose name
-- names no registry row of the same tenant. The stamp cannot give those rows an id (its subselect finds no
-- catalog_collections row), so a NOT VALID CHECK (NULLIF(<name>, '') IS NULL) = (<name>_id IS NULL) would fail its
-- VALIDATE on them. '' is "unset" by linda's ruling (T2 nexus_rdr/229-p32-finding-empty-string-names-2026-11-20) and
-- counted apart, never as an orphan.
--
-- READ ONLY: one BEGIN READ ONLY transaction, SELECTs only (no DDL, no temp tables, no writes), statement_timeout 60 s.
-- Safe on production, a fork or a local store. One tenant per run:
--
--   psql -X -d nexus -v tenant=nexus -f orphan_counts.sql
--
-- The tenant is set as the engine's RLS setting (nexus.tenant, set_config(..., true) as TenantScope.withTenant does)
-- and also filtered on explicitly, so a superuser or BYPASSRLS session (which RLS does not narrow) counts the same rows.
--
-- The Expand list: RDR-229 item 6 and P3.1's bead (nexus-p2m1x.17), with the four history tables linda added on
-- 2026-11-21 (relevance_log, search_telemetry, hook_failures, gc_audit: a bare nullable collection_id, no CHECK, no
-- stamp; counted here anyway, since a later stamp or a reader by id meets the same rows). A table or column this
-- database does not have (aspect_extraction_queue is retired on most installs; after P4.1 the name columns are gone)
-- is skipped, and its absence printed. The match is by (tenant_id, name), as the stamp's subselect matches.
--
-- Output: one row per (table, name column): table, name_column, total_rows, orphan_rows, orphan_distinct_names,
-- empty_name_rows.
\set ON_ERROR_STOP on
\if :{?tenant}
\else
\echo 'orphan_counts.sql: pass the tenant, e.g. psql -v tenant=nexus -f orphan_counts.sql'
\quit
\endif
\pset footer off
BEGIN READ ONLY;
SET LOCAL statement_timeout = '60s';
SELECT set_config('nexus.tenant', :'tenant', true) AS tenant_setting \gset
\echo 'orphan_counts.sql: tenant' :tenant

-- the list, and which entries this database lacks
SELECT format('skipped (absent here): nexus.%s.%s', s.t, s.c) AS absent
  FROM (VALUES ('catalog_collections', 'superseded_by'), ('chunks', 'collection'),
               ('taxonomy_centroids', 'collection'), ('catalog_document_chunks', 'collection'),
               ('chunk_orphaned_at', 'collection'), ('topic_assignments', 'source_collection'),
               ('document_aspects', 'collection'), ('topics', 'collection'), ('taxonomy_meta', 'collection'),
               ('document_highlights', 'collection'), ('canary_query_ring', 'collection'),
               ('aspect_extraction_queue', 'collection'), ('catalog_documents', 'physical_collection'),
               ('chash_remap', 'source_collection'), ('chash_remap', 'target_collection'),
               ('pdf_pipeline', 'collection'), ('reembed_jobs', 'source_collection'),
               ('reembed_jobs', 'target_collection'), ('relevance_log', 'collection'),
               ('search_telemetry', 'collection'), ('hook_failures', 'collection'), ('gc_audit', 'collection')) s(t, c)
 WHERE NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = to_regclass('nexus.' || s.t)
                      AND a.attname = s.c AND NOT a.attisdropped);

-- one counting SELECT per present (table, column), generated from the catalog and run by \gexec
\echo 'table,name_column,total_rows,orphan_rows,orphan_distinct_names,empty_name_rows'
\pset tuples_only on
SELECT format($q$SELECT %L AS "table", %L AS name_column, count(*) AS total_rows,
       count(*) FILTER (WHERE NULLIF(t.%I, '') IS NOT NULL AND r.name IS NULL) AS orphan_rows,
       count(DISTINCT t.%I) FILTER (WHERE NULLIF(t.%I, '') IS NOT NULL AND r.name IS NULL) AS orphan_distinct_names,
       count(*) FILTER (WHERE t.%I = '') AS empty_name_rows
  FROM nexus.%I t
  LEFT JOIN nexus.catalog_collections r ON r.tenant_id = t.tenant_id AND r.name = t.%I
 WHERE t.tenant_id = %L$q$,
              'nexus.' || s.t, s.c, s.c, s.c, s.c, s.c, s.t, s.c, :'tenant')
  FROM (VALUES (1, 'catalog_collections', 'superseded_by'), (2, 'chunks', 'collection'),
               (3, 'taxonomy_centroids', 'collection'), (4, 'catalog_document_chunks', 'collection'),
               (5, 'chunk_orphaned_at', 'collection'), (6, 'topic_assignments', 'source_collection'),
               (7, 'document_aspects', 'collection'), (8, 'topics', 'collection'), (9, 'taxonomy_meta', 'collection'),
               (10, 'document_highlights', 'collection'), (11, 'canary_query_ring', 'collection'),
               (12, 'aspect_extraction_queue', 'collection'), (13, 'catalog_documents', 'physical_collection'),
               (14, 'chash_remap', 'source_collection'), (15, 'chash_remap', 'target_collection'),
               (16, 'pdf_pipeline', 'collection'), (17, 'reembed_jobs', 'source_collection'),
               (18, 'reembed_jobs', 'target_collection'), (19, 'relevance_log', 'collection'),
               (20, 'search_telemetry', 'collection'), (21, 'hook_failures', 'collection'),
               (22, 'gc_audit', 'collection')) s(n, t, c)
 WHERE EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = to_regclass('nexus.' || s.t)
                  AND a.attname = s.c AND NOT a.attisdropped)
   AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = to_regclass('nexus.' || s.t)
                  AND a.attname = 'tenant_id' AND NOT a.attisdropped)
 ORDER BY s.n
\gexec
\pset tuples_only off
COMMIT;
