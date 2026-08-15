-- SPDX-License-Identifier: AGPL-3.0-or-later
-- schema_type_hygiene_preflight_tier_a.sql — nexus-cefa1.1/.2 (P0 pre-flight
-- audit of the schema type-hygiene arc, Tier A half).
--
-- PRE-MIGRATION PROBE — ERRORS BY DESIGN ONCE ITS PHASE HAS SHIPPED ON THIS
-- CLUSTER. This file's four columns are ALL audited (and ALL converted) by
-- ONE changeset (catalog-031-1-documents-temporal / catalog-031-2-links-
-- created-at, bead nexus-cefa1.2) — there is no partial-migration state for
-- Tier A the way there is for Tier B below. Once that changeset has run,
-- indexed_at/bib_enriched_at/index_started_at/created_at are timestamptz,
-- and every predicate below (`col = ''`, `col !~ ...`,
-- `pg_input_is_valid(col, 'timestamptz')`) raises a Postgres type error —
-- "operator does not exist: timestamp with time zone = unknown" or similar —
-- rather than returning zero rows. THAT ERROR IS THE CORRECT, EXPECTED
-- SIGNAL that this probe's job is done, not a probe bug to work around.
-- Callers MUST check `information_schema.columns.data_type` for these four
-- columns before running this file at all — see
-- tests/db/test_schema_type_hygiene_preflight.py, which does exactly that
-- and is the canonical caller.
--
-- Audits: catalog_documents.indexed_at, catalog_documents.bib_enriched_at,
-- catalog_documents.index_started_at, catalog_links.created_at (all -> timestamptz).
--
-- COLUMNS EMITTED (one row per audited column):
--   tier                  'A'.
--   table_name / column_name
--   total_rows            count(*) over the whole table — denominator
--                          context, not scoped to this column.
--   null_count             col IS NULL.
--   empty_string_count      col = '' (the pervasive empty-string-instead-of-
--                          NULL sentinel). The eventual USING clause is
--                          NULLIF(col,'')::timestamptz, so '' becomes NULL
--                          and never reaches the cast — a non-zero
--                          empty_string_count here is expected and benign,
--                          not an abort signal (contrast Tier B's
--                          plan_json/link_types, which have no NULLIF step).
--   non_iso_prefix_count   col IS NOT NULL AND col <> '' AND
--                          col !~ '^[0-9]{4}-' — a cheap lexical eyeball
--                          check. It flags values that obviously do not
--                          start like an ISO timestamp; it does NOT prove
--                          the remaining values parse (that is
--                          invalid_cast_count's job).
--   invalid_cast_count     THE real check — the one the ALTER COLUMN TYPE
--                          ... USING clause will actually perform: col IS
--                          NOT NULL AND col <> '' AND NOT
--                          pg_input_is_valid(col, 'timestamptz').
--                          pg_input_is_valid() is PG16+ (this cluster is
--                          PG17) and runs the real input function WITHOUT
--                          raising, so a malformed value becomes a counted
--                          row here instead of aborting the whole probe (or
--                          the eventual ALTER).
--
-- GENUINE PROBE FINDING (nexus-cefa1.1, still true, kept for the historical
-- record even though this block no longer runs post-migration): PostgreSQL's
-- timestamptz input grammar recognizes a small set of relative keywords
-- ('now', 'today', 'yesterday', 'epoch', 'infinity', '-infinity',
-- 'allballs') as VALID input, so a value like 'yesterday' fails the lexical
-- ISO-prefix check but PASSES pg_input_is_valid — it would have silently
-- converted to a moving, conversion-time-relative instant under the
-- eventual ALTER. non_iso_prefix_count and invalid_cast_count answer
-- different questions and must not be conflated.
--
-- ACCEPTANCE (nexus-cefa1.1): every invalid_cast_count must be 0 before this
-- phase's ALTER TABLE ships. A non-zero count is a required, NAMED
-- remediation decision recorded in T2, not itself a blocker to filing the bead.
--
-- RLS / MULTI-TENANT VISIBILITY: see schema_type_hygiene_preflight_tier_b.sql
-- header — identical discipline applies (BYPASSRLS / nexus_diag-legacy-era /
-- per-tenant loop). Not repeated here to keep the two files' divergence
-- (columns audited) the only thing that differs at a glance.

SELECT tier, table_name, column_name,
       total_rows, null_count, empty_string_count,
       non_iso_prefix_count, invalid_cast_count
FROM (
    -- ── column: catalog_documents.indexed_at ──────────────────────────────
    SELECT 'A' AS tier, 'catalog_documents' AS table_name, 'indexed_at' AS column_name,
           count(*) AS total_rows,
           count(*) FILTER (WHERE indexed_at IS NULL) AS null_count,
           count(*) FILTER (WHERE indexed_at = '') AS empty_string_count,
           count(*) FILTER (WHERE indexed_at IS NOT NULL AND indexed_at <> ''
                             AND indexed_at !~ '^[0-9]{4}-') AS non_iso_prefix_count,
           count(*) FILTER (WHERE indexed_at IS NOT NULL AND indexed_at <> ''
                             AND NOT pg_input_is_valid(indexed_at, 'timestamptz')) AS invalid_cast_count
    FROM nexus.catalog_documents

    UNION ALL
    -- ── column: catalog_documents.bib_enriched_at ─────────────────────────
    SELECT 'A', 'catalog_documents', 'bib_enriched_at',
           count(*),
           count(*) FILTER (WHERE bib_enriched_at IS NULL),
           count(*) FILTER (WHERE bib_enriched_at = ''),
           count(*) FILTER (WHERE bib_enriched_at IS NOT NULL AND bib_enriched_at <> ''
                             AND bib_enriched_at !~ '^[0-9]{4}-'),
           count(*) FILTER (WHERE bib_enriched_at IS NOT NULL AND bib_enriched_at <> ''
                             AND NOT pg_input_is_valid(bib_enriched_at, 'timestamptz'))
    FROM nexus.catalog_documents

    UNION ALL
    -- ── column: catalog_documents.index_started_at ────────────────────────
    SELECT 'A', 'catalog_documents', 'index_started_at',
           count(*),
           count(*) FILTER (WHERE index_started_at IS NULL),
           count(*) FILTER (WHERE index_started_at = ''),
           count(*) FILTER (WHERE index_started_at IS NOT NULL AND index_started_at <> ''
                             AND index_started_at !~ '^[0-9]{4}-'),
           count(*) FILTER (WHERE index_started_at IS NOT NULL AND index_started_at <> ''
                             AND NOT pg_input_is_valid(index_started_at, 'timestamptz'))
    FROM nexus.catalog_documents

    UNION ALL
    -- ── column: catalog_links.created_at ──────────────────────────────────
    SELECT 'A', 'catalog_links', 'created_at',
           count(*),
           count(*) FILTER (WHERE created_at IS NULL),
           count(*) FILTER (WHERE created_at = ''),
           count(*) FILTER (WHERE created_at IS NOT NULL AND created_at <> ''
                             AND created_at !~ '^[0-9]{4}-'),
           count(*) FILTER (WHERE created_at IS NOT NULL AND created_at <> ''
                             AND NOT pg_input_is_valid(created_at, 'timestamptz'))
    FROM nexus.catalog_links
) t
ORDER BY tier, table_name, column_name;
