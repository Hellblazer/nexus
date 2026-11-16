CREATE OR REPLACE FUNCTION ddl_utils.get_table_lock_settings(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text
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
        SELECT tls.ddl_lock_timeout, tls.sleep_time, tls.statement_duration
        FROM ddl_utils.table_lock_settings AS tls
        WHERE tls.schema_name = i_schema_name
          AND tls.table_name = i_table_name;
END;
$$;

COMMENT ON FUNCTION ddl_utils.get_table_lock_settings IS
    'Returns the lock settings for a table, or no row when there are none.';
