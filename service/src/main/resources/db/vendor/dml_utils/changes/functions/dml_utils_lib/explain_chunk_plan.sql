CREATE OR REPLACE FUNCTION dml_utils_lib.explain_chunk_plan(
    i_sql_text dml_utils_data.non_null_text,
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_table_alias dml_utils_data.non_null_text,
    i_key_columns name[],
    i_key_kinds text[],
    i_start_values text[],
    i_end_values text[],
    i_is_final boolean
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
    -- Validate the template up front, exactly as explain_chunk_plans does, so a
    -- bad call fails before any plan is produced.
    PERFORM dml_utils_lib.assert_chunking_template(i_sql_text => i_sql_text);

    -- Render the caller's explicit range. render_chunk_sql validates and casts
    -- the boundary values and rejects a reversed or empty range, so the EXPLAIN
    -- below always sees a valid chunk. The plan kind matches
    -- explain_chunk_plans's chunk rows: the final chunk uses an inclusive upper
    -- bound.
    o_plan_kind := CASE WHEN i_is_final THEN 'chunk_final' ELSE 'chunk_non_final' END;
    o_sql_text := dml_utils_lib.render_chunk_sql(
            i_sql_text => i_sql_text,
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_table_alias => i_table_alias,
            i_key_columns => i_key_columns,
            i_key_kinds => i_key_kinds,
            i_start_values => i_start_values,
            i_end_values => i_end_values,
            i_is_final => i_is_final);
    o_plan := dml_utils_lib.explain_query_plan(i_sql_text => o_sql_text);
    RETURN NEXT;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.explain_chunk_plan IS
    'Returns the EXPLAIN (FORMAT JSON) plan for one explicitly supplied key chunk '
        'range, as a single chunk_non_final or chunk_final row. Unlike '
        'explain_chunk_plans it explains the exact predicate a real chunk renders, '
        'including the prefix-equality shapes a multi-column key produces when the '
        'leading key columns do not differ between the start and end boundaries. '
        'render_chunk_sql validates and casts the range; no run or boundary is read '
        'or written.';
