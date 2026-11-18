CREATE OR REPLACE FUNCTION dml_utils_lib.partitioned_ordered_key_attributes(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_index_name dml_utils_data.non_null_text
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
    VOLATILE
    SECURITY INVOKER
AS
$$
DECLARE
    l_index_oid oid;
BEGIN
    l_index_oid := dml_utils_lib.resolve_partitioned_ordered_key_index(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_index_name => i_index_name);

    PERFORM dml_utils_lib.assert_partitioned_ordered_key_child_indexes(
            i_index_oid => l_index_oid,
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_index_name => i_index_name);

    RETURN QUERY
        SELECT a.ordinality,
               a.column_name,
               a.column_oid,
               a.column_type,
               a.key_kind
        FROM dml_utils_lib.ordered_key_attributes_for_index(
                     i_index_oid => l_index_oid,
                     i_schema_name => i_schema_name,
                     i_table_name => i_table_name,
                     i_index_name => i_index_name) AS a;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.partitioned_ordered_key_attributes(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) IS
    'Validates a partition-wise ordered key: resolve_partitioned_ordered_key_index '
        'checks the parent and its index, '
        'assert_partitioned_ordered_key_child_indexes validates every storage '
        'leaf''s matching descendant child index, and the parent''s key columns and '
        'kinds are returned. Rejects a missing index (42704), a non-partitioned '
        'parent (0A000), a leaf without a matching child index (0A000), and an '
        'unusable parent or child index (0A000/55000). VOLATILE because '
        'leaf_partitions calls the volatile pg_partition_tree.';
