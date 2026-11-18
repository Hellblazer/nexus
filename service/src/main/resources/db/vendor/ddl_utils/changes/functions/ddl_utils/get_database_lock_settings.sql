CREATE OR REPLACE FUNCTION ddl_utils.get_database_lock_settings()
    RETURNS TABLE
            (
                ddl_lock_timeout   integer,
                sleep_time         integer,
                statement_duration integer
            )
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
BEGIN
    -- The settings table is a singleton, enforced by the id = 1 check
    -- constraint on ddl_utils.database_lock_settings.
    RETURN QUERY
        SELECT ls.ddl_lock_timeout, ls.sleep_time, ls.statement_duration
        FROM ddl_utils.database_lock_settings AS ls
        WHERE ls.id = 1;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'ddl_utils.get_database_lock_settings: lock settings row (id = 1) does not exist'
            USING ERRCODE = 'P0002';
    END IF;
END;
$$;

COMMENT ON FUNCTION ddl_utils.get_database_lock_settings IS
    'Returns the singleton database lock settings; raises P0002 when the row is missing.';
