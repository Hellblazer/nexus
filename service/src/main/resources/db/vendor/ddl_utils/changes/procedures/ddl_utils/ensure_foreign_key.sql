CREATE OR REPLACE PROCEDURE ddl_utils.ensure_foreign_key(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_constraint_name ddl_utils.non_null_text,
    i_column_names ddl_utils.non_empty_non_null_text_array,
    i_referenced_schema_name ddl_utils.non_null_text,
    i_referenced_table_name ddl_utils.non_null_text,
    i_referenced_column_names ddl_utils.non_empty_non_null_text_array
)
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
DECLARE
    l_settings            ddl_utils.lock_settings;
    l_relation            regclass;
    l_referenced_relation regclass;
    l_lock_key            bigint;
    l_existing_definition text;
BEGIN
    -- PostgreSQL truncates an identifier to NAMEDATALEN - 1 bytes with only a
    -- NOTICE. The catalog guards below compare conname with the untruncated
    -- argument, so an over-long name would be added under a truncated name,
    -- committed, and then fail to match on the validation/recovery path.
    IF pg_catalog.octet_length(i_constraint_name) > 63 THEN
        RAISE EXCEPTION
            'ddl_utils.ensure_foreign_key: the constraint name must be at most 63 bytes'
            USING ERRCODE = '22023';
    END IF;

    -- Read the lock settings once; they are reused across both steps.
    SELECT ls.ddl_lock_timeout,
           ls.sleep_time,
           ls.statement_duration
    INTO STRICT l_settings.ddl_lock_timeout,
        l_settings.sleep_time,
        l_settings.statement_duration
    FROM ddl_utils.get_lock_settings(
                 i_schema_name => i_schema_name,
                 i_table_name => i_table_name
         ) AS ls;

    l_relation := pg_catalog.format('%I.%I', i_schema_name, i_table_name)::regclass;

    -- regclass only proves the relation exists; a view, sequence or index would
    -- pass the catalog checks below and fail later inside ALTER TABLE.
    IF (SELECT c.relkind
        FROM pg_catalog.pg_class AS c
        WHERE c.oid = l_relation) NOT IN ('r', 'p') THEN
        RAISE EXCEPTION 'ddl_utils.ensure_foreign_key: %.% is not a table',
            i_schema_name, i_table_name
            USING ERRCODE = '42809';
    END IF;

    -- The definitions are compared as a column mapping, which only makes sense
    -- for equal-length lists; reject mismatched lengths up front instead of
    -- silently dropping the surplus when they are zipped.
    PERFORM ddl_utils_lib.assert_equal_cardinality(
            i_a => i_column_names,
            i_b => i_referenced_column_names,
            i_context => 'ddl_utils.ensure_foreign_key: column lists');

    -- PostgreSQL requires a foreign key's column lists to name distinct
    -- columns; the join-based existence checks below would not notice a repeat,
    -- so reject duplicates before comparing or adding.
    IF pg_catalog.cardinality(i_column_names) <> (SELECT pg_catalog.count(DISTINCT name)
                                                  FROM pg_catalog.unnest(i_column_names) AS t(name))
        OR pg_catalog.cardinality(i_referenced_column_names) <>
           (SELECT pg_catalog.count(DISTINCT name)
            FROM pg_catalog.unnest(i_referenced_column_names) AS t(name)) THEN
        RAISE EXCEPTION 'ddl_utils.ensure_foreign_key: column lists must not contain duplicates'
            USING ERRCODE = '22023';
    END IF;

    -- A transaction-scoped advisory lock on the pair of relations serializes
    -- concurrent runs, including reciprocal ensure_foreign_key(A -> B) and
    -- (B -> A): ADD FOREIGN KEY takes SHARE ROW EXCLUSIVE on both relations, so
    -- a source-only key lets reciprocal calls acquire the table locks in
    -- opposite order and deadlock. Sort the two qualified names so both
    -- directions use the same slot. The lock is released by each step's COMMIT
    -- and on error, and step 2 re-takes it after step 1's COMMIT.
    l_lock_key := pg_catalog.hashtextextended(
            pg_catalog.format('ddl_utils.ensure_foreign_key|%s|%s',
                              least(
                                      pg_catalog.format('%I.%I', i_schema_name, i_table_name),
                                      pg_catalog.format('%I.%I', i_referenced_schema_name,
                                                        i_referenced_table_name)),
                              greatest(
                                      pg_catalog.format('%I.%I', i_schema_name, i_table_name),
                                      pg_catalog.format('%I.%I', i_referenced_schema_name,
                                                        i_referenced_table_name))),
            0);
    PERFORM pg_catalog.pg_advisory_xact_lock(l_lock_key);

    l_referenced_relation := pg_catalog.to_regclass(
            pg_catalog.format('%I.%I', i_referenced_schema_name, i_referenced_table_name));
    IF l_referenced_relation IS NULL THEN
        RAISE EXCEPTION 'ddl_utils.ensure_foreign_key: referenced table %.% does not exist',
            i_referenced_schema_name, i_referenced_table_name
            USING ERRCODE = '42P01';
    END IF;

    IF (SELECT c.relkind
        FROM pg_catalog.pg_class AS c
        WHERE c.oid = l_referenced_relation) NOT IN ('r', 'p') THEN
        RAISE EXCEPTION 'ddl_utils.ensure_foreign_key: referenced relation %.% is not a table',
            i_referenced_schema_name, i_referenced_table_name
            USING ERRCODE = '42809';
    END IF;

    -- A named column that does not resolve must fail loudly. Otherwise the
    -- pg_attribute join in the definition check silently drops it and reports a
    -- misleading "already exists with a different definition".
    IF (SELECT pg_catalog.count(*)
        FROM pg_catalog.unnest(i_column_names) AS t(name)
                 JOIN pg_catalog.pg_attribute AS a
                      ON a.attrelid = l_relation
                          AND a.attname = t.name
                          AND a.attnum > 0
                          AND NOT a.attisdropped)
        <> pg_catalog.cardinality(i_column_names) THEN
        RAISE EXCEPTION 'ddl_utils.ensure_foreign_key: a column of %.% does not exist',
            i_schema_name, i_table_name
            USING ERRCODE = '42703';
    END IF;

    IF (SELECT pg_catalog.count(*)
        FROM pg_catalog.unnest(i_referenced_column_names) AS t(name)
                 JOIN pg_catalog.pg_attribute AS a
                      ON a.attrelid = l_referenced_relation
                          AND a.attname = t.name
                          AND a.attnum > 0
                          AND NOT a.attisdropped)
        <> pg_catalog.cardinality(i_referenced_column_names) THEN
        RAISE EXCEPTION 'ddl_utils.ensure_foreign_key: a column of %.% does not exist',
            i_referenced_schema_name, i_referenced_table_name
            USING ERRCODE = '42703';
    END IF;

    -- A foreign key can be compared exactly, so a same-named key that references
    -- a different table, maps different columns, or has different referential
    -- actions/deferrability is a mismatch rather than the target; fail loudly so
    -- the wrong constraint is never silently accepted.
    --
    -- conkey/confkey are not guaranteed to be stored in the caller's declaration
    -- order (PostgreSQL may align them with the referenced unique index), so
    -- compare the source/referenced column *mapping* as a multiset of pairs
    -- rather than as ordered arrays. add_foreign_key creates the key with the
    -- PostgreSQL defaults (NO ACTION, MATCH SIMPLE, not deferrable).
    IF EXISTS (SELECT 1
               FROM pg_catalog.pg_constraint
               WHERE conname = i_constraint_name
                 AND conrelid = l_relation
                 AND contype = 'f')
        AND NOT EXISTS (SELECT 1
                        FROM pg_catalog.pg_constraint AS c
                        WHERE c.conname = i_constraint_name
                          AND c.conrelid = l_relation
                          AND c.contype = 'f'
                          AND c.confrelid = l_referenced_relation
                          AND c.confupdtype = 'a'
                          AND c.confdeltype = 'a'
                          AND c.confmatchtype = 's'
                          AND NOT c.condeferrable
                          AND NOT c.condeferred
                          AND NOT EXISTS (WITH stored AS (SELECT sa.attname AS source_name,
                                                                 ra.attname AS referenced_name
                                                          FROM pg_catalog.generate_subscripts(c.conkey, 1) AS s(i)
                                                                   JOIN pg_catalog.pg_attribute AS sa
                                                                        ON sa.attrelid = c.conrelid
                                                                            AND
                                                                           sa.attnum = c.conkey[s.i]
                                                                   JOIN pg_catalog.pg_attribute AS ra
                                                                        ON ra.attrelid = c.confrelid
                                                                            AND ra.attnum =
                                                                                c.confkey[s.i]),
                                               requested AS (SELECT s.name AS source_name,
                                                                    r.name AS referenced_name
                                                             FROM pg_catalog.unnest(
                                                                          i_column_names::text[]
                                                                  ) WITH ORDINALITY AS s(name, ord)
                                                                      JOIN pg_catalog.unnest(
                                                                     i_referenced_column_names::text[]
                                                                           ) WITH ORDINALITY AS r(name, ord)
                                                                           ON s.ord = r.ord)
                                          SELECT 1
                                          WHERE EXISTS (SELECT source_name, referenced_name
                                                        FROM requested
                                                        EXCEPT ALL
                                                        SELECT source_name, referenced_name
                                                        FROM stored)
                                             OR EXISTS (SELECT source_name, referenced_name
                                                        FROM stored
                                                        EXCEPT ALL
                                                        SELECT source_name, referenced_name
                                                        FROM requested))) THEN
        SELECT pg_catalog.pg_get_constraintdef(c.oid)
        INTO l_existing_definition
        FROM pg_catalog.pg_constraint AS c
        WHERE c.conname = i_constraint_name
          AND c.conrelid = l_relation
          AND c.contype = 'f';

        RAISE EXCEPTION
            'ddl_utils.ensure_foreign_key: constraint % already exists on %.% with a different definition (existing: %)',
            i_constraint_name, i_schema_name, i_table_name, l_existing_definition
            USING ERRCODE = '42710';
    END IF;

    -- The advisory lock taken above is still held here. A same-named constraint
    -- of another kind (PRIMARY KEY, UNIQUE, CHECK, EXCLUDE) would make
    -- add_foreign_key fail with a generic duplicate-object error; name the
    -- conflict instead.
    IF EXISTS (SELECT 1
               FROM pg_catalog.pg_constraint
               WHERE conname = i_constraint_name
                 AND conrelid = l_relation
                 AND contype <> 'f') THEN
        RAISE EXCEPTION
            'ddl_utils.ensure_foreign_key: constraint % already exists on %.% as a different kind of constraint',
            i_constraint_name, i_schema_name, i_table_name
            USING ERRCODE = '42710';
    END IF;

    -- Step 1: add the foreign key as NOT VALID (no scan; SHARE ROW EXCLUSIVE on
    -- both tables, released at the commit). Skipped when it already exists, so a
    -- re-run recovers after a partial failure.
    IF NOT EXISTS (SELECT 1
                   FROM pg_catalog.pg_constraint
                   WHERE conname = i_constraint_name
                     AND conrelid = l_relation
                     AND contype = 'f') THEN
        PERFORM ddl_utils_lib.add_foreign_key(
                i_schema_name => i_schema_name,
                i_table_name => i_table_name,
                i_constraint_name => i_constraint_name,
                i_column_names => i_column_names,
                i_referenced_schema_name => i_referenced_schema_name,
                i_referenced_table_name => i_referenced_table_name,
                i_referenced_column_names => i_referenced_column_names,
                i_ddl_lock_timeout => l_settings.ddl_lock_timeout,
                i_sleep_time => l_settings.sleep_time,
                i_statement_duration => l_settings.statement_duration
                );
        COMMIT;
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(l_lock_key);
    -- Step 2: validate the constraint, scanning under SHARE UPDATE EXCLUSIVE.
    IF EXISTS (SELECT 1
               FROM pg_catalog.pg_constraint
               WHERE conname = i_constraint_name
                 AND conrelid = l_relation
                 AND contype = 'f'
                 AND NOT convalidated) THEN
        PERFORM ddl_utils_lib.validate_constraint(
                i_schema_name => i_schema_name,
                i_table_name => i_table_name,
                i_constraint_name => i_constraint_name,
                i_ddl_lock_timeout => l_settings.ddl_lock_timeout,
                i_sleep_time => l_settings.sleep_time,
                i_statement_duration => l_settings.statement_duration
                );
        COMMIT;
    END IF;
END;
$$;

COMMENT ON PROCEDURE ddl_utils.ensure_foreign_key IS
    'Adds a foreign key as NOT VALID and then validates it, committing between steps. '
        'Call it outside a transaction block (autocommit).';
