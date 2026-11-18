CREATE OR REPLACE FUNCTION dml_utils_lib.stop_migration_workers(
    i_pids integer[],
    i_cookies bigint[],
    i_cancel boolean,
    i_wait_ms integer
)
    RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SECURITY INVOKER
AS
$$
DECLARE
    l_cancel_failed boolean[] := ARRAY []::boolean[];
    l_finished      boolean;
BEGIN
    IF i_pids IS NULL OR pg_catalog.cardinality(i_pids) = 0 THEN
        RETURN;
    END IF;

    -- The pid/cookie arrays are parallel handles; a missing or short cookie
    -- array would make every cancel/wait/detach a silent no-op.
    IF i_cookies IS NULL
        OR pg_catalog.cardinality(i_cookies) <> pg_catalog.cardinality(i_pids)
    THEN
        RAISE EXCEPTION 'i_pids and i_cookies must be non-null arrays of equal length'
            USING ERRCODE = '22023';
    END IF;

    -- 0 means wait forever to pg_background_wait, which this helper uses only
    -- for a NULL i_wait_ms; reject it so the two spellings cannot be confused.
    IF i_wait_ms IS NOT NULL AND i_wait_ms < 1 THEN
        RAISE EXCEPTION 'i_wait_ms must be positive or NULL, not %', i_wait_ms
            USING ERRCODE = '22023';
    END IF;

    -- Cancel every worker first, so a long-running chunk does not delay the
    -- cancellation of the ones behind it. A worker that already finished is a
    -- no-op for cancel; the wait/detach below reaps it.
    IF i_cancel THEN
        FOR l_index IN 1..pg_catalog.cardinality(i_pids)
            LOOP
                BEGIN
                    PERFORM public.pg_background_cancel(
                            i_pids[l_index], i_cookies[l_index], 1000);
                    l_cancel_failed[l_index] := false;
                EXCEPTION
                    WHEN OTHERS THEN
                        l_cancel_failed[l_index] := true;
                        RAISE WARNING 'stop_migration_workers: cancel failed for pid=% cookie=%: %',
                            i_pids[l_index], i_cookies[l_index], SQLERRM;
                END;
            END LOOP;
    END IF;

    -- Wait for each worker to stop and release its handle. A NULL wait timeout
    -- waits indefinitely, unless the cancel above failed; in that case use a
    -- bounded wait so a wedged worker cannot hang the coordinator forever. A
    -- cancelled chunk rolls back, leaving its boundary unclaimed.
    FOR l_index IN 1..pg_catalog.cardinality(i_pids)
        LOOP
            BEGIN
                IF i_wait_ms IS NULL
                    AND NOT COALESCE(l_cancel_failed[l_index], false)
                THEN
                    l_finished := public.pg_background_wait(
                            i_pids[l_index], i_cookies[l_index]);
                ELSE
                    l_finished := public.pg_background_wait(
                            i_pids[l_index], i_cookies[l_index],
                            COALESCE(i_wait_ms, 5000));
                END IF;
                IF NOT l_finished THEN
                    RAISE WARNING 'stop_migration_workers: pid=% cookie=% did not stop in time',
                        i_pids[l_index], i_cookies[l_index];
                END IF;
            EXCEPTION
                WHEN OTHERS THEN
                    RAISE WARNING 'stop_migration_workers: wait failed for pid=% cookie=%: %',
                        i_pids[l_index], i_cookies[l_index], SQLERRM;
            END;

            -- Detach in its own block: a failed wait must not leak the handle.
            BEGIN
                PERFORM public.pg_background_detach(
                        i_pids[l_index], i_cookies[l_index]);
            EXCEPTION
                WHEN OTHERS THEN
                    RAISE WARNING 'stop_migration_workers: detach failed for pid=% cookie=%: %',
                        i_pids[l_index], i_cookies[l_index], SQLERRM;
            END;
        END LOOP;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.stop_migration_workers(
    integer[],
    bigint[],
    boolean,
    integer
    ) IS
    'Cancels (when requested) and waits for/detaches a set of pg_background '
        'worker handles, given as parallel non-null pid/cookie arrays of equal '
        'length. A NULL wait timeout waits indefinitely unless cancellation failed, '
        'in which case a bounded wait is used. A positive timeout is required when '
        'not NULL. Per-worker errors are reported as warnings so the coordinator '
        'can finish its own error path without leaking a handle.';
