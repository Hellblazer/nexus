CREATE OR REPLACE FUNCTION dml_utils.explain_migration_chunk(
    i_sql_text dml_utils_data.non_null_text,
    i_driving_table_schema_name dml_utils_data.non_null_text,
    i_driving_table_name dml_utils_data.non_null_text,
    i_start_values text[],
    i_end_values text[],
    i_is_final boolean,
    i_driving_table_alias dml_utils_data.non_null_text DEFAULT 't',
    -- The strategy whose key the explicit values belong to. This single-chunk
    -- explain supports primary_key and ordered_key; use explain_migration_chunks
    -- for block plans.
    i_chunk_by dml_utils_data.chunking_strategy DEFAULT 'primary_key',
    -- The unique index whose order an ordered_key chunk uses. Required for
    -- ordered_key and rejected for the other strategies.
    i_boundary_key_index name DEFAULT NULL
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
DECLARE
    l_key_columns name[];
    l_key_kinds   text[];
BEGIN
    -- Resolve and validate the driving table and its key the same way the runner
    -- does, before any plan is produced. The template itself is validated by
    -- dml_utils_lib.explain_chunk_plan.
    PERFORM dml_utils_lib.assert_schema_exists(i_schema_name => i_driving_table_schema_name);
    PERFORM dml_utils_lib.assert_table_exists(
            i_schema_name => i_driving_table_schema_name,
            i_table_name => i_driving_table_name);

    IF i_chunk_by = 'blocks' THEN
        RAISE EXCEPTION 'explain_migration_chunk does not explain block chunks; use explain_migration_chunks'
            USING ERRCODE = '0A000';
    END IF;
    PERFORM dml_utils_lib.assert_boundary_key_index(
            i_chunk_by => i_chunk_by,
            i_boundary_key_index => i_boundary_key_index);

    IF i_chunk_by = 'ordered_key' THEN
        PERFORM dml_utils_lib.assert_usable_ordered_key(
                i_schema_name => i_driving_table_schema_name,
                i_table_name => i_driving_table_name,
                i_index_name => i_boundary_key_index);
        l_key_columns := dml_utils_lib.ordered_key_columns(
                i_schema_name => i_driving_table_schema_name,
                i_table_name => i_driving_table_name,
                i_index_name => i_boundary_key_index);
        l_key_kinds := dml_utils_lib.ordered_key_kinds(
                i_schema_name => i_driving_table_schema_name,
                i_table_name => i_driving_table_name,
                i_index_name => i_boundary_key_index);
    ELSE
        l_key_columns := dml_utils_lib.primary_key_columns(
                i_schema_name => i_driving_table_schema_name,
                i_table_name => i_driving_table_name);
        l_key_kinds := dml_utils_lib.primary_key_kinds(
                i_schema_name => i_driving_table_schema_name,
                i_table_name => i_driving_table_name);
    END IF;

    RETURN QUERY
        SELECT p.o_plan_kind, p.o_sql_text, p.o_plan
        FROM dml_utils_lib.explain_chunk_plan(
                     i_sql_text => i_sql_text,
                     i_schema_name => i_driving_table_schema_name,
                     i_table_name => i_driving_table_name,
                     i_table_alias => i_driving_table_alias,
                     i_key_columns => l_key_columns,
                     i_key_kinds => l_key_kinds,
                     i_start_values => i_start_values,
                     i_end_values => i_end_values,
                     i_is_final => i_is_final) AS p;
END;
$$;

COMMENT ON FUNCTION dml_utils.explain_migration_chunk IS
    'Returns the EXPLAIN (FORMAT JSON) plan for one explicitly supplied chunk of '
        'the template, as a single chunk_non_final or chunk_final row. The start '
        'and end values are one text value per key column, in key order; they are '
        'validated and cast by their column''s kind. i_chunk_by selects the key: '
        'primary_key (the default) or ordered_key with i_boundary_key_index. Use '
        'it to explain a real chunk (for example one read from '
        'dml_utils.migration_boundaries for a primary_key or ordered_key run) rather '
        'than only the synthetic representative ranges. Block chunks are not '
        'supported here: their boundaries are block numbers that render a ctid '
        'range, so use explain_migration_chunks for representative block-chunk '
        'plans. It resolves no run, reads no boundaries and writes nothing.';
