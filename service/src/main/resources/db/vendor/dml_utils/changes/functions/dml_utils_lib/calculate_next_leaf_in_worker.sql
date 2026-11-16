CREATE OR REPLACE FUNCTION dml_utils_lib.calculate_next_leaf_in_worker(
    i_run_id bigint
)
    RETURNS integer
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
DECLARE
    l_handle       public.pg_background_handle;
    l_ordinal      integer;
    l_leaf_ordinal integer;
    l_info         public.pg_background_result_info;
    l_error        public.pg_background_error;
BEGIN
    -- One leaf per worker transaction: calculate_migration_leaf claims the first
    -- pending/failed leaf, appends its starts and marks it calculated. A NULL
    -- result means there is no leaf left to calculate. The launch itself is
    -- outside the handler: an infrastructure failure there did not attempt the
    -- leaf, so it must not mark that leaf failed.
    l_handle := public.pg_background_launch(pg_catalog.format(
            'SELECT dml_utils_lib.calculate_migration_leaf(%s) AS ordinal',
            i_run_id));

    BEGIN
        PERFORM public.pg_background_wait(l_handle.pid, l_handle.cookie);
    EXCEPTION
        WHEN OTHERS THEN
            -- The wait failed while the worker may still be running, so its leaf
            -- transaction has not necessarily rolled back. Do not attribute the
            -- failure to a leaf; cancel/detach best-effort so the handle is not
            -- leaked, then re-raise.
            BEGIN
                PERFORM public.pg_background_cancel(
                        l_handle.pid, l_handle.cookie, 1000);
            EXCEPTION
                WHEN OTHERS THEN
                    NULL;
            END;
            BEGIN
                PERFORM public.pg_background_detach(
                        l_handle.pid, l_handle.cookie);
            EXCEPTION
                WHEN OTHERS THEN
                    NULL;
            END;
            RAISE;
    END;

    -- Inspect the worker's structured outcome before consuming the result. Only
    -- a worker-reported error is attributed to a leaf; a result-materialisation
    -- failure after a successful commit is not, and must not mark the next
    -- pending leaf failed.
    BEGIN
        l_info := public.pg_background_result_info(
                l_handle.pid, l_handle.cookie);
    EXCEPTION
        WHEN OTHERS THEN
            BEGIN
                PERFORM public.pg_background_detach(
                        l_handle.pid, l_handle.cookie);
            EXCEPTION
                WHEN OTHERS THEN
                    NULL;
            END;
            RAISE;
    END;

    IF COALESCE(l_info.has_error, false) THEN
        BEGIN
            l_error := public.pg_background_error_info(
                    l_handle.pid, l_handle.cookie);
        EXCEPTION
            WHEN OTHERS THEN
                l_error := NULL;
        END;

        -- The failed leaf transaction rolled back its claim, so the first
        -- non-calculated leaf is the one it claimed. Record the error on the
        -- manifest in its own worker transaction before re-raising.
        SELECT leaf.leaf_ordinal
        INTO l_leaf_ordinal
        FROM dml_utils_data.migration_run_leaf AS leaf
        WHERE leaf.run_id = i_run_id
          AND leaf.state <> 'calculated'
        ORDER BY leaf.leaf_ordinal
        LIMIT 1;
        IF FOUND THEN
            BEGIN
                PERFORM public.pg_background_run(pg_catalog.format(
                        'SELECT dml_utils_lib.record_migration_leaf_error(%s, %s, %L, %L)',
                        i_run_id, l_leaf_ordinal,
                        COALESCE(l_error.sqlstate, 'XX000'),
                        COALESCE(l_error.message, 'worker failed')));
            EXCEPTION
                WHEN OTHERS THEN
                    NULL;
            END;
        END IF;
    END IF;

    -- Consume the result. On the error path this re-raises the worker's SQLSTATE;
    -- detach best-effort if it does.
    BEGIN
        SELECT ordinal
        INTO l_ordinal
        FROM public.pg_background_result(
                     l_handle.pid, l_handle.cookie) AS (ordinal integer);
    EXCEPTION
        WHEN OTHERS THEN
            BEGIN
                PERFORM public.pg_background_detach(
                        l_handle.pid, l_handle.cookie);
            EXCEPTION
                WHEN OTHERS THEN
                    NULL;
            END;
            RAISE;
    END;

    RETURN l_ordinal;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.calculate_next_leaf_in_worker(bigint) IS
    'Runs one calculate_migration_leaf step in its own pg_background '
        'transaction and returns the calculated ordinal (NULL when no leaf is '
        'left). A worker-reported failure is recorded on the manifest in its own '
        'worker transaction before the worker error is re-raised; a wait or '
        'result-materialisation failure cancels/detaches the handle without '
        'blaming an unattempted leaf.';
