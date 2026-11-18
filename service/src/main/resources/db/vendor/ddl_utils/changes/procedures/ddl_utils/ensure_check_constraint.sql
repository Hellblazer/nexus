CREATE OR REPLACE PROCEDURE ddl_utils.ensure_check_constraint(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_constraint_name ddl_utils.non_null_text,
    i_check_expression ddl_utils.non_null_text
)
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
DECLARE
    l_settings ddl_utils.lock_settings;
    l_relation regclass;
    l_lock_key bigint;
BEGIN
    -- PostgreSQL truncates an identifier to NAMEDATALEN - 1 bytes with only a
    -- NOTICE. The catalog guards below compare conname with the untruncated
    -- argument, so an over-long name would be added under a truncated name,
    -- committed, and then fail to match on the validation/recovery path.
    IF pg_catalog.octet_length(i_constraint_name) > 63 THEN
        RAISE EXCEPTION
            'ddl_utils.ensure_check_constraint: the constraint name must be at most 63 bytes'
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
        RAISE EXCEPTION 'ddl_utils.ensure_check_constraint: %.% is not a table',
            i_schema_name, i_table_name
            USING ERRCODE = '42809';
    END IF;

    -- A same-named constraint of another kind would make add_check_constraint
    -- fail with a generic duplicate-object error; name the conflict instead.
    IF EXISTS (SELECT 1
               FROM pg_catalog.pg_constraint
               WHERE conname = i_constraint_name
                 AND conrelid = l_relation
                 AND contype <> 'c') THEN
        RAISE EXCEPTION
            'ddl_utils.ensure_check_constraint: constraint % already exists on %.% as a different kind of constraint',
            i_constraint_name, i_schema_name, i_table_name
            USING ERRCODE = '42710';
    END IF;

    -- A transaction-scoped advisory lock on this table serializes concurrent
    -- runs. The read-then-act guards are separated by COMMIT, so the lock is
    -- taken again before each step; it is released by that step's COMMIT and on
    -- error, so a failed run cannot leak it.
    l_lock_key := pg_catalog.hashtextextended(
            pg_catalog.format('ddl_utils.ensure_check_constraint|%I.%I', i_schema_name, i_table_name),
            0);

    -- The name is the identity: a same-named CHECK is treated as the target and
    -- its expression is not re-checked. Unlike a foreign key, the definition
    -- cannot be compared exactly -- pg_get_constraintdef returns a normalized,
    -- version-dependent form of the expression -- so a same-named constraint
    -- with a different expression is not detected. Use a distinct name per
    -- expression.
    --
    PERFORM pg_catalog.pg_advisory_xact_lock(l_lock_key);
    -- Step 1: add the constraint as NOT VALID (instant; brief ACCESS
    -- EXCLUSIVE). Skipped when it already exists, which is how a re-run
    -- recovers after the add committed but validation did not.
    IF NOT EXISTS (SELECT 1
                   FROM pg_catalog.pg_constraint
                   WHERE conname = i_constraint_name
                     AND conrelid = l_relation
                     AND contype = 'c') THEN
        PERFORM ddl_utils_lib.add_check_constraint(
                i_schema_name => i_schema_name,
                i_table_name => i_table_name,
                i_constraint_name => i_constraint_name,
                i_check_expression => i_check_expression,
                i_ddl_lock_timeout => l_settings.ddl_lock_timeout,
                i_sleep_time => l_settings.sleep_time,
                i_statement_duration => l_settings.statement_duration
                );
        -- The embedded COMMIT requires the procedure to be CALLed outside a
        -- transaction block (autocommit); see procedures/README.md.
        COMMIT;
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(l_lock_key);
    -- Step 2: validate the constraint, scanning under SHARE UPDATE EXCLUSIVE.
    IF EXISTS (SELECT 1
               FROM pg_catalog.pg_constraint
               WHERE conname = i_constraint_name
                 AND conrelid = l_relation
                 AND contype = 'c'
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

COMMENT ON PROCEDURE ddl_utils.ensure_check_constraint IS
    'Adds a CHECK constraint as NOT VALID and then validates it, committing '
        'between steps. Call it outside a transaction block (autocommit).';
