CREATE OR REPLACE FUNCTION dml_utils.explain_migration_chunks(
    i_sql_text dml_utils_data.non_null_text,
    i_driving_table_schema_name dml_utils_data.non_null_text,
    i_driving_table_name dml_utils_data.non_null_text,
    i_chunk_size dml_utils_data.positive_integer DEFAULT 1000,
    i_driving_table_alias dml_utils_data.non_null_text DEFAULT 't',
    i_chunk_by dml_utils_data.chunking_strategy DEFAULT 'primary_key',
    -- The unique index whose order an ordered_key run uses. Required for
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
    -- Validate the driving table and its key the same way the runner does,
    -- before any plan is produced. The template itself is validated by
    -- dml_utils_lib.explain_chunk_plans.
    PERFORM dml_utils_lib.assert_schema_exists(i_schema_name => i_driving_table_schema_name);
    PERFORM dml_utils_lib.assert_table_exists(
            i_schema_name => i_driving_table_schema_name,
            i_table_name => i_driving_table_name);

    PERFORM dml_utils_lib.assert_boundary_key_index(
            i_chunk_by => i_chunk_by,
            i_boundary_key_index => i_boundary_key_index);

    IF i_chunk_by = 'blocks' THEN
        PERFORM dml_utils_lib.assert_plain_heap(
                i_schema_name => i_driving_table_schema_name,
                i_table_name => i_driving_table_name);
        -- The placeholder columns and kind are unused by the block renderer.
        l_key_columns := ARRAY ['ctid']::name[];
        l_key_kinds := ARRAY ['bigint'];
    ELSIF i_chunk_by = 'ordered_key' THEN
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
        FROM dml_utils_lib.explain_chunk_plans(
                     i_sql_text => i_sql_text,
                     i_schema_name => i_driving_table_schema_name,
                     i_table_name => i_driving_table_name,
                     i_table_alias => i_driving_table_alias,
                     i_key_columns => l_key_columns,
                     i_key_kinds => l_key_kinds,
                     i_chunk_size => i_chunk_size::bigint,
                     i_chunk_by => i_chunk_by) AS p;
END;
$$;

COMMENT ON FUNCTION dml_utils.explain_migration_chunks IS
    'Returns the EXPLAIN (FORMAT JSON) plans for the SQL a chunked run of the '
        'template would generate: the boundary-population insert, a non-final '
        'chunk and the final chunk, as rows boundary_population, chunk_non_final '
        'and chunk_final, for the given chunking strategy (primary_key, ordered_key '
        'or blocks; for blocks both chunk plans are half-open ctid ranges). An '
        'ordered_key run requires i_boundary_key_index. It resolves no run, reads '
        'no boundaries and writes nothing; the chunk ranges are synthetic, so their '
        'estimates may differ from a real chunk. Use it to inspect the plans before '
        'running dml_utils.run_migration_chunks.';
