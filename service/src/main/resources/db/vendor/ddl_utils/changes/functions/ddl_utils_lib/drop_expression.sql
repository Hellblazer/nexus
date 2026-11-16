CREATE OR REPLACE FUNCTION ddl_utils_lib.drop_expression(
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
    -- Turns a generated column into a plain base column. Metadata-only, but the
    -- ALTER TABLE still takes ACCESS EXCLUSIVE, so it uses the lock/retry loop.
    -- A column that is not a stored generated column raises 55000, and a missing
    -- column 42703; alter_table only retries lock_not_available, so neither is
    -- retried.
    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => pg_catalog.format('ALTER COLUMN %I DROP EXPRESSION',
                                                        i_column_name),
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.drop_expression IS
    'Drops a generated column expression, taking the lock settings explicitly. '
        'Metadata-only, but ALTER TABLE still takes ACCESS EXCLUSIVE.';
