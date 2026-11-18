CREATE OR REPLACE FUNCTION dml_utils_lib.complete_migration_boundary(
    i_run_id bigint,
    i_boundary_no bigint,
    i_started_at timestamptz
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
DECLARE
    l_updated      integer;
    l_completed_at timestamptz;
BEGIN
    IF i_started_at IS NULL THEN
        RAISE EXCEPTION 'i_started_at must not be NULL'
            USING ERRCODE = '22023';
    END IF;

    -- Use one timestamp for both columns and clamp a caller-supplied future
    -- start, so a clock-skewed caller cannot violate
    -- migration_boundary_time_order_check and abort an already-executed chunk.
    l_completed_at := pg_catalog.clock_timestamp();

    UPDATE dml_utils_data.migration_boundary
    SET started_at   = LEAST(i_started_at, l_completed_at),
        completed_at = l_completed_at
    WHERE run_id = i_run_id
      AND boundary_no = i_boundary_no
      AND completed_at IS NULL
      AND cancelled_at IS NULL;

    GET DIAGNOSTICS l_updated = ROW_COUNT;
    IF l_updated = 0 THEN
        -- P0C02 is a completion-protocol failure, distinct from P0C01 (claim)
        -- and from P0002 raised inside user chunk SQL.
        RAISE EXCEPTION 'migration boundary % for run % was not found while completing it',
            i_boundary_no, i_run_id
            USING ERRCODE = 'P0C02';
    END IF;

    -- Keep the counter in the same transaction as the boundary completion so it
    -- cannot drift from the completed boundaries.
    UPDATE dml_utils_data.migration_run
    SET total_chunks_completed = total_chunks_completed + 1
    WHERE run_id = i_run_id;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.complete_migration_boundary IS
    'Records started_at/completed_at for one pending boundary and advances the '
        'run''s completed-chunk counter. The pending/cancelled guards make a second '
        'call a P0C02 error without double-incrementing the counter. SECURITY '
        'DEFINER because the caller-owned chunk worker intentionally has no metadata '
        'write privileges.';
