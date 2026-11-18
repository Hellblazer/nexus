CREATE OR REPLACE FUNCTION ddl_utils_lib.set_column_compression(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_column_name ddl_utils.non_null_text,
    i_compression ddl_utils.non_null_text,
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
    -- Compression is a small, fixed keyword set; validate it so a caller cannot
    -- splice arbitrary SQL. The keyword is emitted verbatim (not as an
    -- identifier) because these are unquoted PostgreSQL keywords. Affects
    -- future writes only; existing values are not rewritten.
    PERFORM ddl_utils_lib.assert_allowed_keyword(
            i_value => i_compression,
            i_allowed => ARRAY ['pglz', 'lz4', 'default'],
            i_context => 'ddl_utils_lib.set_column_compression: compression');

    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => pg_catalog.format(
                    'ALTER COLUMN %I SET COMPRESSION %s',
                    i_column_name,
                    pg_catalog.lower(i_compression)
                                      ),
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.set_column_compression IS
    'Sets a column compression method, taking the lock settings explicitly.';
