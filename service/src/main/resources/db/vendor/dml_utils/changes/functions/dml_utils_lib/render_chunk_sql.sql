CREATE OR REPLACE FUNCTION dml_utils_lib.render_chunk_sql(
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
    RETURNS text
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_prepared record;
BEGIN
    -- Prepare the run-invariant context and render one chunk from it. A caller that
    -- renders many chunks for the same table (run_migration_chunks) calls the two
    -- steps directly and prepares once, so the invariant work is not repeated per
    -- chunk; this one-shot form exists for a single render and for tests.
    l_prepared := dml_utils_lib.prepare_chunk_sql_render(
            i_sql_text => i_sql_text,
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_table_alias => i_table_alias,
            i_key_columns => i_key_columns,
            i_key_kinds => i_key_kinds);

    RETURN dml_utils_lib.render_prepared_chunk_sql(
            i_template => l_prepared.o_template,
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_table_alias => i_table_alias,
            i_column_names => l_prepared.o_column_names,
            i_key_kinds => i_key_kinds,
            i_key_collations => l_prepared.o_key_collations,
            i_start_values => i_start_values,
            i_end_values => i_end_values,
            i_is_final => i_is_final);
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.render_chunk_sql IS
    'Returns the SQL template with <driving_table> and <chunking_clause> '
        'substituted for the given table, alias, key columns and chunk range; each '
        'key kind (bigint, text, uuid, timestamptz or bytea) selects the explicit cast. '
        'The chunking clause is a disjunction of axis-aligned boxes that exactly '
        'tiles the lexicographic range, so the planner estimates it column by '
        'column and uses the key index. The final chunk uses an inclusive upper '
        'bound, and a NULL boundary value is rejected. A text key is ordered under '
        'its column''s collation, matching the predicate. This is the one-shot pair '
        'of prepare_chunk_sql_render and render_prepared_chunk_sql.';
