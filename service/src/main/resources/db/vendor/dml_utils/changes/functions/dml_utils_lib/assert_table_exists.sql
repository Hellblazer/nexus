CREATE OR REPLACE FUNCTION dml_utils_lib.assert_table_exists(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
BEGIN
    IF NOT EXISTS (SELECT 1
                   FROM pg_catalog.pg_class AS c
                            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                   WHERE n.nspname = i_schema_name
                     AND c.relname = i_table_name
                     AND c.relkind IN ('r', 'p'))
    THEN
        RAISE EXCEPTION 'table %.% does not exist', i_schema_name, i_table_name
            USING ERRCODE = '42P01';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_table_exists IS
    'Raises undefined_table (42P01) when the table does not exist.';
