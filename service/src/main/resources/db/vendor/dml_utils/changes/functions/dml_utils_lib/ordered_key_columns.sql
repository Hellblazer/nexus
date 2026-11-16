CREATE OR REPLACE FUNCTION dml_utils_lib.ordered_key_columns(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_index_name dml_utils_data.non_null_text
)
    RETURNS name[]
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_key_columns name[];
BEGIN
    -- ordered_key_attributes validates the index and returns one row per key
    -- column in index order, so the aggregate is never NULL or short.
    SELECT pg_catalog.array_agg(a.column_name ORDER BY a.ordinality)
    INTO l_key_columns
    FROM dml_utils_lib.ordered_key_attributes(
                 i_schema_name => i_schema_name,
                 i_table_name => i_table_name,
                 i_index_name => i_index_name) AS a;

    RETURN l_key_columns;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.ordered_key_columns IS
    'Returns the ordered-key columns of a unique btree index in index order, '
        'delegating validation to ordered_key_attributes (so it raises the same '
        'SQLSTATEs for an unusable index).';
