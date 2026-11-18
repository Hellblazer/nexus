CREATE OR REPLACE FUNCTION dml_utils_lib.record_migration_worker_failure(
    i_run_id bigint,
    i_boundary_no bigint,
    i_sqlstate text,
    i_error_message text
)
    RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SECURITY INVOKER
AS
$$
BEGIN
    -- Record the failure in its own worker/transaction, so the row commits
    -- autonomously and survives the coordinator's re-raise. Best effort: if the
    -- recording worker cannot be launched, the original worker error is still
    -- raised by the caller.
    BEGIN
        PERFORM public.pg_background_run(pg_catalog.format(
                'SELECT dml_utils_lib.record_migration_error(%s, %s, %L, %L)',
                i_run_id, i_boundary_no, i_sqlstate, i_error_message));
    EXCEPTION
        WHEN OTHERS THEN
            NULL;
    END;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.record_migration_worker_failure(
    bigint,
    bigint,
    text,
    text
    ) IS
    'Records a failed chunk in dml_utils_data.migration_error through an '
        'autonomous pg_background transaction, swallowing a recording failure so '
        'the caller can still re-raise the original worker error.';
