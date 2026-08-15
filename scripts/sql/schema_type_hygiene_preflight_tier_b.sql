-- SPDX-License-Identifier: AGPL-3.0-or-later
-- schema_type_hygiene_preflight_tier_b.sql — nexus-cefa1.1/.2 (P0 pre-flight
-- audit of the schema type-hygiene arc, Tier B half).
--
-- PRE-MIGRATION PROBE, PER COLUMN — ERRORS BY DESIGN FOR ANY COLUMN WHOSE
-- OWN PHASE HAS SHIPPED ON THIS CLUSTER. Unlike Tier A (one changeset
-- converts all four columns together), Tier B's six columns land across
-- FOUR SEPARATE, INDEPENDENT phases:
--   P2 (hook_failures.batch_doc_ids)
--   P3 (document_aspects.extras, document_aspects.salient_sentences)
--   P4 (plans.plan_json, plans.default_bindings)
--   P5 (topic_links.link_types)
-- So this file can be in a PARTIALLY migrated state — some columns still
-- TEXT, others already jsonb — for long stretches between phases. Once a
-- given column's phase has shipped, its predicates below (`col = ''`, `NOT
-- pg_input_is_valid(col,'jsonb')`) raise "operator does not exist: jsonb =
-- unknown" (or similar) instead of returning zero rows. THAT ERROR IS THE
-- CORRECT SIGNAL that column's audit job is done, not a probe bug.
-- Callers MUST check `information_schema.columns.data_type` per column
-- before running the branch that touches it — see
-- tests/db/test_schema_type_hygiene_preflight.py, the canonical caller,
-- which classifies each column as still-TEXT (audit) or already-converted
-- (assert the new type) and only ever runs this file's UNION ALL as a whole
-- while EVERY column in it is still TEXT. The FIRST column here to convert
-- (P2's hook_failures.batch_doc_ids) breaks whole-file execution — at that
-- point this file must be split further (drop that column's branch into
-- its own UNION ALL, mirroring how catalog-031 already split Tier A out of
-- the original combined probe) rather than patched with dynamic SQL.
--
-- Audits: plans.plan_json, plans.default_bindings, topic_links.link_types,
-- document_aspects.extras, document_aspects.salient_sentences,
-- hook_failures.batch_doc_ids (all -> jsonb).
--
-- COLUMNS EMITTED (one row per audited column):
--   tier                  'B'.
--   table_name / column_name
--   total_rows            count(*) over the whole table.
--   null_count             col IS NULL.
--   empty_string_count      col = ''. Reported separately from
--                          invalid_cast_count on purpose: for
--                          default_bindings/extras/salient_sentences/
--                          batch_doc_ids the eventual USING clause is
--                          NULLIF(col,'')::jsonb, so '' becomes NULL and
--                          never reaches the cast — benign. But
--                          plans.plan_json (NOT NULL, kept NOT NULL per the
--                          plan of record) and topic_links.link_types (NOT
--                          NULL DEFAULT '[]') have NO NULLIF step:
--                          ''::jsonb is invalid input, so for THOSE TWO
--                          columns a non-zero empty_string_count IS itself
--                          an ALTER-abort signal needing a named
--                          remediation before that phase ships.
--   non_iso_prefix_count   Always NULL (literal SQL NULL) — Tier-A-only
--                          column, kept for row-shape parity with Tier A so
--                          a caller merging both tiers' output sees a
--                          uniform column set.
--   invalid_cast_count     THE real check: col IS NOT NULL AND col <> ''
--                          AND NOT pg_input_is_valid(col, 'jsonb').
--
-- WHAT invalid_cast_count DOES NOT CATCH: pg_input_is_valid(col,'jsonb')
-- accepts bare JSON scalars ('1', '"x"', 'null', 'true'). Consumers of
-- plan_json/extras/link_types expect an object or array; a scalar converts
-- cleanly and then breaks the reader. Occurrence-time remedy:
-- jsonb_typeof(col) IN ('object','array') spot-check on the affected
-- column, only if the write path is ever shown to have emitted one (per the
-- 2026-08-14 directive: prose note, not a new probe column, until a live
-- population shows up).
--
-- ACCEPTANCE (nexus-cefa1.1): every invalid_cast_count must be 0 before its
-- own phase's ALTER TABLE ships. A non-zero count is a required, NAMED
-- remediation decision recorded in T2, not itself a blocker to filing the bead.
--
-- RLS / MULTI-TENANT VISIBILITY — READ BEFORE RUNNING AGAINST A LIVE
-- MULTI-TENANT CLUSTER (e.g. the cloud engine):
-- All source tables are ENABLE + FORCE ROW LEVEL SECURITY with the standard
-- `tenant_isolation` policy (`tenant_id = current_setting('nexus.tenant',
-- true)`) — verified against plans-001-baseline.xml, taxonomy-001-
-- baseline.xml, aspects-001-baseline.xml, telemetry-001-baseline.xml. A
-- plain tenant-scoped application connection sees ONE tenant's rows and
-- silently UNDERCOUNTS every column here (nexus-vounk: demonstrated 0-vs-9
-- on exactly this failure shape) — a false-clean, not a legitimately empty
-- store. Run this file through ONE of:
--
--   (a) A BYPASSRLS connection — a superuser session, or (on the local
--       dev/CI substrate) the OS-superuser role tests/_engine_substrate.py
--       provisions (`pg_user`, trust auth, no RLS at all: see
--       `tests/db/test_schema_type_hygiene_preflight.py`, which uses
--       exactly this path).
--   (b) The `nexus_diag` role IF AND ONLY IF it is still in the "legacy
--       era" for this cluster (direct SELECT + BYPASSRLS granted —
--       grants-nexus-diag.xml's first changeset). CHECK THE ERA FIRST:
--       `grants-nexus-diag-2` (same file) REVOKES nexus_diag's direct table
--       SELECT once the superuser provisioning path creates the
--       `nexus.diag_chash_conformance` view (VIEW era) — in that era
--       nexus_diag can only read these tables through blessed
--       count-by-construction views, and this file will report
--       "permission denied" for every statement, NOT zero counts. Do not
--       mistake a permission failure for a clean result.
--   (c) NO BYPASSRLS access available at all: loop per tenant instead. For
--       each known tenant_id, run in its own transaction:
--         BEGIN;
--         SET LOCAL "nexus.tenant" = '<tenant_id>';
--         -- run the SELECT below; sum/union the per-tenant results
--         COMMIT;
--       Prefer (a)/(b) when available; use (c) only when neither is.
--
-- Do NOT run this file as a plain tenant-scoped nexus_svc / application
-- connection with no GUC and no BYPASSRLS: every FORCE-RLS table then
-- evaluates `current_setting('nexus.tenant', true)` as NULL for a raw psql
-- session, matching ZERO rows for every table — a false-clean, not a
-- legitimately empty store.

SELECT tier, table_name, column_name,
       total_rows, null_count, empty_string_count,
       non_iso_prefix_count, invalid_cast_count
FROM (
    -- ── column: plans.plan_json ────────────────────────────────────────
    SELECT 'B' AS tier, 'plans' AS table_name, 'plan_json' AS column_name,
           count(*) AS total_rows,
           count(*) FILTER (WHERE plan_json IS NULL) AS null_count,
           count(*) FILTER (WHERE plan_json = '') AS empty_string_count,
           NULL::bigint AS non_iso_prefix_count,
           count(*) FILTER (WHERE plan_json IS NOT NULL AND plan_json <> ''
                             AND NOT pg_input_is_valid(plan_json, 'jsonb')) AS invalid_cast_count
    FROM nexus.plans

    UNION ALL
    -- ── column: plans.default_bindings ──────────────────────────────────
    SELECT 'B', 'plans', 'default_bindings',
           count(*),
           count(*) FILTER (WHERE default_bindings IS NULL),
           count(*) FILTER (WHERE default_bindings = ''),
           NULL::bigint,
           count(*) FILTER (WHERE default_bindings IS NOT NULL AND default_bindings <> ''
                             AND NOT pg_input_is_valid(default_bindings, 'jsonb'))
    FROM nexus.plans

    UNION ALL
    -- ── column: topic_links.link_types ──────────────────────────────────
    SELECT 'B', 'topic_links', 'link_types',
           count(*),
           count(*) FILTER (WHERE link_types IS NULL),
           count(*) FILTER (WHERE link_types = ''),
           NULL::bigint,
           count(*) FILTER (WHERE link_types IS NOT NULL AND link_types <> ''
                             AND NOT pg_input_is_valid(link_types, 'jsonb'))
    FROM nexus.topic_links

    UNION ALL
    -- ── column: document_aspects.extras ─────────────────────────────────
    SELECT 'B', 'document_aspects', 'extras',
           count(*),
           count(*) FILTER (WHERE extras IS NULL),
           count(*) FILTER (WHERE extras = ''),
           NULL::bigint,
           count(*) FILTER (WHERE extras IS NOT NULL AND extras <> ''
                             AND NOT pg_input_is_valid(extras, 'jsonb'))
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

    UNION ALL
    -- ── column: hook_failures.batch_doc_ids ─────────────────────────────
    SELECT 'B', 'hook_failures', 'batch_doc_ids',
           count(*),
           count(*) FILTER (WHERE batch_doc_ids IS NULL),
           count(*) FILTER (WHERE batch_doc_ids = ''),
           NULL::bigint,
           count(*) FILTER (WHERE batch_doc_ids IS NOT NULL AND batch_doc_ids <> ''
                             AND NOT pg_input_is_valid(batch_doc_ids, 'jsonb'))
    FROM nexus.hook_failures
) t
ORDER BY tier, table_name, column_name;
