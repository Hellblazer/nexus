CREATE OR REPLACE FUNCTION dml_utils_lib.complete_migration_run(
    i_run_id bigint
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
BEGIN
    -- The coordinator calls this after every boundary is claimed. It is
    -- idempotent and leaves a cancelled run unchanged.
    UPDATE dml_utils_data.migration_run
    SET completed_at = pg_catalog.clock_timestamp()
    WHERE run_id = i_run_id
      AND completed_at IS NULL
      AND cancelled_at IS NULL
      AND total_chunks_completed >= total_chunks;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.complete_migration_run IS
    'Marks a run complete only when completed chunks cover total_chunks. '
        'SECURITY DEFINER so the invoker-owned coordinator does not need metadata '
        'write privileges.';
