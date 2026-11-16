CREATE OR REPLACE FUNCTION dml_utils_lib.ordered_key_kinds(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_index_name dml_utils_data.non_null_text
)
    RETURNS text[]
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_key_kinds text[];
BEGIN
    -- ordered_key_attributes validates the index (including the supported-type
    -- check) and returns one row per key column in index order, so the
    -- aggregate is never NULL or short.
    SELECT pg_catalog.array_agg(a.key_kind ORDER BY a.ordinality)
    INTO l_key_kinds
    FROM dml_utils_lib.ordered_key_attributes(
                 i_schema_name => i_schema_name,
                 i_table_name => i_table_name,
                 i_index_name => i_index_name) AS a;

    RETURN l_key_kinds;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.ordered_key_kinds IS
    'Returns the collapsed boundary key kind (bigint, text, uuid, timestamptz or bytea) '
        'of each ordered-key column in index order, delegating validation to '
        'ordered_key_attributes and raising invalid_parameter_value (22023) for '
        'an unsupported type.';
