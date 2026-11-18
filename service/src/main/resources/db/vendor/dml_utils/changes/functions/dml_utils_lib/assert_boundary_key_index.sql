CREATE OR REPLACE FUNCTION dml_utils_lib.assert_boundary_key_index(
    i_chunk_by dml_utils_data.chunking_strategy,
    i_boundary_key_index name
)
    RETURNS void
    LANGUAGE plpgsql
    IMMUTABLE
    SECURITY INVOKER
AS
$$
BEGIN
    -- The index selects an ordered key, so it is required for that strategy and
    -- must be absent for primary_key/blocks, whose runs store no key descriptor.
    -- This is the new-run and explain contract; a resumed run keeps its stored
    -- strategy and index (resolve_migration_run notices and ignores a differing
    -- input rather than re-validating it here).
    IF i_chunk_by IS NULL THEN
        RAISE EXCEPTION 'i_chunk_by must not be NULL'
            USING ERRCODE = '22023';
    END IF;

    -- A blank name is not a usable index; treat it as absent here so the caller
    -- gets the parameter error instead of a misleading 42704 on the next step.
    IF i_boundary_key_index IS NOT NULL
        AND pg_catalog.btrim(i_boundary_key_index::text) = ''
    THEN
        RAISE EXCEPTION 'i_boundary_key_index must not be blank'
            USING ERRCODE = '22023';
    END IF;

    IF i_chunk_by = 'ordered_key' AND i_boundary_key_index IS NULL THEN
        RAISE EXCEPTION 'i_boundary_key_index is required when i_chunk_by is ordered_key'
            USING ERRCODE = '22023';
    END IF;

    IF i_chunk_by <> 'ordered_key' AND i_boundary_key_index IS NOT NULL THEN
        RAISE EXCEPTION 'i_boundary_key_index is only valid when i_chunk_by is ordered_key'
            USING ERRCODE = '22023';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_boundary_key_index IS
    'Raises invalid_parameter_value (22023) when i_chunk_by is NULL, when '
        'i_boundary_key_index is blank, when ordered_key is selected without an '
        'index, or when an index is supplied for primary_key or blocks. Shared by '
        'the new-run and explain paths; a resumed run keeps its stored strategy '
        'and index.';
