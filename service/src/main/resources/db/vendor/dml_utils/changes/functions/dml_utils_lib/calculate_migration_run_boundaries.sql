CREATE OR REPLACE FUNCTION dml_utils_lib.calculate_migration_run_boundaries(
    i_run_id bigint
)
    RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SECURITY INVOKER
AS
$$
DECLARE
    l_partition_wise boolean;
    l_cancelled_at   timestamptz;
    l_calculated_at  timestamptz;
    l_lock_key       bigint;
    l_ordinal        integer;
BEGIN
    IF i_run_id IS NULL THEN
        RAISE EXCEPTION 'run id must not be NULL'
            USING ERRCODE = '22023';
    END IF;

    SELECT run.partition_wise, run.cancelled_at
    INTO l_partition_wise, l_cancelled_at
    FROM dml_utils_data.migration_run AS run
    WHERE run.run_id = i_run_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'migration run % does not exist', i_run_id
            USING ERRCODE = 'P0002';
    END IF;
    IF NOT l_partition_wise THEN
        RAISE EXCEPTION 'run % is not partition-wise', i_run_id
            USING ERRCODE = '22023';
    END IF;
    -- A cancelled run is terminal: do not take the lock or compare the tree.
    IF l_cancelled_at IS NOT NULL THEN
        RETURN;
    END IF;

    SELECT run.boundaries_calculated_at
    INTO l_calculated_at
    FROM dml_utils_data.migration_run AS run
    WHERE run.run_id = i_run_id;
    IF l_calculated_at IS NOT NULL THEN
        RETURN;
    END IF;

    -- Serialize calculation for the run. The transaction-scoped advisory lock
    -- releases on both success and failure; a second coordinator that cannot take
    -- it fails closed instead of appending duplicate boundaries.
    l_lock_key := dml_utils_lib.migration_run_calculation_lock_key(i_run_id => i_run_id);
    -- IS NOT TRUE fails closed when the helper returns NULL, unlike NOT <NULL>.
    IF pg_catalog.pg_try_advisory_xact_lock(l_lock_key) IS NOT TRUE THEN
        RAISE EXCEPTION 'boundary calculation for run % is already in progress', i_run_id
            USING ERRCODE = '55000';
    END IF;

    -- Re-check terminal state under the lock. A coordinator that finished
    -- between the read above and the lock must not reach the tree assertion,
    -- which would report a misleading 'tree changed' error for a complete run.
    SELECT run.cancelled_at, run.boundaries_calculated_at
    INTO l_cancelled_at, l_calculated_at
    FROM dml_utils_data.migration_run AS run
    WHERE run.run_id = i_run_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'migration run % does not exist', i_run_id
            USING ERRCODE = 'P0002';
    END IF;
    IF l_cancelled_at IS NOT NULL OR l_calculated_at IS NOT NULL THEN
        RETURN;
    END IF;

    -- Compare the live tree with the manifest once before starting. Each leaf is
    -- re-checked when it is claimed, and the tree is compared again before the
    -- terminal boundary, so a change cannot slip between leaves unnoticed.
    PERFORM dml_utils_lib.assert_migration_tree_unchanged(i_run_id => i_run_id);

    LOOP
        SELECT run.cancelled_at, run.boundaries_calculated_at
        INTO l_cancelled_at, l_calculated_at
        FROM dml_utils_data.migration_run AS run
        WHERE run.run_id = i_run_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'migration run % does not exist', i_run_id
                USING ERRCODE = 'P0002';
        END IF;

        EXIT WHEN l_cancelled_at IS NOT NULL OR l_calculated_at IS NOT NULL;

        l_ordinal := dml_utils_lib.calculate_next_leaf_in_worker(i_run_id => i_run_id);
        EXIT WHEN l_ordinal IS NULL;
    END LOOP;

    -- Re-checks the run state and the tree, then finalizes in its own worker.
    PERFORM dml_utils_lib.finalize_calculated_run(i_run_id => i_run_id);
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.calculate_migration_run_boundaries(bigint) IS
    'Calculates a partition-wise run''s boundaries one leaf per pg_background '
        'transaction until every leaf is calculated, then finalizes the run in its '
        'own worker transaction. Serialized by a transaction-scoped advisory lock '
        'keyed by the run id, so a concurrent calculation fails closed with '
        'object_not_in_prerequisite_state (55000) instead of duplicating boundaries. '
        'The live tree is compared with the manifest before the loop and before '
        'finalization, and the claimed leaf is re-checked by '
        'claim_next_migration_leaf. A failing leaf is recorded on the manifest '
        '(state = failed, SQLSTATE and message) in its own worker transaction '
        'before the error is re-raised. A cancelled run stops without finalizing.';
