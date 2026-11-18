CREATE OR REPLACE FUNCTION ddl_utils.get_schema_lock_settings(
    i_schema_name ddl_utils.non_null_text
)
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
    RETURN QUERY
        SELECT sls.ddl_lock_timeout, sls.sleep_time, sls.statement_duration
        FROM ddl_utils.schema_lock_settings AS sls
        WHERE sls.schema_name = i_schema_name;
END;
$$;

COMMENT ON FUNCTION ddl_utils.get_schema_lock_settings IS
    'Returns the lock settings for a schema, or no row when there are none.';
