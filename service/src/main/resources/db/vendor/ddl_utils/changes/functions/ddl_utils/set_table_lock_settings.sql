CREATE OR REPLACE FUNCTION ddl_utils.set_table_lock_settings(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_ddl_lock_timeout ddl_utils.non_negative_integer,
    i_sleep_time ddl_utils.non_negative_integer,
    i_statement_duration ddl_utils.non_negative_integer
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS
$$
BEGIN
    -- The schema/table key is stored as given and not resolved against
    -- pg_class, so settings can be provisioned before the table exists. A typo
    -- simply never matches.
    --
    -- Concurrent upserts on the same key can still deadlock or fail with a
    -- serialization error; callers that need to survive that must retry.
    INSERT INTO ddl_utils.table_lock_settings (schema_name, table_name, ddl_lock_timeout,
                                               sleep_time,
                                               statement_duration)
    VALUES (i_schema_name, i_table_name, i_ddl_lock_timeout, i_sleep_time, i_statement_duration)
    ON CONFLICT (schema_name, table_name) DO UPDATE
        SET ddl_lock_timeout   = EXCLUDED.ddl_lock_timeout,
            sleep_time         = EXCLUDED.sleep_time,
            statement_duration = EXCLUDED.statement_duration;
END;
$$;

COMMENT ON FUNCTION ddl_utils.set_table_lock_settings IS
    'Upserts the lock settings for a table.';
