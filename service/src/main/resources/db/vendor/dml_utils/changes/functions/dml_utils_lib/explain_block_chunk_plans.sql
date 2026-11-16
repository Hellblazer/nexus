CREATE OR REPLACE FUNCTION dml_utils_lib.explain_block_chunk_plans(
    i_sql_text dml_utils_data.non_null_text,
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_table_alias dml_utils_data.non_null_text,
    i_chunk_size bigint
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
    -- 1. The block boundary-population insert, with a synthetic run id (0):
    -- EXPLAIN plans the insert as a ModifyTable and does not evaluate the
    -- foreign key, so no run needs to exist and nothing is written.
    o_plan_kind := 'boundary_population';
    o_sql_text := dml_utils_lib.build_block_boundary_population_sql(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_run_id => 0,
            i_chunk_size => i_chunk_size);
    o_plan := dml_utils_lib.explain_query_plan(i_sql_text => o_sql_text);
    RETURN NEXT;

    -- 2 and 3. Two representative half-open block ranges. There is no
    -- final/inclusive distinction for blocks, so chunk_final is simply a
    -- later chunk; both are half-open ctid ranges.
    o_plan_kind := 'chunk_non_final';
    o_sql_text := dml_utils_lib.render_block_chunk_sql(
            i_sql_text => i_sql_text,
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_table_alias => i_table_alias,
            i_start_block => 0,
            i_end_block => i_chunk_size);
    o_plan := dml_utils_lib.explain_query_plan(i_sql_text => o_sql_text);
    RETURN NEXT;

    o_plan_kind := 'chunk_final';
    o_sql_text := dml_utils_lib.render_block_chunk_sql(
            i_sql_text => i_sql_text,
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_table_alias => i_table_alias,
            i_start_block => i_chunk_size,
            i_end_block => 2 * i_chunk_size);
    o_plan := dml_utils_lib.explain_query_plan(i_sql_text => o_sql_text);
    RETURN NEXT;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.explain_block_chunk_plans(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    bigint
    ) IS
    'Returns the EXPLAIN (FORMAT JSON) plans for a blocks run: the boundary '
        'insert and two representative half-open ctid ranges. No run or boundary '
        'is read or written. Shared by explain_chunk_plans.';
