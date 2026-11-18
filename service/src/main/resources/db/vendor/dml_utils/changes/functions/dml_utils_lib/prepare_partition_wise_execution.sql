CREATE OR REPLACE FUNCTION dml_utils_lib.prepare_partition_wise_execution(
    i_run_id bigint,
    i_label dml_utils_data.non_null_text
)
    RETURNS boolean
    LANGUAGE plpgsql
    VOLATILE
    SECURITY INVOKER
AS
$$
DECLARE
    l_boundaries_calculated_at timestamptz;
    l_cancelled_at             timestamptz;
BEGIN
    SELECT run.boundaries_calculated_at
    INTO l_boundaries_calculated_at
    FROM dml_utils_data.migration_run AS run
    WHERE run.run_id = i_run_id;

    IF l_boundaries_calculated_at IS NULL THEN
        PERFORM dml_utils_lib.calculate_migration_run_boundaries(i_run_id => i_run_id);

        SELECT run.cancelled_at, run.boundaries_calculated_at
        INTO l_cancelled_at, l_boundaries_calculated_at
        FROM dml_utils_data.migration_run AS run
        WHERE run.run_id = i_run_id;

        IF l_cancelled_at IS NOT NULL THEN
            -- A leaf worker that committed after the canceller ran leaves its
            -- boundaries pending; mark them cancelled in an autonomous worker,
            -- exactly as the processing-phase cancellation handler does.
            BEGIN
                PERFORM public.pg_background_run(pg_catalog.format(
                        'SELECT dml_utils_lib.mark_migration_run_cancelled(%s)',
                        i_run_id));
            EXCEPTION
                WHEN OTHERS THEN
                    RAISE WARNING 'could not mark the boundaries of cancelled run % cancelled: %',
                        i_run_id, SQLERRM;
            END;
            RAISE NOTICE 'run for label % was cancelled during boundary calculation', i_label;
            RETURN false;
        END IF;
        IF l_boundaries_calculated_at IS NULL THEN
            RAISE EXCEPTION 'run % boundary calculation did not finish', i_run_id
                USING ERRCODE = '55000';
        END IF;
    END IF;

    -- The live tree is compared with the manifest before processing, so a
    -- detach, attach, rename or heap rewrite fails closed instead of
    -- misdirecting chunks.
    PERFORM dml_utils_lib.assert_migration_tree_unchanged(i_run_id => i_run_id);
    RETURN true;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.prepare_partition_wise_execution(
    bigint,
    dml_utils_data.non_null_text
    ) IS
    'Prepares a partition-wise run for processing: finishes any pending one-leaf-'
        'per-transaction boundary calculation, returns false after marking a run '
        'that was cancelled during calculation, and compares the live tree with '
        'the manifest. A calculation that neither finishes nor cancels raises '
        '55000. Returns true when the run is ready to process.';
