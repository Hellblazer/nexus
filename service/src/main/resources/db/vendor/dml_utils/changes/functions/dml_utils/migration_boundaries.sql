CREATE OR REPLACE FUNCTION dml_utils.migration_boundaries(
    i_run_id bigint
)
    RETURNS TABLE
            (
                boundary_no  bigint,
                boundary_id  dml_utils_data.migration_key,
                started_at   timestamptz,
                completed_at timestamptz
            )
    LANGUAGE sql
    STABLE
    SECURITY INVOKER
AS
$$
SELECT b.boundary_no, b.boundary_id, b.started_at, b.completed_at
FROM dml_utils_data.migration_boundary AS b
WHERE b.run_id = i_run_id
ORDER BY b.boundary_no;
$$;

COMMENT ON FUNCTION dml_utils.migration_boundaries(bigint) IS
    'Returns the run''s chunk boundaries in order, with started_at and '
        'completed_at NULL for chunks not yet started or not yet finished.';
