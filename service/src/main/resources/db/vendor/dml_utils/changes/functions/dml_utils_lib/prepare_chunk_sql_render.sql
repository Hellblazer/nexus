CREATE OR REPLACE FUNCTION dml_utils_lib.prepare_chunk_sql_render(
    i_sql_text dml_utils_data.non_null_text,
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_table_alias dml_utils_data.non_null_text,
    i_key_columns name[],
    i_key_kinds text[],
    OUT o_template text,
    OUT o_column_names text[],
    OUT o_key_collations text[]
)
    RETURNS record
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    -- Control-character sentinels that stand in for one token each. Both the
    -- <driving_table> and <chunking_clause> tokens become sentinels here;
    -- render_prepared_chunk_sql expands the table per chunk and the clause per
    -- range, so neither substitution can be re-scanned by the other.
    l_driving_table_sentinel   constant text := pg_catalog.chr(1);
    l_chunking_clause_sentinel constant text := pg_catalog.chr(2);
BEGIN
    PERFORM dml_utils_lib.assert_chunking_template(i_sql_text => i_sql_text);

    -- Validate the key and resolve the per-column render metadata once; the
    -- per-chunk renderer only consumes the resulting context.
    SELECT columns.o_column_names, columns.o_key_collations
    INTO o_column_names, o_key_collations
    FROM dml_utils_lib.build_key_render_columns(
                 i_table_alias => i_table_alias,
                 i_schema_name => i_schema_name,
                 i_table_name => i_table_name,
                 i_key_columns => i_key_columns,
                 i_key_kinds => i_key_kinds) AS columns;

    -- The caller's template could in principle contain a sentinel character;
    -- reject that so the substitutions stay unambiguous.
    IF pg_catalog.strpos(i_sql_text, l_driving_table_sentinel) > 0
        OR pg_catalog.strpos(i_sql_text, l_chunking_clause_sentinel) > 0
    THEN
        RAISE EXCEPTION 'template contains a reserved substitution character'
            USING ERRCODE = '22023';
    END IF;

    -- Replace both tokens with sentinels. The driving table is deliberately left
    -- as a sentinel: the prepared context is reused for every chunk of a run, and
    -- a partition-wise run renders each chunk against its leaf. The caller passes
    -- the concrete schema, table and alias to render_prepared_chunk_sql, so the
    -- same context serves the parent or any of its leaves.
    o_template := pg_catalog.replace(i_sql_text, '<driving_table>', l_driving_table_sentinel);
    o_template := pg_catalog.replace(o_template, '<chunking_clause>', l_chunking_clause_sentinel);
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.prepare_chunk_sql_render IS
    'Validates a chunking template and its key columns and kinds once, and '
        'returns the run-invariant render context: the template with both '
        '<driving_table> and <chunking_clause> replaced by internal sentinels, the '
        'alias-qualified quoted column names, and each text column''s collation '
        '(NULL when the column is not in the catalog). The key metadata is built by '
        'build_key_render_columns. The column names are qualified with '
        'i_table_alias, so every render from this context must pass that same '
        'alias; render_prepared_chunk_sql rejects a different one. The caller must '
        'pass the kind resolved from the catalog for each column; the public '
        'wrappers derive the columns and kinds together, so a direct lib caller is '
        'responsible for keeping them consistent. render_prepared_chunk_sql '
        'consumes the context per chunk, selects the concrete driving table (the '
        'parent or one leaf), and substitutes the chunking clause.';
