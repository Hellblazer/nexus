CREATE OR REPLACE FUNCTION dml_utils_lib.assert_schema_exists(
    i_schema_name dml_utils_data.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
BEGIN
    IF NOT EXISTS (SELECT 1
                   FROM pg_catalog.pg_namespace
                   WHERE nspname = i_schema_name)
    THEN
        RAISE EXCEPTION 'schema % does not exist', i_schema_name
            USING ERRCODE = '3F000';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_schema_exists IS
    'Raises invalid_schema_name (3F000) when the schema does not exist.';
