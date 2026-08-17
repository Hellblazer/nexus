-- SPDX-License-Identifier: AGPL-3.0-or-later
-- schema_type_hygiene_preflight_tier_b_document_aspects.sql — nexus-cefa1.4
-- (P3 of the schema type-hygiene arc).
--
-- SPLIT OUT of schema_type_hygiene_preflight_tier_b.sql (nexus-cefa1.1/.2)
-- the moment document_aspects.extras / .salient_sentences converted to
-- jsonb (aspects-003-type-hygiene.xml) — exactly the split that file's own
-- header predicted would be needed the next time one of its remaining
-- columns' phase shipped ("this file must be split further... rather than
-- patched with dynamic SQL"). Mirrors how telemetry-004 (P2) split
-- hook_failures.batch_doc_ids out into
-- schema_type_hygiene_preflight_tier_b_hook_failures.sql, and how
-- catalog-031 (P1) split Tier A out of the original combined
-- schema_type_hygiene_preflight.sql.
--
-- PRE-MIGRATION PROBE — ERRORS BY DESIGN ONCE THESE COLUMNS' OWN PHASE HAS
-- SHIPPED ON THIS CLUSTER (P3, aspects-003-type-hygiene.xml). Once shipped,
-- `pg_input_is_valid(extras, 'jsonb')` / `extras = ''` (and the same pair
-- for salient_sentences) both raise "operator does not exist: jsonb =
-- unknown" instead of returning zero rows. THAT ERROR IS THE CORRECT
-- SIGNAL these columns' audit job is done, not a probe bug — see
-- tests/db/test_schema_type_hygiene_preflight.py, the canonical caller,
-- which checks information_schema.columns.data_type before running this
-- file and retires the malformed-seed audit the same way it already
-- retired Tier A's and the hook_failures split's.
--
-- Audits: document_aspects.extras, document_aspects.salient_sentences
-- (both -> jsonb).
--
-- COLUMNS EMITTED (one row per audited column):
--   tier                  'B'.
--   table_name / column_name
--   total_rows            count(*) over the whole table.
--   null_count             col IS NULL.
--   empty_string_count      col = ''. The eventual (now shipped) USING
--                          clause is NULLIF(col,'')::jsonb, so '' becomes
--                          NULL and never reaches the cast — benign, unlike
--                          plans.plan_json / topic_links.link_types (see
--                          schema_type_hygiene_preflight_tier_b.sql).
--   non_iso_prefix_count   Always NULL (literal SQL NULL) — Tier-A-only
--                          column, kept for row-shape parity.
--   invalid_cast_count     THE real check: col IS NOT NULL AND col <> ''
--                          AND NOT pg_input_is_valid(col, 'jsonb').
--
-- RLS / MULTI-TENANT VISIBILITY: identical caveats to
-- schema_type_hygiene_preflight_tier_b.sql's own header (document_aspects
-- is ENABLE + FORCE ROW LEVEL SECURITY with the standard tenant_isolation
-- policy, per aspects-001-baseline.xml) — read that file's RLS section
-- before running this one against a live multi-tenant cluster.

SELECT tier, table_name, column_name,
       total_rows, null_count, empty_string_count,
       non_iso_prefix_count, invalid_cast_count
FROM (
    -- ── column: document_aspects.extras ─────────────────────────────────
    SELECT 'B' AS tier, 'document_aspects' AS table_name, 'extras' AS column_name,
           count(*) AS total_rows,
           count(*) FILTER (WHERE extras IS NULL) AS null_count,
           count(*) FILTER (WHERE extras = '') AS empty_string_count,
           NULL::bigint AS non_iso_prefix_count,
           count(*) FILTER (WHERE extras IS NOT NULL AND extras <> ''
                             AND NOT pg_input_is_valid(extras, 'jsonb')) AS invalid_cast_count
    FROM nexus.document_aspects

    UNION ALL
    -- ── column: document_aspects.salient_sentences ──────────────────────
    SELECT 'B', 'document_aspects', 'salient_sentences',
           count(*),
           count(*) FILTER (WHERE salient_sentences IS NULL),
           count(*) FILTER (WHERE salient_sentences = ''),
           NULL::bigint,
           count(*) FILTER (WHERE salient_sentences IS NOT NULL AND salient_sentences <> ''
                             AND NOT pg_input_is_valid(salient_sentences, 'jsonb'))
    FROM nexus.document_aspects
) t
ORDER BY tier, table_name, column_name;
