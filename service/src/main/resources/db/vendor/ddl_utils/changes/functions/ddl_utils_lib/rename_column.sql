CREATE OR REPLACE FUNCTION ddl_utils_lib.rename_column(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_column_name ddl_utils.non_null_text,
    i_new_column_name ddl_utils.non_null_text,
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
    -- PostgreSQL silently truncates an identifier longer than NAMEDATALEN - 1
    -- bytes (with only a NOTICE), so a rename to an over-long name would appear
    -- to succeed while later references to the full name fail.
    IF pg_catalog.octet_length(i_column_name) > 63
        OR pg_catalog.octet_length(i_new_column_name) > 63 THEN
        RAISE EXCEPTION 'ddl_utils_lib.rename_column: an identifier must be at most 63 bytes'
            USING ERRCODE = '22023';
    END IF;

    -- Both names are identifiers, so they are quoted with %I and the fragment
    -- contains no caller-supplied SQL.
    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => pg_catalog.format(
                    'RENAME COLUMN %I TO %I',
                    i_column_name,
                    i_new_column_name
                                      ),
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.rename_column IS
    'Renames a column, taking the lock settings explicitly. Both identifiers must '
        'be at most 63 bytes.';
