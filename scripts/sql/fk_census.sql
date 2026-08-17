-- SPDX-License-Identifier: AGPL-3.0-or-later
-- fk_census.sql — RDR-194 P0 mechanical FK census (bead nexus-tk070).
--
-- READ-ONLY. Every SELECT below is information_schema / pg_catalog derived —
-- there is no hand-typed table/column list anywhere in this file. Running it
-- twice, or running it against a schema that has gained/lost columns since
-- this file was written, must change the OUTPUT, never require an edit HERE.
--
-- Scope: schemas 'nexus' and 't1' are live (in-scope); 'staging' rows are
-- returned but labelled census_scope = 'EXEMPT' (RDR-194 Problem Statement:
-- "the staging schema (deliberately typeless landing zone)" is explicitly
-- excluded from the decision, not from the census itself — the exemption
-- must be visible in the output, not silently absent from it).
--
-- Five result sets, run as five independent SELECTs (psql -f prints all of
-- them in order; each is also independently re-runnable). A caller wanting
-- one result set only can copy the relevant CTE block out — nothing here
-- depends on session-level temp objects surviving between the SELECTs.
--
-- ============================================================================
-- RESULT SET 1: join-column census.
-- ============================================================================
-- A "join column" is any nexus/t1/staging column whose name either:
--   (a) ends in '_id' (regex \_id$), or
--   (b) is one of the RDR-194-named shape-carrying names: tumbler,
--       from_tumbler, to_tumbler, doc_id, chash, chunk_id, collection,
--       physical_collection, parent_id, topic_id, job_id, tenant_id
--       (doc_id/topic_id/job_id/tenant_id already match (a); listed for
--       clarity — the OR is harmless), or
--   (c) shares its name with some OTHER table's single-column PK or UNIQUE
--       constraint column (self-join heuristic: catches e.g. a bare
--       "chash" or "name" column that isn't shaped like *_id but is
--       plausibly a join key because some table's identity IS that name).
--
-- For each: live type/nullability/default, whether a FK already exists on
-- that exact column (target table+column, ON DELETE action, VALIDATED /
-- NOT VALID), whether a plausible target (a table whose PK or UNIQUE is
-- exactly that column name, or the RDR-194-documented tumbler/chash targets)
-- exists, and a first-cut class:
--   fk_enforced      — FK exists and pg_constraint.convalidated = true.
--   fk_not_valid     — FK exists but convalidated = false (NOT VALID).
--   fk_able_now      — no FK yet, but a plausible single-column target
--                       exists and this column's data_type matches the
--                       target's data_type (a same-type FK could be added
--                       without a conversion step first).
--   needs_design     — no FK, a plausible target exists by NAME, but the
--                       data_type differs (e.g. hex TEXT vs bytea) or the
--                       column is tenant_id (composite-FK judgment, not a
--                       plain single-column candidate) — a design decision
--                       is required before an FK can be added.
--   no_plausible_target — no FK, and no PK/UNIQUE column anywhere in scope
--                       shares this column's name; likely not an FK
--                       candidate at all (kept in output for eyeball
--                       review, not silently dropped).
--   exempt           — column lives in the staging schema.
--
-- "deliberately_loose" is NOT computed here — that requires reading the
-- Problem Statement's in-tree rationale citations (catalog_links.created_at
-- empty-string sentinel, pdf_chunks.embedding, plans.dimensions byte-
-- equality identity) which are not schema-visible facts. The RDR text
-- applies that label on top of this census's raw classes.

WITH scope_schemas AS (
    SELECT unnest(ARRAY['nexus', 't1', 'staging']) AS schema_name
),
all_cols AS (
    SELECT c.table_schema, c.table_name, c.column_name, c.data_type,
           c.udt_name, c.is_nullable, c.column_default,
           c.character_maximum_length
    FROM information_schema.columns c
    JOIN scope_schemas s ON s.schema_name = c.table_schema
),
-- Single-column PK/UNIQUE targets across the same three schemas, used both
-- to drive the name-equality heuristic and to test plausible-target
-- existence + type-compatibility for the fk_able_now/needs_design split.
pk_unique_cols AS (
    SELECT n.nspname AS table_schema,
           c.relname AS table_name,
           a.attname AS column_name,
           con.contype AS con_type,
           format_type(a.atttypid, a.atttypmod) AS pg_type
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
    WHERE con.contype IN ('p', 'u')
      AND cardinality(con.conkey) = 1
      AND n.nspname IN ('nexus', 't1', 'staging')
),
join_col_candidates AS (
    SELECT DISTINCT ac.*
    FROM all_cols ac
    WHERE ac.column_name ~ '_id$'
       OR ac.column_name IN (
            'tumbler', 'from_tumbler', 'to_tumbler', 'doc_id', 'chash',
            'chunk_id', 'collection', 'physical_collection', 'parent_id',
            'topic_id', 'job_id', 'tenant_id'
          )
       OR EXISTS (
            SELECT 1 FROM pk_unique_cols pu
            WHERE pu.column_name = ac.column_name
              AND NOT (pu.table_schema = ac.table_schema AND pu.table_name = ac.table_name)
          )
),
-- Existing FK constraints keyed by the exact (schema,table,column) of their
-- FIRST referencing column — good enough to answer "does column X already
-- carry an FK" for both single- and multi-column FKs (a composite FK like
-- fk_catalog_chunks_chunk(tenant_id, collection, chash) will show up under
-- all three of its referencing columns via the unnest below, which is the
-- desired behaviour: querying chash's row should reveal that chash is
-- already inside a composite FK).
existing_fks AS (
    SELECT n.nspname AS table_schema,
           c.relname AS table_name,
           a.attname AS column_name,
           con.conname AS constraint_name,
           tn.nspname AS target_schema,
           tc.relname AS target_table,
           array_agg(ta.attname ORDER BY tk.ord) AS target_columns,
           con.convalidated AS validated,
           CASE con.confdeltype
               WHEN 'a' THEN 'NO ACTION'
               WHEN 'r' THEN 'RESTRICT'
               WHEN 'c' THEN 'CASCADE'
               WHEN 'n' THEN 'SET NULL'
               WHEN 'd' THEN 'SET DEFAULT'
           END AS on_delete
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_class tc ON tc.oid = con.confrelid
    JOIN pg_namespace tn ON tn.oid = tc.relnamespace
    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
    JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS tk(attnum, ord) ON true
    JOIN pg_attribute ta ON ta.attrelid = tc.oid AND ta.attnum = tk.attnum
    WHERE con.contype = 'f'
      AND n.nspname IN ('nexus', 't1', 'staging')
    GROUP BY n.nspname, c.relname, a.attname, con.conname, tn.nspname,
             tc.relname, con.convalidated, con.confdeltype
),
-- Plausible target: a PK/UNIQUE column elsewhere sharing this column's name
-- (picks the first alphabetically if more than one — informational only,
-- flagged via target_ambiguous).
plausible_targets AS (
    SELECT jc.table_schema, jc.table_name, jc.column_name,
           (array_agg(pu.table_schema || '.' || pu.table_name || '(' || pu.column_name || ')'
                      ORDER BY pu.table_schema, pu.table_name))[1] AS best_target,
           (array_agg(pu.pg_type ORDER BY pu.table_schema, pu.table_name))[1] AS target_pg_type,
           count(*) AS n_candidate_targets
    FROM join_col_candidates jc
    JOIN pk_unique_cols pu
      ON pu.column_name = jc.column_name
     AND NOT (pu.table_schema = jc.table_schema AND pu.table_name = jc.table_name)
    GROUP BY jc.table_schema, jc.table_name, jc.column_name
)
SELECT
    jc.table_schema,
    jc.table_name,
    jc.column_name,
    jc.data_type,
    jc.is_nullable,
    jc.column_default,
    ef.constraint_name AS existing_fk_name,
    ef.target_schema || '.' || ef.target_table || '(' || array_to_string(ef.target_columns, ',') || ')' AS existing_fk_target,
    ef.on_delete AS existing_fk_on_delete,
    ef.validated AS existing_fk_validated,
    pt.best_target AS plausible_target,
    pt.n_candidate_targets,
    CASE
        WHEN jc.table_schema = 'staging' THEN 'exempt'
        WHEN ef.constraint_name IS NOT NULL AND ef.validated THEN 'fk_enforced'
        WHEN ef.constraint_name IS NOT NULL AND NOT ef.validated THEN 'fk_not_valid'
        WHEN ef.constraint_name IS NULL AND pt.best_target IS NOT NULL
             AND jc.column_name <> 'tenant_id'
             AND jc.data_type = (
                 SELECT data_type FROM all_cols ac2
                 WHERE ac2.table_schema || '.' || ac2.table_name || '(' || ac2.column_name || ')' = pt.best_target
                 LIMIT 1
             )
             THEN 'fk_able_now'
        WHEN ef.constraint_name IS NULL AND pt.best_target IS NOT NULL
             THEN 'needs_design'
        ELSE 'no_plausible_target'
    END AS census_class
FROM join_col_candidates jc
LEFT JOIN existing_fks ef
       ON ef.table_schema = jc.table_schema AND ef.table_name = jc.table_name AND ef.column_name = jc.column_name
LEFT JOIN plausible_targets pt
       ON pt.table_schema = jc.table_schema AND pt.table_name = jc.table_name AND pt.column_name = jc.column_name
ORDER BY
    CASE WHEN jc.table_schema = 'staging' THEN 1 ELSE 0 END,
    jc.table_schema, jc.table_name, jc.column_name;

-- ============================================================================
-- RESULT SET 2: chash-encoding census.
-- ============================================================================
-- Every column named 'chash', ending in '_chash', or documented in-tree as
-- carrying a chunk chash value (doc_id in topic_assignments per RDR-194
-- Problem Statement item 1). Reports live type/width so the three-encoding
-- finding (bytea-32, hex TEXT 32-or-64, hex TEXT 64) is a query result, not
-- an assertion.
SELECT
    c.table_schema, c.table_name, c.column_name,
    c.data_type, c.udt_name, c.character_maximum_length,
    (SELECT string_agg(pg_get_constraintdef(con.oid), ' | ')
     FROM pg_constraint con
     JOIN pg_class pc ON pc.oid = con.conrelid
     JOIN pg_namespace pn ON pn.oid = pc.relnamespace
     WHERE con.contype = 'c'
       AND pn.nspname = c.table_schema AND pc.relname = c.table_name
       AND pg_get_constraintdef(con.oid) ILIKE '%' || c.column_name || '%'
    ) AS check_constraints
FROM information_schema.columns c
WHERE c.table_schema IN ('nexus', 't1', 'staging')
  AND (c.column_name = 'chash'
       OR c.column_name ~ '_chash$'
       OR c.column_name ~ '^chash_'
       OR (c.table_name = 'topic_assignments' AND c.column_name = 'doc_id'))
ORDER BY c.table_schema, c.table_name, c.column_name;

-- ============================================================================
-- RESULT SET 3: doc_id-semantics census.
-- ============================================================================
-- Every table carrying a doc_id column: is it FK-anchored (tumbler +
-- catalog_documents FK), unconstrained, or something else. Derived from
-- RESULT SET 1's own existing_fks join, filtered to column_name = 'doc_id'.
SELECT
    c.table_schema, c.table_name, c.data_type AS doc_id_type,
    ef.constraint_name AS fk_name,
    ef.target_schema || '.' || ef.target_table AS fk_target,
    ef.validated AS fk_validated,
    CASE
        WHEN ef.constraint_name IS NOT NULL THEN 'tumbler-with-fk'
        WHEN c.data_type IN ('bytea') THEN 'chash-bytea-unconstrained'
        WHEN c.data_type IN ('text', 'character varying') THEN 'hex-chash-or-tumbler-text-unconstrained'
        ELSE 'unconstrained-other'
    END AS doc_id_semantics_class
FROM information_schema.columns c
LEFT JOIN pg_constraint con
       ON con.contype = 'f'
LEFT JOIN LATERAL (
    SELECT con2.conname, tn.nspname AS target_schema, tc.relname AS target_table, con2.convalidated AS validated
    FROM pg_constraint con2
    JOIN pg_class rc ON rc.oid = con2.conrelid
    JOIN pg_namespace rn ON rn.oid = rc.relnamespace
    JOIN pg_class tc ON tc.oid = con2.confrelid
    JOIN pg_namespace tn ON tn.oid = tc.relnamespace
    JOIN LATERAL unnest(con2.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = rc.oid AND a.attnum = k.attnum
    WHERE con2.contype = 'f'
      AND rn.nspname = c.table_schema AND rc.relname = c.table_name
      AND a.attname = 'doc_id'
    LIMIT 1
) ef ON true
WHERE c.column_name = 'doc_id'
  AND c.table_schema IN ('nexus', 't1', 'staging')
ORDER BY c.table_schema, c.table_name;

-- ============================================================================
-- RESULT SET 4: tenant-in-PK census.
-- ============================================================================
-- Per table in scope: its PK columns, whether tenant_id is in the PK or in
-- some UNIQUE constraint/index, and whether an RLS policy exists.
WITH pk_cols AS (
    SELECT n.nspname AS table_schema, c.relname AS table_name,
           array_agg(a.attname ORDER BY k.ord) AS pk_columns
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
    WHERE con.contype = 'p' AND n.nspname IN ('nexus', 't1', 'staging')
    GROUP BY n.nspname, c.relname
),
unique_has_tenant AS (
    SELECT n.nspname AS table_schema, c.relname AS table_name, bool_or(true) AS has
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
    WHERE con.contype = 'u' AND n.nspname IN ('nexus', 't1', 'staging')
      AND a.attname = 'tenant_id'
    GROUP BY n.nspname, c.relname
),
rls AS (
    SELECT n.nspname AS table_schema, c.relname AS table_name,
           c.relrowsecurity AS rls_enabled, c.relforcerowsecurity AS rls_forced,
           (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS n_policies
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p') AND n.nspname IN ('nexus', 't1', 'staging')
),
has_tenant_col AS (
    SELECT table_schema, table_name FROM information_schema.columns
    WHERE column_name = 'tenant_id' AND table_schema IN ('nexus', 't1', 'staging')
)
SELECT
    r.table_schema, r.table_name,
    pk.pk_columns,
    (htc.table_name IS NOT NULL) AS has_tenant_id_column,
    (pk.pk_columns IS NOT NULL AND 'tenant_id' = ANY(pk.pk_columns)) AS tenant_in_pk,
    COALESCE(uht.has, false) AS tenant_in_some_unique,
    r.rls_enabled, r.rls_forced, r.n_policies
FROM rls r
LEFT JOIN pk_cols pk ON pk.table_schema = r.table_schema AND pk.table_name = r.table_name
LEFT JOIN unique_has_tenant uht ON uht.table_schema = r.table_schema AND uht.table_name = r.table_name
LEFT JOIN has_tenant_col htc ON htc.table_schema = r.table_schema AND htc.table_name = r.table_name
WHERE htc.table_name IS NOT NULL
ORDER BY r.table_schema, tenant_in_pk, r.table_name;

-- ============================================================================
-- RESULT SET 5: TTL census.
-- ============================================================================
-- Every column named 'ttl' or 'ttl_days' (or matching '_ttl$'/'^ttl_'):
-- type, nullability, default, and any CHECK constraint text mentioning it
-- (a proxy for documented null-semantics — the actual reader/writer
-- semantics are a code-site question, not a schema-visible one, and are
-- left to the Research Findings prose).
SELECT
    c.table_schema, c.table_name, c.column_name,
    c.data_type, c.is_nullable, c.column_default,
    (SELECT string_agg(pg_get_constraintdef(con.oid), ' | ')
     FROM pg_constraint con
     JOIN pg_class pc ON pc.oid = con.conrelid
     JOIN pg_namespace pn ON pn.oid = pc.relnamespace
     WHERE con.contype = 'c'
       AND pn.nspname = c.table_schema AND pc.relname = c.table_name
       AND pg_get_constraintdef(con.oid) ILIKE '%' || c.column_name || '%'
    ) AS check_constraints
FROM information_schema.columns c
WHERE c.table_schema IN ('nexus', 't1', 'staging')
  AND (c.column_name IN ('ttl', 'ttl_days') OR c.column_name ~ '_ttl$' OR c.column_name ~ '^ttl_')
ORDER BY c.table_schema, c.table_name, c.column_name;
