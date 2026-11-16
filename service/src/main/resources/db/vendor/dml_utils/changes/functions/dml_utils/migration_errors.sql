CREATE OR REPLACE FUNCTION dml_utils.migration_errors(
    i_run_id bigint
)
    RETURNS TABLE
            (
                error_id    bigint,
                boundary_no bigint,
                sqlstate    text,
                message     text,
                created_at  timestamptz
            )
    LANGUAGE sql
    STABLE
    SECURITY INVOKER
AS
$$
SELECT e.error_id, e.boundary_no, e.sqlstate, e.message, e.created_at
FROM dml_utils_data.migration_error AS e
WHERE e.run_id = i_run_id
ORDER BY e.error_id;
$$;

COMMENT ON FUNCTION dml_utils.migration_errors(bigint) IS
    'Returns the recorded errors of the run, ordered by error id.';
