CREATE OR REPLACE FUNCTION ddl_utils.get_lock_settings(
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
    -- Each RETURN QUERY sets FOUND based on whether it returned rows, so the
    -- chain falls through table -> schema -> database. The table and schema
    -- lookups delegate to their accessors so the column projection is defined
    -- once; the database fallback is kept inline so the final error can still
    -- name the schema and table.
    RETURN QUERY
        SELECT tls.ddl_lock_timeout, tls.sleep_time, tls.statement_duration
        FROM ddl_utils.get_table_lock_settings(
                     i_schema_name => i_schema_name,
                     i_table_name => i_table_name
             ) AS tls;

    IF FOUND THEN
        RETURN;
    END IF;

    RETURN QUERY
        SELECT sls.ddl_lock_timeout, sls.sleep_time, sls.statement_duration
        FROM ddl_utils.get_schema_lock_settings(
                     i_schema_name => i_schema_name
             ) AS sls;

    IF FOUND THEN
        RETURN;
    END IF;

    RETURN QUERY
        SELECT dls.ddl_lock_timeout, dls.sleep_time, dls.statement_duration
        FROM ddl_utils.database_lock_settings AS dls
        WHERE dls.id = 1;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'ddl_utils.get_lock_settings: no lock settings found for schema % and table % and no database defaults (id = 1)',
            i_schema_name, i_table_name
            USING ERRCODE = 'P0002';
    END IF;
END;
$$;

COMMENT ON FUNCTION ddl_utils.get_lock_settings IS
    'Resolves the effective lock settings for a table: table, then schema, then database.';
