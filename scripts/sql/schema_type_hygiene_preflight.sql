-- SPDX-License-Identifier: AGPL-3.0-or-later
-- schema_type_hygiene_preflight.sql — nexus-cefa1.1 (P0 pre-flight audit)
--
-- READ-ONLY. No DDL, no DML. Emits ONE ROW PER COLUMN across the 10
-- TEXT columns targeted by the schema type-hygiene arc (epic nexus-cefa1;
-- plan of record T2 nexus/plan-schema-type-hygiene-2026-08-12 [22360]):
--
--   Tier A (4, -> timestamptz): catalog_documents.indexed_at,
--     catalog_documents.bib_enriched_at, catalog_documents.index_started_at,
--     catalog_links.created_at.
--   Tier B (6, -> jsonb): plans.plan_json, plans.default_bindings,
--     topic_links.link_types, document_aspects.extras,
--     document_aspects.salient_sentences, hook_failures.batch_doc_ids.
--
-- COLUMNS EMITTED (one row per audited column):
--   tier                  'A' or 'B'.
--   table_name / column_name
--   total_rows            count(*) over the whole table — denominator
--                          context, not scoped to this column.
--   null_count             col IS NULL.
--   empty_string_count      col = '' (the pervasive empty-string-instead-of-
--                          NULL sentinel). Reported separately from
--                          invalid_cast_count on purpose: where the phase's
--                          USING clause is NULLIF(col,'')::<type> (the 4
--                          Tier-A cols, default_bindings, extras,
--                          salient_sentences, batch_doc_ids) '' becomes NULL
--                          and never reaches the cast. But plans.plan_json
--                          (NOT NULL, kept NOT NULL per plan P4) and
--                          topic_links.link_types (NOT NULL DEFAULT '[]', plan
--                          P5) have NO NULLIF step: ''::jsonb is invalid
--                          input, so for THOSE TWO columns a non-zero
--                          empty_string_count is itself an ALTER-abort
--                          signal and needs a named remediation (repair to
--                          '{}'/'[]', or add NULLIF + DROP NOT NULL) before
--                          P4/P5 ship.
--
-- WHAT invalid_cast_count DOES NOT CATCH (named here so nobody dismisses a
-- clean run as "semantically clean"; per the 2026-08-14 directive these are
-- prose notes, not new probe columns, until a live population shows up):
--   * Tier A: PG's timestamptz input accepts relative keywords ('now',
--     'today', 'yesterday', 'epoch', 'infinity', 'allballs', ...) as VALID,
--     so such a value passes the cast and silently becomes a moving,
--     conversion-time-relative instant. non_iso_prefix_count is the lexical
--     eyeball that catches these; a non-zero lexical hit with a zero cast
--     count still needs a look, not a dismissal.
--   * Tier B: pg_input_is_valid(col,'jsonb') accepts bare JSON scalars
--     ('1', '"x"', 'null', 'true'). Consumers of plan_json/extras/link_types
--     expect an object or array; a scalar converts cleanly and then breaks
--     the reader. Occurrence-time remedy: jsonb_typeof(col) IN ('object',
--     'array') spot-check on the affected column, only if the write path is
--     ever shown to have emitted one.
--   non_iso_prefix_count   Tier A only (NULL for Tier B rows): col IS NOT
--                          NULL AND col <> '' AND col !~ '^[0-9]{4}-' — a
--                          cheap lexical eyeball check. It flags values that
--                          obviously do not start like an ISO timestamp; it
--                          does NOT prove the remaining values parse (that
--                          is invalid_cast_count's job).
--   invalid_cast_count     THE real check — the one each phase's
--                          ALTER COLUMN TYPE ... USING clause will actually
--                          perform: col IS NOT NULL AND col <> '' AND NOT
--                          pg_input_is_valid(col, '<target type>')
--                          ('jsonb' for Tier B, 'timestamptz' for Tier A).
--                          pg_input_is_valid() is PG16+ (this cluster is
--                          PG17) and runs the real input function WITHOUT
--                          raising, so a malformed value becomes a counted
--                          row here instead of aborting the whole probe (or
--                          the eventual ALTER).
--
-- ACCEPTANCE (nexus-cefa1.1): every invalid_cast_count must be 0 before its
-- phase's ALTER TABLE ships. A non-zero count is not itself a blocker to
-- filing the phase bead — it IS a required, NAMED remediation decision
-- (NULLIF fixup, explicit data repair, or a scoped exclusion recorded in the
-- changeset comment, mirroring plans.dimensions' documented exclusion in the
-- plan of record) that must be made and recorded in T2 BEFORE that phase's
-- changeset runs.
--
-- RLS / MULTI-TENANT VISIBILITY — READ BEFORE RUNNING AGAINST A LIVE
-- MULTI-TENANT CLUSTER (e.g. the cloud engine):
-- All six source tables are ENABLE + FORCE ROW LEVEL SECURITY with the
-- standard `tenant_isolation` policy (`tenant_id = current_setting
-- ('nexus.tenant', true)`) — verified against catalog-001-baseline.xml,
-- plans-001-baseline.xml, taxonomy-001-baseline.xml,
-- aspects-001-baseline.xml, telemetry-001-baseline.xml. A plain
-- tenant-scoped application connection sees ONE tenant's rows and silently
-- UNDERCOUNTS every column here (nexus-vounk: demonstrated 0-vs-9 on
-- exactly this failure shape) — a false-clean, not a legitimately empty
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
--       mistake a permission failure for a clean result. Per RDR-182 P2.1
--       (`src/nexus/db/diag_connection.py::run_diagnostic_sql`), nexus_diag
--       is BYPASSRLS precisely so a legacy-era session sees every tenant
--       with no per-tenant looping needed.
--   (c) NO BYPASSRLS access available at all: loop per tenant instead. For
--       each known tenant_id, run in its own transaction:
--         BEGIN;
--         SET LOCAL "nexus.tenant" = '<tenant_id>';
--         -- run the SELECT below; sum/union the per-tenant results
--         COMMIT;
--       This is the NOBYPASSRLS discipline nexus-cefa1.1 names for the
--       cloud relay — prefer (a)/(b) when available; use (c) only when
--       neither is, since it requires an out-of-band tenant roster and N
--       round trips instead of one.
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
    -- ---- Tier A: TEXT timestamp columns -> timestamptz ----
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
    SELECT 'A', 'catalog_links', 'created_at',
           count(*),
           count(*) FILTER (WHERE created_at IS NULL),
           count(*) FILTER (WHERE created_at = ''),
           count(*) FILTER (WHERE created_at IS NOT NULL AND created_at <> ''
                             AND created_at !~ '^[0-9]{4}-'),
           count(*) FILTER (WHERE created_at IS NOT NULL AND created_at <> ''
                             AND NOT pg_input_is_valid(created_at, 'timestamptz'))
    FROM nexus.catalog_links

    UNION ALL
    -- ---- Tier B: TEXT JSON-as-text columns -> jsonb ----
    SELECT 'B', 'plans', 'plan_json',
           count(*),
           count(*) FILTER (WHERE plan_json IS NULL),
           count(*) FILTER (WHERE plan_json = ''),
           NULL::bigint,
           count(*) FILTER (WHERE plan_json IS NOT NULL AND plan_json <> ''
                             AND NOT pg_input_is_valid(plan_json, 'jsonb'))
    FROM nexus.plans

    UNION ALL
    SELECT 'B', 'plans', 'default_bindings',
           count(*),
           count(*) FILTER (WHERE default_bindings IS NULL),
           count(*) FILTER (WHERE default_bindings = ''),
           NULL::bigint,
           count(*) FILTER (WHERE default_bindings IS NOT NULL AND default_bindings <> ''
                             AND NOT pg_input_is_valid(default_bindings, 'jsonb'))
    FROM nexus.plans

    UNION ALL
    SELECT 'B', 'topic_links', 'link_types',
           count(*),
           count(*) FILTER (WHERE link_types IS NULL),
           count(*) FILTER (WHERE link_types = ''),
           NULL::bigint,
           count(*) FILTER (WHERE link_types IS NOT NULL AND link_types <> ''
                             AND NOT pg_input_is_valid(link_types, 'jsonb'))
    FROM nexus.topic_links

    UNION ALL
    SELECT 'B', 'document_aspects', 'extras',
           count(*),
           count(*) FILTER (WHERE extras IS NULL),
           count(*) FILTER (WHERE extras = ''),
           NULL::bigint,
           count(*) FILTER (WHERE extras IS NOT NULL AND extras <> ''
                             AND NOT pg_input_is_valid(extras, 'jsonb'))
    FROM nexus.document_aspects

    UNION ALL
    SELECT 'B', 'document_aspects', 'salient_sentences',
           count(*),
           count(*) FILTER (WHERE salient_sentences IS NULL),
           count(*) FILTER (WHERE salient_sentences = ''),
           NULL::bigint,
           count(*) FILTER (WHERE salient_sentences IS NOT NULL AND salient_sentences <> ''
                             AND NOT pg_input_is_valid(salient_sentences, 'jsonb'))
    FROM nexus.document_aspects

    UNION ALL
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
