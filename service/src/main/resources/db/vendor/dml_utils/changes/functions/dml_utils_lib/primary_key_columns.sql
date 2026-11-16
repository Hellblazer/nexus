CREATE OR REPLACE FUNCTION dml_utils_lib.primary_key_columns(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text
)
    RETURNS name[]
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_key_column_count    smallint;
    l_primary_key_columns name[];
BEGIN
    PERFORM dml_utils_lib.assert_schema_exists(i_schema_name => i_schema_name);
    PERFORM dml_utils_lib.assert_table_exists(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name);

    SELECT i.indnkeyatts
    INTO l_key_column_count
    FROM pg_catalog.pg_index AS i
             JOIN pg_catalog.pg_class AS c ON c.oid = i.indrelid
             JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE i.indisprimary
      AND n.nspname = i_schema_name
      AND c.relname = i_table_name;

    IF l_key_column_count IS NULL THEN
        RAISE EXCEPTION 'table %.% has no primary key', i_schema_name, i_table_name
            USING ERRCODE = '22023';
    END IF;

    IF l_key_column_count > 3 THEN
        RAISE EXCEPTION 'table %.% has a % column primary key; at most 3 are supported',
            i_schema_name, i_table_name, l_key_column_count
            USING ERRCODE = '22023';
    END IF;

    -- The columns, in key order, come from the shared catalog reader so all
    -- callers agree on the key order.
    SELECT pg_catalog.array_agg(a.column_name ORDER BY a.ordinality)
    INTO l_primary_key_columns
    FROM dml_utils_lib.primary_key_attributes(
                 i_schema_name => i_schema_name,
                 i_table_name => i_table_name) AS a;

    -- A concurrent drop of the primary key between the count read above and this
    -- one would leave the aggregate NULL (or short); fail loudly instead of
    -- returning a NULL/partial list to callers that zip it with the kinds.
    IF pg_catalog.cardinality(l_primary_key_columns) IS DISTINCT FROM l_key_column_count::integer
    THEN
        RAISE EXCEPTION 'primary key of table %.% changed while reading its columns',
            i_schema_name, i_table_name
            USING ERRCODE = '22023';
    END IF;

    RETURN l_primary_key_columns;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.primary_key_columns IS
    'Returns the primary-key columns in key order, raising '
        'invalid_parameter_value (22023) when the table has no primary key or more '
        'than 3 key columns.';
