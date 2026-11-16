CREATE OR REPLACE FUNCTION ddl_utils.drop_column(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_column_name ddl_utils.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
BEGIN
    -- Single-column convenience over ddl_utils.drop_columns.
    PERFORM ddl_utils.drop_columns(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_column_names => ARRAY [i_column_name]
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils.drop_column IS
    'Lock-aware wrapper: drops one column.';
