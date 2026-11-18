CREATE OR REPLACE FUNCTION dml_utils_lib.explain_chunk_plans(
    i_sql_text dml_utils_data.non_null_text,
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_table_alias dml_utils_data.non_null_text,
    i_key_columns name[],
    i_key_kinds text[],
    i_chunk_size bigint,
    i_chunk_by dml_utils_data.chunking_strategy DEFAULT 'primary_key'
)
    RETURNS TABLE
            (
                o_plan_kind text,
                o_sql_text  text,
                o_plan      json
            )
    LANGUAGE plpgsql
    VOLATILE
    SECURITY INVOKER
AS
$$
BEGIN
    -- Validate the template once here, so both caller-facing explain functions
    -- (a raw template and the function wrapper) share the check.
    PERFORM dml_utils_lib.assert_chunking_template(i_sql_text => i_sql_text);

    IF i_chunk_by = 'blocks' THEN
        RETURN QUERY
            SELECT p.o_plan_kind,
                   p.o_sql_text,
                   p.o_plan
            FROM dml_utils_lib.explain_block_chunk_plans(
                         i_sql_text => i_sql_text,
                         i_schema_name => i_schema_name,
                         i_table_name => i_table_name,
                         i_table_alias => i_table_alias,
                         i_chunk_size => i_chunk_size) AS p;
    ELSE
        RETURN QUERY
            SELECT p.o_plan_kind,
                   p.o_sql_text,
                   p.o_plan
            FROM dml_utils_lib.explain_key_chunk_plans(
                         i_sql_text => i_sql_text,
                         i_schema_name => i_schema_name,
                         i_table_name => i_table_name,
                         i_table_alias => i_table_alias,
                         i_key_columns => i_key_columns,
                         i_key_kinds => i_key_kinds,
                         i_chunk_size => i_chunk_size) AS p;
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.explain_chunk_plans IS
    'Returns the EXPLAIN (FORMAT JSON) plans for the three statements a chunked '
        'run generates: the boundary-population insert (with a synthetic run id), '
        'a non-final chunk and the final chunk, for the given chunking strategy '
        '(primary_key or blocks). It reads no boundaries and writes nothing; the '
        'chunk ranges are synthetic. The strategy-specific plans live in '
        'explain_block_chunk_plans and explain_key_chunk_plans. Shared by '
        'dml_utils.explain_migration_chunks and '
        'dml_utils.explain_function_over_table.';
