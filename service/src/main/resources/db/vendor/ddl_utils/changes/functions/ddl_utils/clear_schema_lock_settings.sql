CREATE OR REPLACE FUNCTION ddl_utils.clear_schema_lock_settings(
    i_schema_name ddl_utils.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS
$$
BEGIN
    -- SECURITY DEFINER: the caller may clear any schema's settings. That trust
    -- is intentional; ddl_utils_caller is the application role.
    DELETE
    FROM ddl_utils.schema_lock_settings
    WHERE schema_name = i_schema_name;

    -- The name is matched verbatim; report a miss so a typo is observable.
    IF NOT FOUND THEN
        RAISE NOTICE 'ddl_utils.clear_schema_lock_settings: no lock settings found for schema %',
            i_schema_name;
    END IF;
END;
$$;

COMMENT ON FUNCTION ddl_utils.clear_schema_lock_settings IS
    'Deletes the lock settings for a schema; a no-op when there are none.';
