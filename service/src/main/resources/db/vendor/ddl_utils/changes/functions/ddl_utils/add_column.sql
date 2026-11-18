CREATE OR REPLACE FUNCTION ddl_utils.add_column(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_column_name ddl_utils.non_null_text,
    i_column_type ddl_utils.non_null_text,
    i_nullable ddl_utils.non_null_boolean,
    i_default_value text DEFAULT NULL
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
BEGIN
    -- ddl_utils.add_columns resolves the lock settings and delegates to
    -- ddl_utils_lib.add_columns, which validates the raw type/default SQL; see
    -- that function for the rules.
    PERFORM ddl_utils.add_columns(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_column_names => ARRAY [i_column_name],
            i_column_types => ARRAY [i_column_type],
            i_default_values => ARRAY [i_default_value],
            i_nullable => ARRAY [i_nullable]
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils.add_column IS
    'Lock-aware wrapper: adds one column. A NULL i_default_value means no '
        'DEFAULT clause; a non-null value is raw SQL (quoted literals must '
        'include their quotes) and must not come from untrusted input.';
