CREATE OR REPLACE FUNCTION ddl_utils_lib.drop_columns(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_column_names ddl_utils.non_empty_non_null_text_array,
    i_ddl_lock_timeout ddl_utils.non_negative_integer,
    i_sleep_time ddl_utils.non_negative_integer,
    i_statement_duration ddl_utils.non_negative_integer
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
DECLARE
    l_fragment   text;
    l_duplicates text;
    l_count      integer := pg_catalog.cardinality(i_column_names);
BEGIN
    -- Duplicates would emit the same DROP COLUMN twice and fail with a raw
    -- 42703 from PostgreSQL; reject them with this function's own error.
    -- Compare as name, not text: PostgreSQL truncates an identifier to
    -- NAMEDATALEN - 1 bytes and compares the name type byte-wise (it has no
    -- collation support), so two entries that collapse to the same identifier
    -- are duplicates. Case differences remain distinct, which is correct because
    -- quote_identifiers preserves case with %I.
    IF l_count <> (SELECT pg_catalog.count(DISTINCT t.name::pg_catalog.name)
                   FROM pg_catalog.unnest(i_column_names) AS t(name)) THEN
        SELECT pg_catalog.string_agg(d.name, ', ' ORDER BY d.name)
        INTO l_duplicates
        FROM (SELECT pg_catalog.min(t.name) AS name
              FROM pg_catalog.unnest(i_column_names) AS t(name)
              GROUP BY t.name::pg_catalog.name
              HAVING pg_catalog.count(*) > 1) AS d;

        RAISE EXCEPTION
            'ddl_utils_lib.drop_columns: duplicate column names are not allowed: %',
            l_duplicates
            USING ERRCODE = '22023';
    END IF;

    -- A blank name cannot produce a valid identifier; the helper also enforces
    -- the 1-based precondition.
    PERFORM ddl_utils_lib.assert_non_blank_elements(
            i_values => i_column_names,
            i_context => 'ddl_utils_lib.drop_columns',
            i_label => 'column name');

    -- The column names are identifiers, quoted with %I; the fragment contains
    -- no caller-supplied SQL.
    l_fragment := ddl_utils_lib.quote_identifiers(
            i_values => i_column_names,
            i_prefix => 'DROP COLUMN ');

    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => l_fragment,
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.drop_columns IS
    'Drops several columns in one ALTER TABLE, taking the lock settings '
        'explicitly. Each name is quoted with %I and must be at most 63 bytes; '
        'blank names and duplicates are rejected with 22023.';
