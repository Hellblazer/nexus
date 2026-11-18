CREATE OR REPLACE FUNCTION dml_utils.set_migration_run_threads(
    i_label dml_utils_data.non_null_text,
    i_threads dml_utils_data.positive_integer
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
BEGIN
    -- Only an unfinished run can have its thread count adjusted: a completed
    -- run's chunks are already processed.
    --
    -- The row is not locked for the whole of a run_migration_chunks call (that
    -- holds no lock on the run row while processing), so a coordinator that is
    -- already running keeps the thread count it read at its start: this update
    -- affects the *next* run_migration_chunks call, not one in progress. That is
    -- the intended contract (adjust between runs), not a race to fix.
    UPDATE dml_utils_data.migration_run
    SET threads = i_threads
    WHERE label = i_label
      AND archived_at IS NULL
      AND completed_at IS NULL
      AND cancelled_at IS NULL;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'no unfinished migration run for label %', i_label
            USING ERRCODE = 'P0002';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils.set_migration_run_threads IS
    'Replaces the recorded threads of the unfinished run for the label, so the '
        'next run_migration_chunks call uses the adjusted worker count. A '
        'coordinator already processing the run keeps the count it read at its '
        'start, so adjust between runs.';
