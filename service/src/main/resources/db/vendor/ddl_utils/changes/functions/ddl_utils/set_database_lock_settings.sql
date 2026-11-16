CREATE OR REPLACE FUNCTION ddl_utils.set_database_lock_settings(
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
    -- Last writer wins; a concurrent read-modify-write can lose the other
    -- fields. That is acceptable for a plain setter.
    UPDATE ddl_utils.database_lock_settings
    SET ddl_lock_timeout   = i_ddl_lock_timeout,
        sleep_time         = i_sleep_time,
        statement_duration = i_statement_duration
    WHERE id = 1;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'ddl_utils.set_database_lock_settings: lock settings row (id = 1) does not exist'
            USING ERRCODE = 'P0002';
    END IF;
END;
$$;

COMMENT ON FUNCTION ddl_utils.set_database_lock_settings IS
    'Overwrites the singleton database lock settings; raises P0002 when the row is missing.';
