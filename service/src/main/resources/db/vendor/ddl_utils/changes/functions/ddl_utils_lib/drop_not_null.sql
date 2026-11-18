CREATE OR REPLACE FUNCTION ddl_utils_lib.drop_not_null(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_column_name ddl_utils.non_null_text,
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
    -- Dropping NOT NULL is metadata-only and performs no table scan; it does
    -- take an ACCESS EXCLUSIVE lock, which alter_table bounds with its retry
    -- loop.
    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => pg_catalog.format('ALTER COLUMN %I DROP NOT NULL',
                                                        i_column_name),
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.drop_not_null IS
    'Drops NOT NULL from a column, taking the lock settings explicitly.';
