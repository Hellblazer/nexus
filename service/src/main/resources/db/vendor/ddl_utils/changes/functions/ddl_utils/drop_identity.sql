CREATE OR REPLACE FUNCTION ddl_utils.drop_identity(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_column_name ddl_utils.non_null_text,
    i_if_exists ddl_utils.non_null_boolean
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
DECLARE
    l_settings ddl_utils.lock_settings;
BEGIN
    -- Lock-aware wrapper: resolves the settings for the table and delegates to
    -- the generic helper.
    SELECT ls.ddl_lock_timeout,
           ls.sleep_time,
           ls.statement_duration
    INTO STRICT l_settings.ddl_lock_timeout,
        l_settings.sleep_time,
        l_settings.statement_duration
    FROM ddl_utils.get_lock_settings(
                 i_schema_name => i_schema_name,
                 i_table_name => i_table_name
         ) AS ls;

    PERFORM ddl_utils_lib.drop_identity(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_column_name => i_column_name,
            i_if_exists => i_if_exists,
            i_ddl_lock_timeout => l_settings.ddl_lock_timeout,
            i_sleep_time => l_settings.sleep_time,
            i_statement_duration => l_settings.statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils.drop_identity IS
    'Lock-aware wrapper: drops the identity from a column.';
