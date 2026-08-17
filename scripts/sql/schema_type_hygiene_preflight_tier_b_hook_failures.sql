-- SPDX-License-Identifier: AGPL-3.0-or-later
-- schema_type_hygiene_preflight_tier_b_hook_failures.sql — nexus-cefa1.3 (P2 of
-- the schema type-hygiene arc).
--
-- SPLIT OUT of schema_type_hygiene_preflight_tier_b.sql (nexus-cefa1.1/.2) the
-- moment hook_failures.batch_doc_ids converted to jsonb (telemetry-004-type-
-- hygiene.xml) — exactly the split that file's own header predicted would be
-- needed the first time one of its six columns' phase shipped ("this file must
-- be split further... rather than patched with dynamic SQL"). Mirrors how
-- catalog-031 (P1) split Tier A out of the original combined
-- schema_type_hygiene_preflight.sql.
--
-- PRE-MIGRATION PROBE — ERRORS BY DESIGN ONCE THIS COLUMN'S OWN PHASE HAS
-- SHIPPED ON THIS CLUSTER (P2, telemetry-004-type-hygiene.xml). Once shipped,
-- `pg_input_is_valid(batch_doc_ids, 'jsonb')` and `batch_doc_ids = ''` both
-- raise "operator does not exist: jsonb = unknown" instead of returning zero
-- rows. THAT ERROR IS THE CORRECT SIGNAL this column's audit job is done, not
-- a probe bug — see tests/db/test_schema_type_hygiene_preflight.py, the
-- canonical caller, which checks information_schema.columns.data_type before
-- running this file and retires the malformed-seed audit the same way it
-- already retired Tier A's once catalog-031 shipped.
--
-- Audits: hook_failures.batch_doc_ids (-> jsonb).
--
-- COLUMNS EMITTED (one row):
--   tier                  'B'.
--   table_name / column_name
--   total_rows            count(*) over the whole table.
--   null_count             col IS NULL.
--   empty_string_count      col = ''. The eventual (now shipped) USING clause
--                          is NULLIF(col,'')::jsonb, so '' becomes NULL and
--                          never reaches the cast — benign, unlike
--                          plans.plan_json / topic_links.link_types (see
--                          schema_type_hygiene_preflight_tier_b.sql).
--   non_iso_prefix_count   Always NULL (literal SQL NULL) — Tier-A-only
--                          column, kept for row-shape parity.
--   invalid_cast_count     THE real check: col IS NOT NULL AND col <> ''
--                          AND NOT pg_input_is_valid(col, 'jsonb').
--
-- RLS / MULTI-TENANT VISIBILITY: identical caveats to
-- schema_type_hygiene_preflight_tier_b.sql's own header (hook_failures is
-- ENABLE + FORCE ROW LEVEL SECURITY with the standard tenant_isolation
-- policy, per telemetry-001-baseline.xml) — read that file's RLS section
-- before running this one against a live multi-tenant cluster.

SELECT tier, table_name, column_name,
       total_rows, null_count, empty_string_count,
       non_iso_prefix_count, invalid_cast_count
FROM (
    -- ── column: hook_failures.batch_doc_ids ─────────────────────────────
    SELECT 'B' AS tier, 'hook_failures' AS table_name, 'batch_doc_ids' AS column_name,
           count(*) AS total_rows,
           count(*) FILTER (WHERE batch_doc_ids IS NULL) AS null_count,
           count(*) FILTER (WHERE batch_doc_ids = '') AS empty_string_count,
           NULL::bigint AS non_iso_prefix_count,
           count(*) FILTER (WHERE batch_doc_ids IS NOT NULL AND batch_doc_ids <> ''
                             AND NOT pg_input_is_valid(batch_doc_ids, 'jsonb')) AS invalid_cast_count
    FROM nexus.hook_failures
) t
ORDER BY tier, table_name, column_name;
