CREATE OR REPLACE FUNCTION dml_utils_lib.assert_chunking_template(
    i_sql_text dml_utils_data.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    IMMUTABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_driving_table_count   integer;
    l_chunking_clause_count integer;
    l_probe                 text;
BEGIN
    -- Count occurrences of each token by removing it and comparing lengths:
    -- length difference / token length = number of occurrences. The tokens are
    -- fixed literals, so this avoids regex escaping entirely.
    l_probe := pg_catalog.replace(i_sql_text, '<driving_table>', '');
    l_driving_table_count :=
            (pg_catalog.length(i_sql_text) - pg_catalog.length(l_probe))
                / pg_catalog.length('<driving_table>');

    l_probe := pg_catalog.replace(i_sql_text, '<chunking_clause>', '');
    l_chunking_clause_count :=
            (pg_catalog.length(i_sql_text) - pg_catalog.length(l_probe))
                / pg_catalog.length('<chunking_clause>');

    IF l_driving_table_count <> 1 THEN
        RAISE EXCEPTION 'i_sql_text must contain <driving_table> exactly once'
            USING ERRCODE = '22023',
                HINT =
                        'the tokens are counted as raw substrings; they must appear in executable SQL, not inside a string literal, comment or a longer token';
    END IF;

    IF l_chunking_clause_count <> 1 THEN
        RAISE EXCEPTION 'i_sql_text must contain <chunking_clause> exactly once'
            USING ERRCODE = '22023',
                HINT =
                        'the tokens are counted as raw substrings; they must appear in executable SQL, not inside a string literal, comment or a longer token';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_chunking_template IS
    'Raises invalid_parameter_value (22023) unless the SQL template contains '
        '<driving_table> and <chunking_clause> exactly once each. The count is a '
        'raw substring count, so the tokens must appear in executable SQL, not '
        'inside comments or string literals.';
