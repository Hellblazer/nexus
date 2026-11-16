CREATE OR REPLACE FUNCTION dml_utils_lib.migration_run_calculation_lock_key(
    i_run_id bigint
)
    RETURNS bigint
    LANGUAGE sql
    STABLE
    SECURITY INVOKER
AS
$$
    -- A single bigint advisory-lock key, derived from the run id. The prefix keeps
    -- the namespace distinct from any other advisory lock a caller may take.
SELECT pg_catalog.hashtextextended(
               pg_catalog.format('dml_utils.migration_run.calculation.%s', i_run_id), 0);
$$;

COMMENT ON FUNCTION dml_utils_lib.migration_run_calculation_lock_key(bigint) IS
    'Returns the advisory-lock key used to serialize partition-wise boundary '
        'calculation for a run. STABLE because pg_catalog.format is not immutable.';
