CREATE OR REPLACE FUNCTION dml_utils_lib.process_migration_chunk(
    i_run_id bigint,
    i_boundary_no bigint,
    i_sql_text dml_utils_data.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
DECLARE
    l_started_at timestamptz;
BEGIN
    -- Claim the boundary through the owner-owned helper. The helper locks the
    -- row with SELECT ... FOR UPDATE and retains that lock until this caller's
    -- transaction ends; a concurrent worker blocks and then finds completed_at
    -- set under READ COMMITTED. This routine stays SECURITY INVOKER so the chunk
    -- SQL below runs as the caller; the helper is the only metadata writer.
    l_started_at := dml_utils_lib.claim_migration_boundary(
            i_run_id => i_run_id,
            i_boundary_no => i_boundary_no);

    -- The chunk SQL is fully formed by dml_utils_lib.render_chunk_sql from
    -- validated identifiers and literal ids; this is the intended dynamic-SQL
    -- boundary for running one chunk. Because this function is SECURITY INVOKER,
    -- the statement runs with the caller's privileges and can do nothing the
    -- caller could not do by running it directly. If it fails, the worker
    -- transaction aborts and the claim rolls back, leaving the boundary
    -- unclaimed for a retry.
    EXECUTE i_sql_text;

    -- Complete the boundary through the owner-owned helper. The row lock from
    -- claim_migration_boundary is still held, so no other transaction can
    -- interleave; the helper writes both timestamps and advances the run counter
    -- in this same transaction.
    PERFORM dml_utils_lib.complete_migration_boundary(
            i_run_id => i_run_id,
            i_boundary_no => i_boundary_no,
            i_started_at => l_started_at);
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.process_migration_chunk IS
    'Claims one migration boundary through the owner-owned claim helper, runs its '
        'chunk SQL as the caller, then completes the boundary through the '
        'owner-owned completion helper; raises P0C01 when the boundary is missing, '
        'already completed or cancelled. The routine is SECURITY INVOKER so the '
        'chunk SQL runs with the invoker''s privileges; metadata writes are '
        'delegated to SECURITY DEFINER helpers.';
