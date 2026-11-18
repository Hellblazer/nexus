CREATE OR REPLACE FUNCTION ddl_utils_lib.add_column(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_column_name ddl_utils.non_null_text,
    i_column_type ddl_utils.non_null_text,
    i_nullable ddl_utils.non_null_boolean,
    i_default_value text,
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
    -- A NULL i_default_value means "no DEFAULT clause"; a non-null value is raw
    -- SQL (so quoted literals must include their quotes). See add_columns.
    PERFORM ddl_utils_lib.add_columns(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_column_names => ARRAY [i_column_name],
            i_column_types => ARRAY [i_column_type],
            i_default_values => ARRAY [i_default_value],
            i_nullable => ARRAY [i_nullable],
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.add_column IS
    'Adds one column, with an optional default, taking the lock settings explicitly.';
