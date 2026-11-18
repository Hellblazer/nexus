CREATE OR REPLACE FUNCTION dml_utils_lib.leaf_terminal_boundary_id(
    i_run_id bigint,
    i_leaf_ordinal integer
)
    RETURNS dml_utils_data.migration_key
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
DECLARE
    l_chunk_by           dml_utils_data.chunking_strategy;
    l_schema_name        text;
    l_name               text;
    l_run_key            record;
    l_descriptor         record;
    l_block_count        bigint;
    l_bigint_values      bigint[];
    l_text_values        text[];
    l_uuid_values        uuid[];
    l_timestamptz_values timestamptz[];
    l_bytea_values       bytea[];
BEGIN
    SELECT run.chunk_by, leaf.leaf_schema_name::text, leaf.leaf_name::text
    INTO l_chunk_by, l_schema_name, l_name
    FROM dml_utils_data.migration_run AS run
             JOIN dml_utils_data.migration_run_leaf AS leaf ON leaf.run_id = run.run_id
    WHERE run.run_id = i_run_id
      AND leaf.leaf_ordinal = i_leaf_ordinal;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'leaf ordinal % does not belong to run %', i_leaf_ordinal, i_run_id
            USING ERRCODE = '55000';
    END IF;

    IF l_chunk_by = 'blocks' THEN
        -- The terminal block is one past the end of the leaf's heap, matching the
        -- single-relation block builder's terminal boundary.
        EXECUTE pg_catalog.format(
                'SELECT pg_catalog.pg_relation_size(%L::pg_catalog.regclass)'
                    ' / pg_catalog.current_setting(''block_size'')::bigint',
                pg_catalog.format('%I.%I', l_schema_name, l_name))
            INTO l_block_count;

        IF l_block_count = 0 THEN
            RETURN NULL;
        END IF;

        RETURN ROW (ARRAY [l_block_count]::bigint[], NULL::text[], NULL::uuid[],
            NULL::timestamptz[], NULL::bytea[])::dml_utils_data.migration_key;
    END IF;

    -- The terminal high-water key is the last row of the leaf in the run's key
    -- order. The descriptor is the same one the leaf's starts were built from,
    -- so the terminal is comparable with them. The packed key is selected as its
    -- five arrays because a composite result cannot be assigned to a composite
    -- variable directly.
    SELECT o_key_columns,
           o_key_kinds
    INTO l_run_key
    FROM dml_utils_lib.migration_run_boundary_key(i_run_id => i_run_id);

    SELECT o_select_list,
           o_id_list,
           o_order_by,
           o_order_by_desc,
           o_pack_expression
    INTO l_descriptor
    FROM dml_utils_lib.boundary_key_descriptor_sql(
            i_key_columns => l_run_key.o_key_columns,
            i_key_kinds => l_run_key.o_key_kinds);

    EXECUTE pg_catalog.format(
            'SELECT (packed.key).bigint_values, (packed.key).text_values,'
                ' (packed.key).uuid_values, (packed.key).timestamptz_values,'
                ' (packed.key).bytea_values'
                ' FROM (SELECT %s AS key'
                ' FROM (SELECT %s FROM %I.%I ORDER BY %s LIMIT 1) AS last_row) AS packed',
            l_descriptor.o_pack_expression,
            l_descriptor.o_select_list,
            l_schema_name,
            l_name,
            l_descriptor.o_order_by_desc)
        INTO l_bigint_values, l_text_values, l_uuid_values, l_timestamptz_values,
            l_bytea_values;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN ROW (l_bigint_values, l_text_values, l_uuid_values,
        l_timestamptz_values, l_bytea_values)::dml_utils_data.migration_key;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.leaf_terminal_boundary_id(
    bigint,
    integer
    ) IS
    'Returns the high-water migration_key for a leaf: the last row in the run''s '
        'key order, or one-past-end block number for blocks. finalize_'
        'migration_boundaries uses it for the run''s single terminal boundary.';
