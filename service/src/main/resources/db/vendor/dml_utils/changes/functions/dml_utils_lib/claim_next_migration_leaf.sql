CREATE OR REPLACE FUNCTION dml_utils_lib.claim_next_migration_leaf(
    i_run_id bigint
)
    RETURNS TABLE
            (
                o_leaf_ordinal     integer,
                o_leaf_schema_name text,
                o_leaf_name        text,
                o_chunk_by         dml_utils_data.chunking_strategy,
                o_chunk_size       integer
            )
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
DECLARE
    l_run    record;
    l_leaf   record;
    l_caller name;
BEGIN
    IF i_run_id IS NULL THEN
        RAISE EXCEPTION 'run id must not be NULL'
            USING ERRCODE = '22023';
    END IF;

    -- The run row lock serializes leaf calculation for this run: a second worker
    -- waits here, then sees the committed leaf state instead of appending the
    -- same leaf twice. The coordinator also holds the run-level advisory lock.
    SELECT run.partition_wise,
           run.boundaries_calculated_at,
           run.cancelled_at,
           run.chunk_by,
           run.chunk_size
    INTO l_run
    FROM dml_utils_data.migration_run AS run
    WHERE run.run_id = i_run_id
        FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'migration run % does not exist', i_run_id
            USING ERRCODE = 'P0002';
    END IF;

    IF NOT l_run.partition_wise THEN
        RAISE EXCEPTION 'run % is not partition-wise', i_run_id
            USING ERRCODE = '22023';
    END IF;

    -- A finalized run has nothing left to calculate, and a cancelled run stops
    -- launching leaves. Both are a no-op so a resume can call this safely.
    IF l_run.boundaries_calculated_at IS NOT NULL OR l_run.cancelled_at IS NOT NULL THEN
        RETURN;
    END IF;

    -- The first leaf that is not calculated, in manifest order. pending,
    -- calculating (a crashed claim) and failed are all retryable; calculated is
    -- never recomputed.
    SELECT leaf.leaf_ordinal,
           leaf.leaf_schema_name,
           leaf.leaf_name
    INTO l_leaf
    FROM dml_utils_data.migration_run_leaf AS leaf
    WHERE leaf.run_id = i_run_id
      AND leaf.state <> 'calculated'
    ORDER BY leaf.leaf_ordinal
        FOR UPDATE;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    -- The stored range is only valid for the relation it was built from; fail
    -- closed on any change before appending more boundaries.
    PERFORM dml_utils_lib.assert_migration_leaf_unchanged(
            i_run_id => i_run_id,
            i_leaf_ordinal => l_leaf.leaf_ordinal);

    -- This function is SECURITY DEFINER so the metadata writes and the leaf scan
    -- run as the owner. The scan must still be authorized by the caller, so
    -- recover the effective caller from SET ROLE and check the leaf.
    l_caller := COALESCE(NULLIF(pg_catalog.current_setting('role', true), 'none'),
                         session_user)::name;
    IF NOT pg_catalog.has_table_privilege(
            l_caller,
            pg_catalog.format('%I.%I', l_leaf.leaf_schema_name, l_leaf.leaf_name),
            'SELECT')
    THEN
        RAISE EXCEPTION 'caller lacks SELECT privilege on leaf %.%',
            l_leaf.leaf_schema_name, l_leaf.leaf_name
            USING ERRCODE = '42501';
    END IF;

    -- Claim and calculation share the caller's transaction, so a crash leaves
    -- the leaf pending/failed rather than calculation without boundaries. A
    -- retried failed or stale-calculating leaf starts from an empty count.
    UPDATE dml_utils_data.migration_run_leaf AS leaf
    SET state          = 'calculating',
        boundary_count = 0,
        calculated_at  = NULL,
        error_sqlstate = NULL,
        error_message  = NULL
    WHERE leaf.run_id = i_run_id
      AND leaf.leaf_ordinal = l_leaf.leaf_ordinal;

    RETURN QUERY
        SELECT l_leaf.leaf_ordinal,
               l_leaf.leaf_schema_name::text,
               l_leaf.leaf_name::text,
               l_run.chunk_by,
               l_run.chunk_size::integer;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.claim_next_migration_leaf(bigint) IS
    'Locks the run and claims the first pending/failed/stale-calculating leaf in '
        'manifest order, verifies it still matches the manifest, checks the '
        'effective caller can SELECT it and marks it calculating. Returns the leaf '
        'identity plus the run''s chunk strategy and size, or no row when the run '
        'is finalized, cancelled or fully calculated.';
