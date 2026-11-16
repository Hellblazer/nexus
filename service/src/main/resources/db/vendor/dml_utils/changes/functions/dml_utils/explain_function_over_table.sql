CREATE OR REPLACE FUNCTION dml_utils.explain_function_over_table(
    i_driving_table_schema_name dml_utils_data.non_null_text,
    i_driving_table_name dml_utils_data.non_null_text,
    i_function_schema_name dml_utils_data.non_null_text,
    i_function_name dml_utils_data.non_null_text,
    i_chunk_size dml_utils_data.positive_integer DEFAULT 1000,
    -- Plain text, not non_null_text: an omitted filter is NULL and means no
    -- filter. non_null_text would reject the DEFAULT NULL before the body.
    i_filter text DEFAULT NULL,
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
DECLARE
    l_function_chunk_template text;
    l_primary_key_columns     name[];
    l_key_kinds               text[];
BEGIN
    -- The generated function template binds the primary-key column types, so an
    -- ordered key would change the function contract; it is out of scope for v1.
    IF i_chunk_by = 'ordered_key' THEN
        RAISE EXCEPTION 'explain_function_over_table does not support ordered_key chunking'
            USING ERRCODE = '0A000';
    END IF;

    -- Validate the function against the driving table's primary key and build
    -- the per-row template first, exactly as the runner does; a bad table or
    -- function fails before any plan is produced.
    l_function_chunk_template := dml_utils_lib.build_function_chunk_template(
            i_table_schema_name => i_driving_table_schema_name,
            i_table_name => i_driving_table_name,
            i_function_schema_name => i_function_schema_name,
            i_function_name => i_function_name,
            i_filter => i_filter);

    IF i_chunk_by = 'blocks' THEN
        PERFORM dml_utils_lib.assert_plain_heap(
                i_schema_name => i_driving_table_schema_name,
                i_table_name => i_driving_table_name);
        -- The placeholder columns and kind are unused by the block renderer.
        l_primary_key_columns := ARRAY ['ctid']::name[];
        l_key_kinds := ARRAY ['bigint'];
    ELSE
        l_primary_key_columns := dml_utils_lib.primary_key_columns(
                i_schema_name => i_driving_table_schema_name,
                i_table_name => i_driving_table_name);
        l_key_kinds := dml_utils_lib.primary_key_kinds(
                i_schema_name => i_driving_table_schema_name,
                i_table_name => i_driving_table_name);
    END IF;

    -- The generated template is a valid <driving_table>/<chunking_clause>
    -- template whose alias is fixed at 't', so the shared explain path renders
    -- and plans it unchanged.
    RETURN QUERY
        SELECT p.o_plan_kind, p.o_sql_text, p.o_plan
        FROM dml_utils_lib.explain_chunk_plans(
                     i_sql_text => l_function_chunk_template,
                     i_schema_name => i_driving_table_schema_name,
                     i_table_name => i_driving_table_name,
                     i_table_alias => 't',
                     i_key_columns => l_primary_key_columns,
                     i_key_kinds => l_key_kinds,
                     i_chunk_size => i_chunk_size::bigint,
                     i_chunk_by => i_chunk_by) AS p;
END;
$$;

COMMENT ON FUNCTION dml_utils.explain_function_over_table IS
    'Returns the EXPLAIN (FORMAT JSON) plans for the SQL a '
        'run_function_over_table call would generate: the boundary-population '
        'insert, a non-final chunk and the final chunk, as rows '
        'boundary_population, chunk_non_final and chunk_final, for the given '
        'chunking strategy (primary_key or blocks). The function must return void '
        'with argument types matching the driving table''s primary key, in key '
        'order. A non-NULL i_filter is ANDed onto the chunk plans'' range '
        'predicate (the boundary-population plan is unaffected). It resolves no '
        'run, reads no boundaries and writes nothing; the chunk ranges are '
        'synthetic, so their estimates may differ from a real chunk.';
