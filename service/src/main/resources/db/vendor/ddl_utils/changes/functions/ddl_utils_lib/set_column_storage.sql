CREATE OR REPLACE FUNCTION ddl_utils_lib.set_column_storage(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_column_name ddl_utils.non_null_text,
    i_storage ddl_utils.non_null_text,
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
    -- Storage is a small, fixed keyword set; validate it (case-insensitively,
    -- matching set_column_compression) so a caller cannot splice arbitrary SQL.
    -- The keyword is emitted verbatim (not as an identifier) because these are
    -- unquoted PostgreSQL keywords.
    PERFORM ddl_utils_lib.assert_allowed_keyword(
            i_value => i_storage,
            i_allowed => ARRAY ['plain', 'external', 'extended', 'main'],
            i_context => 'ddl_utils_lib.set_column_storage: storage');

    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => pg_catalog.format(
                    'ALTER COLUMN %I SET STORAGE %s',
                    i_column_name,
                    pg_catalog.lower(i_storage)
                                      ),
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.set_column_storage IS
    'Sets a column storage mode, taking the lock settings explicitly.';
