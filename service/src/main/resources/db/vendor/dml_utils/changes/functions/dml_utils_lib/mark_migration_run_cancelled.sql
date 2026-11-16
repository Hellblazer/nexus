CREATE OR REPLACE FUNCTION dml_utils_lib.mark_migration_run_cancelled(
    i_run_id bigint
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
BEGIN
    -- Lock pending boundaries first, then the run row. This matches the chunk
    -- worker's acquisition order (claim locks the boundary, completion then
    -- updates the run counter) and cannot deadlock against an in-flight worker.
    PERFORM 1
    FROM dml_utils_data.migration_boundary
    WHERE run_id = i_run_id
      AND completed_at IS NULL
      AND cancelled_at IS NULL
        FOR UPDATE;

    PERFORM 1
    FROM dml_utils_data.migration_run
    WHERE run_id = i_run_id
        FOR UPDATE;

    -- completed_at wins for a chunk that finished before cancellation reached
    -- it. A completed run is left untouched, so it cannot acquire cancelled
    -- boundaries after the fact.
    IF NOT EXISTS (SELECT 1
                   FROM dml_utils_data.migration_run AS r
                   WHERE r.run_id = i_run_id
                     AND r.completed_at IS NULL)
    THEN
        RETURN;
    END IF;

    UPDATE dml_utils_data.migration_boundary
    SET cancelled_at = COALESCE(cancelled_at, pg_catalog.clock_timestamp())
    WHERE run_id = i_run_id
      AND completed_at IS NULL
      AND cancelled_at IS NULL;

    UPDATE dml_utils_data.migration_run
    SET cancelled_at = COALESCE(cancelled_at, pg_catalog.clock_timestamp())
    WHERE run_id = i_run_id
      AND completed_at IS NULL;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.mark_migration_run_cancelled IS
    'Marks a run and its unfinished, uncompleted boundaries cancelled. Locks '
        'pending boundaries before the run row to match the worker lock order, and '
        'leaves an already-completed run untouched. SECURITY DEFINER so the '
        'invoker-owned coordinator does not need metadata write privileges; callers '
        'run it in an autonomous background worker.';
