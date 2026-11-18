CREATE OR REPLACE FUNCTION ddl_utils_lib.drop_identity(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_column_name ddl_utils.non_null_text,
    i_if_exists ddl_utils.non_null_boolean,
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
    -- Dropping an identity is metadata-only. IF EXISTS tolerates a column that
    -- has no identity; without it a missing identity raises an error.
    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => pg_catalog.format(
                    'ALTER COLUMN %I DROP IDENTITY%s',
                    i_column_name,
                    CASE WHEN i_if_exists THEN ' IF EXISTS' ELSE '' END
                                      ),
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.drop_identity IS
    'Drops a column identity, taking the lock settings explicitly.';
