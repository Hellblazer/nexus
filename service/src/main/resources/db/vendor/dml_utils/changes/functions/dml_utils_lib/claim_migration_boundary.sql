CREATE OR REPLACE FUNCTION dml_utils_lib.claim_migration_boundary(
    i_run_id bigint,
    i_boundary_no bigint
)
    RETURNS timestamptz
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
DECLARE
    l_boundary_no bigint;
BEGIN
    -- Lock the boundary row for the rest of the caller's transaction. The lock
    -- is retained after this SECURITY DEFINER function returns, so a concurrent
    -- worker blocks here and then re-checks completed_at under READ COMMITTED.
    SELECT b.boundary_no
    INTO l_boundary_no
    FROM dml_utils_data.migration_boundary AS b
    WHERE b.run_id = i_run_id
      AND b.boundary_no = i_boundary_no
      AND b.completed_at IS NULL
      AND b.cancelled_at IS NULL
        FOR UPDATE;

    IF l_boundary_no IS NULL THEN
        -- P0C01 is the claim protocol's dedicated no-row code. It must not be
        -- P0002: a P0002 raised by the user's chunk SQL would otherwise be
        -- mistaken by run_migration_chunks for a benign concurrent claim and
        -- retried forever.
        RAISE EXCEPTION 'migration boundary % for run % was not found, is already completed or was cancelled',
            i_boundary_no, i_run_id
            USING ERRCODE = 'P0C01';
    END IF;

    -- The coordinator records this as started_at only after the chunk succeeds.
    RETURN pg_catalog.clock_timestamp();
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.claim_migration_boundary IS
    'Locks one pending migration_boundary row for the caller''s transaction and '
        'returns the claim timestamp. SECURITY DEFINER so the invoker-owned '
        'process_migration_chunk does not need metadata write privileges. The row '
        'lock is retained until the caller''s transaction ends.';
