CREATE OR REPLACE FUNCTION dml_utils.explain_function_over_table_chunk(
    i_driving_table_schema_name dml_utils_data.non_null_text,
    i_driving_table_name dml_utils_data.non_null_text,
    i_function_schema_name dml_utils_data.non_null_text,
    i_function_name dml_utils_data.non_null_text,
    i_start_values text[],
    i_end_values text[],
    i_is_final boolean,
    -- Plain text, not non_null_text: an omitted filter is NULL and means no
    -- filter. non_null_text would reject the DEFAULT NULL before the body.
    i_filter text DEFAULT NULL
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
    -- Validate the function against the driving table's primary key and build
    -- the per-row template first, exactly as the runner does; a bad table or
    -- function fails before any plan is produced.
    l_function_chunk_template := dml_utils_lib.build_function_chunk_template(
            i_table_schema_name => i_driving_table_schema_name,
            i_table_name => i_driving_table_name,
            i_function_schema_name => i_function_schema_name,
            i_function_name => i_function_name,
            i_filter => i_filter);

    l_primary_key_columns := dml_utils_lib.primary_key_columns(
            i_schema_name => i_driving_table_schema_name,
            i_table_name => i_driving_table_name);
    l_key_kinds := dml_utils_lib.primary_key_kinds(
            i_schema_name => i_driving_table_schema_name,
            i_table_name => i_driving_table_name);

    -- The generated template is a valid <driving_table>/<chunking_clause>
    -- template whose alias is fixed at 't', so the shared explain path renders
    -- and plans it unchanged.
    RETURN QUERY
        SELECT p.o_plan_kind, p.o_sql_text, p.o_plan
        FROM dml_utils_lib.explain_chunk_plan(
                     i_sql_text => l_function_chunk_template,
                     i_schema_name => i_driving_table_schema_name,
                     i_table_name => i_driving_table_name,
                     i_table_alias => 't',
                     i_key_columns => l_primary_key_columns,
                     i_key_kinds => l_key_kinds,
                     i_start_values => i_start_values,
                     i_end_values => i_end_values,
                     i_is_final => i_is_final) AS p;
END;
$$;

COMMENT ON FUNCTION dml_utils.explain_function_over_table_chunk IS
    'Returns the EXPLAIN (FORMAT JSON) plan for one explicitly supplied chunk of '
        'a run_function_over_table call, as a single chunk_non_final or '
        'chunk_final row. For the primary_key strategy only: a blocks-strategy '
        'chunk is a half-open ctid range and cannot be expressed here (see '
        'dml_utils.explain_function_over_table for representative block-chunk '
        'plans). The function must return void with argument types matching the '
        'driving table''s primary key, in key order. The start and end values are '
        'one text value per primary-key column, in key order; a non-NULL i_filter '
        'is ANDed onto the chunk predicate. It resolves no run, reads no '
        'boundaries and writes nothing.';
