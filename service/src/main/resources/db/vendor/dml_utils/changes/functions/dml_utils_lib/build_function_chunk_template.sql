CREATE OR REPLACE FUNCTION dml_utils_lib.build_function_chunk_template(
    i_table_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_function_schema_name dml_utils_data.non_null_text,
    i_function_name dml_utils_data.non_null_text,
    -- Plain text, not non_null_text: an omitted filter is NULL and means no
    -- filter. non_null_text would reject the DEFAULT NULL before the body.
    i_filter text DEFAULT NULL
)
    RETURNS text
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_call_arguments          text;
    l_function_chunk_template text;
BEGIN
    IF i_filter IS NOT NULL AND pg_catalog.btrim(i_filter) = '' THEN
        RAISE EXCEPTION 'i_filter must not be blank'
            USING ERRCODE = '22023';
    END IF;

    SELECT target.o_call_arguments
    INTO l_call_arguments
    FROM dml_utils_lib.resolve_function_chunk_target(
                 i_table_schema_name => i_table_schema_name,
                 i_table_name => i_table_name,
                 i_function_schema_name => i_function_schema_name,
                 i_function_name => i_function_name) AS target;

    -- The template calls the function once per row of the chunk's key range,
    -- passing the primary-key columns as arguments. <driving_table> and
    -- <chunking_clause> are filled in per chunk by render_chunk_sql. A non-NULL
    -- i_filter is appended as a further predicate, so each chunk's range is
    -- ANDed with the caller's filter; the filter references the driving table
    -- through the fixed alias t.
    l_function_chunk_template := pg_catalog.format(
            'SELECT %I.%I(%s) FROM <driving_table> WHERE <chunking_clause>',
            i_function_schema_name,
            i_function_name,
            l_call_arguments);

    IF i_filter IS NOT NULL THEN
        l_function_chunk_template := l_function_chunk_template || ' AND (' || i_filter || ')';
    END IF;

    RETURN l_function_chunk_template;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.build_function_chunk_template IS
    'Builds the <driving_table>/<chunking_clause> template that calls the given '
        'function once per row, passing the driving table''s primary-key columns; '
        'resolve_function_chunk_target validates the function and builds the call '
        'list. A non-NULL i_filter is appended as an extra ANDed predicate, so only '
        'the rows it matches are passed to the function.';
