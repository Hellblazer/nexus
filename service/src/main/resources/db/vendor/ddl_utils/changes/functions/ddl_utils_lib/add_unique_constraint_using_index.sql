CREATE OR REPLACE FUNCTION ddl_utils_lib.add_unique_constraint_using_index(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_constraint_name ddl_utils.non_null_text,
    i_index_name ddl_utils.non_null_text,
    i_ddl_lock_timeout ddl_utils.non_negative_integer,
    i_sleep_time ddl_utils.non_negative_integer,
    i_statement_duration ddl_utils.non_negative_integer
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
BEGIN
    -- The index name is emitted as a single %I identifier and resolved in the
    -- table's schema; reject a qualified or pre-quoted name before building it.
    IF pg_catalog.cardinality(pg_catalog.parse_ident(i_index_name)) <> 1
        OR pg_catalog.strpos(i_index_name, '"') > 0 THEN
        RAISE EXCEPTION
            'ddl_utils_lib.add_unique_constraint_using_index: i_index_name must be an unquoted, unqualified identifier'
            USING ERRCODE = '22023';
    END IF;

    IF pg_catalog.octet_length(i_constraint_name) > 63
        OR pg_catalog.octet_length(i_index_name) > 63 THEN
        RAISE EXCEPTION
            'ddl_utils_lib.add_unique_constraint_using_index: a constraint or index name must be at most 63 bytes'
            USING ERRCODE = '22023';
    END IF;

    IF NOT EXISTS (SELECT 1
                   FROM pg_catalog.pg_class AS i
                            JOIN pg_catalog.pg_namespace AS n ON n.oid = i.relnamespace
                   WHERE n.nspname = i_schema_name
                     AND i.relname = i_index_name
                     AND i.relkind = 'i') THEN
        RAISE EXCEPTION
            'ddl_utils_lib.add_unique_constraint_using_index: index %.% does not exist',
            i_schema_name, i_index_name
            USING ERRCODE = '42704';
    END IF;

    -- Attaching is metadata-only only when the index is valid, unique,
    -- non-partial and non-expression.
    IF NOT EXISTS (SELECT 1
                   FROM pg_catalog.pg_class AS i
                            JOIN pg_catalog.pg_namespace AS n ON n.oid = i.relnamespace
                            JOIN pg_catalog.pg_index AS ix ON ix.indexrelid = i.oid
                   WHERE n.nspname = i_schema_name
                     AND i.relname = i_index_name
                     AND ix.indrelid = pg_catalog.format('%I.%I', i_schema_name, i_table_name)::regclass
                     AND ix.indisunique
                     AND ix.indisvalid
                     AND ix.indpred IS NULL
                     AND ix.indexprs IS NULL) THEN
        RAISE EXCEPTION
            'ddl_utils_lib.add_unique_constraint_using_index: index %.% is not a valid, non-partial, non-expression unique index on %.%',
            i_schema_name, i_index_name, i_schema_name, i_table_name
            USING ERRCODE = '42809';
    END IF;

    -- Attaches an already-built unique index as a unique constraint. Build the
    -- index with CREATE UNIQUE INDEX CONCURRENTLY first so the table is never
    -- held under ACCESS EXCLUSIVE for the build; the attach itself still takes
    -- a brief ACCESS EXCLUSIVE lock (hence the alter_table retry).
    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => pg_catalog.format(
                    'ADD CONSTRAINT %I UNIQUE USING INDEX %I',
                    i_constraint_name,
                    i_index_name
                                      ),
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.add_unique_constraint_using_index IS
    'Attaches a pre-built unique index as a unique constraint.';
