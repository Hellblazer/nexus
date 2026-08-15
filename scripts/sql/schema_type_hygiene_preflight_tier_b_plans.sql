-- SPDX-License-Identifier: AGPL-3.0-or-later
-- schema_type_hygiene_preflight_tier_b_plans.sql — nexus-cefa1.5 (P4 of the
-- schema type-hygiene arc).
--
-- SPLIT OUT of schema_type_hygiene_preflight_tier_b.sql (nexus-cefa1.1/.2)
-- the moment plans.plan_json / plans.default_bindings converted to jsonb
-- (plans-002-jsonb.xml) — exactly the split that file's own header predicted
-- would be needed the next time one of its remaining columns' phase shipped
-- ("this file must be split further... rather than patched with dynamic
-- SQL"). Mirrors how telemetry-004 (P2) split hook_failures.batch_doc_ids
-- out into schema_type_hygiene_preflight_tier_b_hook_failures.sql, and how
-- aspects-003 (P3) split document_aspects.extras/.salient_sentences out into
-- schema_type_hygiene_preflight_tier_b_document_aspects.sql. Leaves
-- schema_type_hygiene_preflight_tier_b.sql auditing topic_links.link_types
-- only (P5, the last still-text Tier-B column).
--
-- PRE-MIGRATION PROBE — ERRORS BY DESIGN ONCE THESE COLUMNS' OWN PHASE HAS
-- SHIPPED ON THIS CLUSTER (P4, plans-002-jsonb.xml). Once shipped,
-- `pg_input_is_valid(plan_json, 'jsonb')` / `plan_json = ''` (and the same
-- pair for default_bindings) both raise "operator does not exist: jsonb =
-- unknown" instead of returning zero rows. THAT ERROR IS THE CORRECT SIGNAL
-- these columns' audit job is done, not a probe bug — see
-- tests/db/test_schema_type_hygiene_preflight.py, the canonical caller,
-- which checks information_schema.columns.data_type before running this
-- file and retires the malformed-seed audit the same way it already retired
-- Tier A's and the hook_failures / document_aspects splits'.
--
-- Audits: plans.plan_json, plans.default_bindings (both -> jsonb).
--
-- COLUMNS EMITTED (one row per audited column):
--   tier                  'B'.
--   table_name / column_name
--   total_rows            count(*) over the whole table.
--   null_count             col IS NULL.
--   empty_string_count      col = ''. Reported separately from
--                          invalid_cast_count on purpose: plan_json is NOT
--                          NULL (kept NOT NULL per the plan of record) with
--                          NO NULLIF step in its eventual USING clause
--                          (plain ::jsonb) — a non-zero empty_string_count
--                          for plan_json IS itself an ALTER-abort signal
--                          needing a named remediation before P4 ships.
--                          default_bindings DOES get NULLIF(col,'')::jsonb,
--                          so '' becomes NULL there and never reaches the
--                          cast — benign, same shape as the already-shipped
--                          batch_doc_ids/extras/salient_sentences columns.
--   non_iso_prefix_count   Always NULL (literal SQL NULL) — Tier-A-only
--                          column, kept for row-shape parity with Tier A so
--                          a caller merging both tiers' output sees a
--                          uniform column set.
--   invalid_cast_count     THE real check: col IS NOT NULL AND col <> ''
--                          AND NOT pg_input_is_valid(col, 'jsonb').
--
-- WHAT invalid_cast_count DOES NOT CATCH: pg_input_is_valid(col,'jsonb')
-- accepts bare JSON scalars ('1', '"x"', 'null', 'true'). Consumers of
-- plan_json expect an object; a scalar converts cleanly and then breaks the
-- reader (nexus.plans.runner's json.loads(...)["steps"] etc.). Occurrence-
-- time remedy: jsonb_typeof(col) = 'object' spot-check on plan_json, only
-- if the write path is ever shown to have emitted one (per the 2026-08-14
-- directive: prose note, not a new probe column, until a live population
-- shows up).
--
-- ACCEPTANCE (nexus-cefa1.1): every invalid_cast_count must be 0 before its
-- own phase's ALTER TABLE ships. A non-zero count is a required, NAMED
-- remediation decision recorded in T2, not itself a blocker to filing the
-- bead.
--
-- RLS / MULTI-TENANT VISIBILITY: identical caveats to
-- schema_type_hygiene_preflight_tier_b.sql's own header (nexus.plans is
-- ENABLE + FORCE ROW LEVEL SECURITY with the standard tenant_isolation
-- policy, per plans-001-baseline.xml) — read that file's RLS section before
-- running this one against a live multi-tenant cluster.

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
) t
ORDER BY tier, table_name, column_name;
