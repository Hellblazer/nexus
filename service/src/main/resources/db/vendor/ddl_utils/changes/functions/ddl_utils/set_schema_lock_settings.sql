CREATE OR REPLACE FUNCTION ddl_utils.set_schema_lock_settings(
    i_schema_name ddl_utils.non_null_text,
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
    -- The schema name is stored as given and not resolved against
    -- pg_namespace, so settings can be provisioned before the schema exists.
    -- A typo simply never matches.
    --
    -- A setting of 0 has a distinct meaning: ddl_lock_timeout = 0 waits with no
    -- timeout, sleep_time = 0 busy-waits between retries, and
    -- statement_duration = 0 gives up after the first failed attempt.
    --
    -- Concurrent upserts for the same schema can block on the primary-key
    -- conflict and can surface a serialization failure at REPEATABLE READ or
    -- SERIALIZABLE; callers that need to survive that must retry.
    INSERT INTO ddl_utils.schema_lock_settings (schema_name, ddl_lock_timeout, sleep_time,
                                                statement_duration)
    VALUES (i_schema_name, i_ddl_lock_timeout, i_sleep_time, i_statement_duration)
    ON CONFLICT (schema_name) DO UPDATE
        SET ddl_lock_timeout   = EXCLUDED.ddl_lock_timeout,
            sleep_time         = EXCLUDED.sleep_time,
            statement_duration = EXCLUDED.statement_duration;
END;
$$;

COMMENT ON FUNCTION ddl_utils.set_schema_lock_settings IS
    'Upserts the lock settings for a schema. The name is stored verbatim, so '
        'settings may be provisioned before the schema exists. A zero timing is '
        'significant: no lock timeout, a busy-wait retry, or a zero retry budget.';
