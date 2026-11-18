CREATE OR REPLACE FUNCTION dml_utils_lib.leaf_boundary_population_sql(
    i_chunk_by dml_utils_data.chunking_strategy,
    i_chunk_size integer,
    i_leaf_schema_name text,
    i_leaf_name text,
    i_run_id bigint,
    i_leaf_ordinal integer
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
    IF i_chunk_by = 'blocks' THEN
        RETURN dml_utils_lib.build_block_leaf_boundary_population_sql(
                i_schema_name => i_leaf_schema_name,
                i_table_name => i_leaf_name,
                i_run_id => i_run_id,
                i_chunk_size => i_chunk_size::bigint,
                i_leaf_ordinal => i_leaf_ordinal);
    END IF;

    SELECT o_key_columns,
           o_key_kinds
    INTO l_descriptor
    FROM dml_utils_lib.migration_run_boundary_key(i_run_id => i_run_id);

    RETURN dml_utils_lib.build_leaf_boundary_population_sql(
            i_schema_name => i_leaf_schema_name,
            i_table_name => i_leaf_name,
            i_key_columns => l_descriptor.o_key_columns,
            i_key_kinds => l_descriptor.o_key_kinds,
            i_run_id => i_run_id,
            i_chunk_size => i_chunk_size::bigint,
            i_leaf_ordinal => i_leaf_ordinal);
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.leaf_boundary_population_sql(
    dml_utils_data.chunking_strategy,
    integer,
    text,
    text,
    bigint,
    integer
    ) IS
    'Returns the statement that appends one claimed leaf''s chunk starts, using '
        'the blocks builder or the run''s stored key descriptor. The caller '
        'executes it in the leaf calculation transaction.';
