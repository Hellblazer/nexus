CREATE OR REPLACE FUNCTION ddl_utils.rename_table(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_new_table_name ddl_utils.non_null_text
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

    PERFORM ddl_utils_lib.rename_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_new_table_name => i_new_table_name,
            i_ddl_lock_timeout => l_settings.ddl_lock_timeout,
            i_sleep_time => l_settings.sleep_time,
            i_statement_duration => l_settings.statement_duration
            );

    -- Table-specific lock settings are keyed by the literal table name, so move
    -- any override to the new name; otherwise the renamed table silently falls
    -- back to the schema/database defaults and the old override is orphaned.
    -- The move is one SECURITY DEFINER statement (the caller has only SELECT on
    -- the settings table): no read-then-delete window, and an existing row for
    -- the destination is replaced with a NOTICE.
    PERFORM ddl_utils.move_table_lock_settings(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_new_table_name => i_new_table_name
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils.rename_table IS
    'Lock-aware wrapper: renames a table and moves its table lock settings to the '
        'new name.';
