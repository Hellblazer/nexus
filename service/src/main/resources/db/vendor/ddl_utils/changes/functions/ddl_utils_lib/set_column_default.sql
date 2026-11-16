CREATE OR REPLACE FUNCTION ddl_utils_lib.set_column_default(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_column_name ddl_utils.non_null_text,
    i_default_value ddl_utils.non_null_text,
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
    -- The default is an arbitrary expression (now(), coalesce(a, b), ...), so
    -- reject only a top-level comma that could append another ALTER TABLE
    -- action. The non_null_text domain already rejects a null or blank value.
    PERFORM ddl_utils_lib.assert_no_top_level_comma(
            i_value => i_default_value,
            i_context => 'ddl_utils_lib.set_column_default: the default expression');

    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => pg_catalog.format(
                    'ALTER COLUMN %I SET DEFAULT %s',
                    i_column_name,
                    i_default_value
                                      ),
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.set_column_default IS
    'Sets a column default from a raw SQL expression, taking the lock settings '
        'explicitly; literal defaults must include their quotes.';
