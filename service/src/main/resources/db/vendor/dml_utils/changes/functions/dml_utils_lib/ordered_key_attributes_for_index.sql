CREATE OR REPLACE FUNCTION dml_utils_lib.ordered_key_attributes_for_index(
    i_index_oid oid,
    i_schema_name text,
    i_table_name text,
    i_index_name text
)
    RETURNS TABLE
            (
                ordinality  integer,
                column_name name,
                column_oid  oid,
                column_type pg_catalog.regtype,
                key_kind    text
            )
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
BEGIN
    -- Validate the index-level contract first, so an ON ONLY or otherwise
    -- unusable parent reports the index error rather than a column error.
    PERFORM dml_utils_lib.assert_ordered_key_index_usable(
            i_index_oid => i_index_oid,
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_index_name => i_index_name);

    RETURN QUERY
        SELECT a.ordinality,
               a.column_name,
               a.column_oid,
               a.column_type,
               a.key_kind
        FROM dml_utils_lib.ordered_key_column_attributes(
                     i_index_oid => i_index_oid,
                     i_schema_name => i_schema_name,
                     i_table_name => i_table_name,
                     i_index_name => i_index_name) AS a;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.ordered_key_attributes_for_index(
    oid,
    text,
    text,
    text
    ) IS
    'Validates one ordered-key index by OID (assert_ordered_key_index_usable) and '
        'returns its key columns in index order (ordered_key_column_attributes). '
        'Used for a plain table index and for each leaf''s child index of a '
        'partition-wise parent. The caller supplies the names only for error '
        'messages.';
