CREATE OR REPLACE FUNCTION dml_utils_lib.calculate_migration_leaf(
    i_run_id bigint
)
    RETURNS integer
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
DECLARE
    l_leaf           record;
    l_boundary_count bigint;
    l_terminal_key   dml_utils_data.migration_key;
BEGIN
    SELECT o_leaf_ordinal,
           o_leaf_schema_name,
           o_leaf_name,
           o_chunk_by,
           o_chunk_size
    INTO l_leaf
    FROM dml_utils_lib.claim_next_migration_leaf(i_run_id => i_run_id);

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    EXECUTE dml_utils_lib.leaf_boundary_population_sql(
            i_chunk_by => l_leaf.o_chunk_by,
            i_chunk_size => l_leaf.o_chunk_size,
            i_leaf_schema_name => l_leaf.o_leaf_schema_name,
            i_leaf_name => l_leaf.o_leaf_name,
            i_run_id => i_run_id,
            i_leaf_ordinal => l_leaf.o_leaf_ordinal);

    SELECT pg_catalog.count(*)::bigint
    INTO l_boundary_count
    FROM dml_utils_data.migration_boundary AS boundary
    WHERE boundary.run_id = i_run_id
      AND boundary.leaf_ordinal = l_leaf.o_leaf_ordinal;

    -- Snapshot the leaf's high-water key now, while its rows are the ones the
    -- starts were built from. The runner bounds the leaf's last chunk here, so a
    -- later delete or insert cannot regress or extend it.
    l_terminal_key := dml_utils_lib.leaf_terminal_boundary_id(
            i_run_id => i_run_id,
            i_leaf_ordinal => l_leaf.o_leaf_ordinal);

    UPDATE dml_utils_data.migration_run_leaf
    SET state                = 'calculated',
        boundary_count       = l_boundary_count,
        -- CASE, not a bare assignment: an empty leaf must store SQL NULL, not an
        -- all-NULL composite (which the terminal constraint would see as a value).
        terminal_boundary_id = CASE WHEN l_boundary_count > 0 THEN l_terminal_key END,
        calculated_at        = pg_catalog.clock_timestamp(),
        error_sqlstate       = NULL,
        error_message        = NULL
    WHERE run_id = i_run_id
      AND leaf_ordinal = l_leaf.o_leaf_ordinal;

    RETURN l_leaf.o_leaf_ordinal;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.calculate_migration_leaf(bigint) IS
    'Calculates one partition-wise leaf in the caller''s transaction: '
        'claim_next_migration_leaf claims and authorizes the first pending/failed/'
        'stale-calculating leaf, leaf_boundary_population_sql appends its chunk '
        'starts, and this routine records the boundary count, high-water snapshot '
        'and calculated state. Returns the ordinal, or NULL when the run is '
        'finalized, cancelled or fully calculated.';
