CREATE OR REPLACE FUNCTION dml_utils.set_migration_run_sleep_ms(
    i_label dml_utils_data.non_null_text,
    i_sleep_ms dml_utils_data.non_negative_integer
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
BEGIN
    -- Only an unfinished run can have its throttle adjusted. Like
    -- set_migration_run_threads, a coordinator that is already processing reads
    -- the value at its start; this update affects the next run_migration_chunks
    -- call (a restart/resume), not one in progress.
    UPDATE dml_utils_data.migration_run
    SET sleep_ms = i_sleep_ms
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

COMMENT ON FUNCTION dml_utils.set_migration_run_sleep_ms IS
    'Replaces the recorded per-chunk sleep of the unfinished run for the label, '
        'so the next run_migration_chunks call delays after each successful chunk '
        'by that many milliseconds. A coordinator already processing the run keeps '
        'the value it read at its start, so adjust between calls.';
