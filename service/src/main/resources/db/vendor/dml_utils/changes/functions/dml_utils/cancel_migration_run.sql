CREATE OR REPLACE FUNCTION dml_utils.cancel_migration_run(
    i_label dml_utils_data.non_null_text
)
    RETURNS bigint
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
DECLARE
    l_run_id bigint;
BEGIN
    -- Only an active, unfinished, not-yet-cancelled run can be cancelled. The
    -- completed/cancelled guard is the same one the coordinator's completion
    -- update uses, so a race has exactly one winner.
    UPDATE dml_utils_data.migration_run
    SET cancelled_at = pg_catalog.clock_timestamp()
    WHERE label = i_label
      AND archived_at IS NULL
      AND completed_at IS NULL
      AND cancelled_at IS NULL
    RETURNING run_id
        INTO l_run_id;

    IF l_run_id IS NULL THEN
        RETURN NULL;
    END IF;

    -- Mark the pending boundaries no worker has claimed. A claimed boundary is
    -- locked by its worker's SELECT ... FOR UPDATE; SKIP LOCKED leaves those for
    -- the coordinator to mark after it cancels the worker. Boundaries not yet
    -- launched are marked here, so the cancellation is recorded even when no
    -- coordinator is alive to observe it.
    WITH pending AS (SELECT run_id, boundary_no
                     FROM dml_utils_data.migration_boundary
                     WHERE run_id = l_run_id
                       AND completed_at IS NULL
                       AND cancelled_at IS NULL
                         FOR UPDATE SKIP LOCKED)
    UPDATE dml_utils_data.migration_boundary AS b
    SET cancelled_at = pg_catalog.clock_timestamp()
    FROM pending AS p
    WHERE b.run_id = p.run_id
      AND b.boundary_no = p.boundary_no;

    RETURN l_run_id;
END;
$$;

COMMENT ON FUNCTION dml_utils.cancel_migration_run IS
    'Cancels the active (not archived, unfinished) migration run for the label, '
        'if any: sets its cancelled_at and marks its unclaimed pending boundaries '
        'cancelled, leaving boundaries held by an in-flight worker for the '
        'coordinator to mark after it cancels them. Returns the cancelled run_id, '
        'or NULL when there was no active unfinished run. A cancelled run is '
        'terminal; archive it and create a new run to resume.';
