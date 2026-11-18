CREATE OR REPLACE FUNCTION dml_utils_lib.explain_key_chunk_plans(
    i_sql_text dml_utils_data.non_null_text,
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_table_alias dml_utils_data.non_null_text,
    i_key_columns name[],
    i_key_kinds text[],
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
DECLARE
    l_range record;
BEGIN
    -- 1. The statement that scans the driving table in key order and inserts the
    -- run's boundaries. The run id is synthetic (0): EXPLAIN plans the insert as
    -- a ModifyTable and does not evaluate the foreign key, so no run needs to
    -- exist and nothing is written.
    o_plan_kind := 'boundary_population';
    o_sql_text := dml_utils_lib.build_boundary_population_sql(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_key_columns => i_key_columns,
            i_key_kinds => i_key_kinds,
            i_run_id => 0,
            i_chunk_size => i_chunk_size);
    o_plan := dml_utils_lib.explain_query_plan(i_sql_text => o_sql_text);
    RETURN NEXT;

    -- 2 and 3. A representative non-final and final chunk, rendered from
    -- synthetic ranges so no boundaries are read. ORDER BY o_is_final puts the
    -- non-final (false) row before the final (true) one.
    FOR l_range IN
        SELECT v.o_is_final, v.o_start_values, v.o_end_values
        FROM dml_utils_lib.synthetic_chunk_boundary_values(
                     i_key_kinds => i_key_kinds,
                     i_chunk_size => i_chunk_size) AS v
        ORDER BY v.o_is_final
        LOOP
            o_plan_kind := CASE WHEN l_range.o_is_final THEN 'chunk_final' ELSE 'chunk_non_final' END;
            o_sql_text := dml_utils_lib.render_chunk_sql(
                    i_sql_text => i_sql_text,
                    i_schema_name => i_schema_name,
                    i_table_name => i_table_name,
                    i_table_alias => i_table_alias,
                    i_key_columns => i_key_columns,
                    i_key_kinds => i_key_kinds,
                    i_start_values => l_range.o_start_values,
                    i_end_values => l_range.o_end_values,
                    i_is_final => l_range.o_is_final);
            o_plan := dml_utils_lib.explain_query_plan(i_sql_text => o_sql_text);
            RETURN NEXT;
        END LOOP;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.explain_key_chunk_plans(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    bigint
    ) IS
    'Returns the EXPLAIN (FORMAT JSON) plans for a key-chunked run: the boundary '
        'insert and a representative non-final and final chunk rendered from '
        'synthetic ranges. No run or boundary is read or written. Shared by '
        'explain_chunk_plans.';
