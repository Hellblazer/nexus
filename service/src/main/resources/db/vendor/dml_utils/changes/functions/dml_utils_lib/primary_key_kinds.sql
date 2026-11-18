CREATE OR REPLACE FUNCTION dml_utils_lib.primary_key_kinds(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text
)
    RETURNS text[]
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_key_kinds text[];
    l_key       record;
BEGIN
    -- primary_key_attributes returns each column's collapsed kind in key order;
    -- a NULL kind means an unsupported type.
    FOR l_key IN
        SELECT a.column_name, a.column_type, a.key_kind
        FROM dml_utils_lib.primary_key_attributes(
                     i_schema_name => i_schema_name,
                     i_table_name => i_table_name) AS a
        ORDER BY a.ordinality
        LOOP
            IF l_key.key_kind IS NULL THEN
                RAISE EXCEPTION 'primary key column %.%.% has unsupported type %',
                    i_schema_name, i_table_name, l_key.column_name, l_key.column_type
                    USING ERRCODE = '22023';
            END IF;
            l_key_kinds := l_key_kinds || l_key.key_kind;
        END LOOP;

    RETURN l_key_kinds;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.primary_key_kinds IS
    'Returns the boundary key kind (bigint, text, uuid, timestamptz or bytea) of each '
        'primary-key column in key order, raising invalid_parameter_value (22023) '
        'for an unsupported type.';
