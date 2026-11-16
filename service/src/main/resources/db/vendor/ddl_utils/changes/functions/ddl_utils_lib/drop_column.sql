CREATE OR REPLACE FUNCTION ddl_utils_lib.drop_column(
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
    -- Single-column convenience over ddl_utils_lib.drop_columns.
    PERFORM ddl_utils_lib.drop_columns(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_column_names => ARRAY [i_column_name],
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.drop_column IS
    'Drops one column, taking the lock settings explicitly.';
