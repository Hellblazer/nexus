CREATE OR REPLACE FUNCTION dml_utils_lib.partitioned_ordered_key_columns(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_index_name dml_utils_data.non_null_text
)
    RETURNS name[]
    LANGUAGE sql
    VOLATILE
    SECURITY INVOKER
AS
$$
SELECT pg_catalog.array_agg(a.column_name ORDER BY a.ordinality)
FROM dml_utils_lib.partitioned_ordered_key_attributes(
             i_schema_name => i_schema_name,
             i_table_name => i_table_name,
             i_index_name => i_index_name) AS a;
$$;

COMMENT ON FUNCTION dml_utils_lib.partitioned_ordered_key_columns(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) IS
    'Validates a partition-wise ordered key and returns its columns in key order.';
