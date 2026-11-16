CREATE OR REPLACE FUNCTION dml_utils_lib.build_boundary_population_sql(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_key_columns name[],
    i_key_kinds text[],
    i_run_id bigint,
    i_chunk_size bigint
)
    RETURNS text
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_descriptor record;
BEGIN
    -- The run id and chunk size are inlined (both bigint, so no injection
    -- surface). A NULL run id would render an empty token and a non-positive
    -- chunk size would render a degenerate divisor.
    IF i_run_id IS NULL OR i_chunk_size IS NULL OR i_chunk_size <= 0 THEN
        RAISE EXCEPTION 'run id must not be NULL and chunk size must be positive'
            USING ERRCODE = '22023';
    END IF;

    -- The shared helper validates the key descriptor and renders the select,
    -- order and pack fragments. Both the single-relation and per-leaf builders
    -- use it, so the key logic cannot drift between them.
    SELECT o_select_list,
           o_id_list,
           o_order_by,
           o_order_by_desc,
           o_pack_expression
    INTO l_descriptor
    FROM dml_utils_lib.boundary_key_descriptor_sql(
            i_key_columns => i_key_columns,
            i_key_kinds => i_key_kinds);

    RETURN dml_utils_lib.format_boundary_population_sql(
            i_select_list => l_descriptor.o_select_list,
            i_order_by => l_descriptor.o_order_by,
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_id_list => l_descriptor.o_id_list,
            i_pack_expression => l_descriptor.o_pack_expression,
            i_order_by_desc => l_descriptor.o_order_by_desc,
            i_run_id => i_run_id,
            i_chunk_size => i_chunk_size);
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.build_boundary_population_sql IS
    'Returns the statement that scans the driving table in boundary-key order and '
        'inserts the run''s chunk boundaries (one start per chunk plus the terminal '
        'high-water boundary) as the contiguous sequence 0..N. The caller supplies '
        'the key columns and kinds (primary_key_columns/primary_key_kinds for a '
        'primary-key run, or ordered_key_columns/ordered_key_kinds once the '
        'ordered-key path is wired); the run id and chunk size are inlined so the '
        'runner executes and the explain path explains the identical statement. '
        'format_boundary_population_sql owns the statement text.';
