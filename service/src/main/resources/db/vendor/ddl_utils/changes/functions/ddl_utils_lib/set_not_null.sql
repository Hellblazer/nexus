CREATE OR REPLACE FUNCTION ddl_utils_lib.set_not_null(
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
    -- SET NOT NULL ordinarily scans the table to prove no row is NULL, under an
    -- ACCESS EXCLUSIVE lock. PostgreSQL skips that scan when a valid CHECK
    -- constraint already proves the column non-null. This helper makes the
    -- single ALTER TABLE call only; composing that CHECK and its validation is
    -- the job of a procedure (see ddl_utils.ensure_not_null), because a
    -- function cannot commit between steps.
    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => pg_catalog.format('ALTER COLUMN %I SET NOT NULL',
                                                        i_column_name),
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.set_not_null IS
    'Sets NOT NULL on a column, taking the lock settings explicitly. Unless a '
        'valid CHECK already proves the column non-null, PostgreSQL scans the '
        'table under ACCESS EXCLUSIVE; ddl_utils.ensure_not_null avoids that.';
