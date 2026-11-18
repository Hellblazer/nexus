CREATE OR REPLACE FUNCTION ddl_utils.move_table_lock_settings(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_new_table_name ddl_utils.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS
$$
DECLARE
    l_replaces_destination boolean;
BEGIN
    l_replaces_destination := EXISTS (SELECT 1
                                      FROM ddl_utils.table_lock_settings
                                      WHERE schema_name = i_schema_name
                                        AND table_name = i_new_table_name);

    IF l_replaces_destination THEN
        RAISE NOTICE
            'ddl_utils.move_table_lock_settings: replacing the existing lock settings for %.%',
            i_schema_name, i_new_table_name;
    END IF;

    -- One statement: delete the source row and upsert its values under the new
    -- name. There is no read-then-delete window in which a concurrent
    -- set_table_lock_settings on the old name could be silently lost. If the
    -- destination already has a row it is replaced by the moved values (the
    -- NOTICE above tells the caller).
    WITH moved AS (
        DELETE FROM ddl_utils.table_lock_settings
            WHERE schema_name = i_schema_name
                AND table_name = i_table_name
            RETURNING ddl_lock_timeout, sleep_time, statement_duration)
    INSERT
    INTO ddl_utils.table_lock_settings (schema_name, table_name, ddl_lock_timeout,
                                        sleep_time, statement_duration)
    SELECT i_schema_name,
           i_new_table_name,
           m.ddl_lock_timeout,
           m.sleep_time,
           m.statement_duration
    FROM moved AS m
    ON CONFLICT (schema_name, table_name) DO UPDATE
        SET ddl_lock_timeout   = EXCLUDED.ddl_lock_timeout,
            sleep_time         = EXCLUDED.sleep_time,
            statement_duration = EXCLUDED.statement_duration;
END;
$$;

COMMENT ON FUNCTION ddl_utils.move_table_lock_settings IS
    'Moves a table''s lock settings to a new table name in one statement, '
        'replacing any existing row for the destination.';
