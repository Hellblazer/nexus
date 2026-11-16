CREATE OR REPLACE FUNCTION dml_utils_lib.record_migration_leaf_error(
    i_run_id bigint,
    i_leaf_ordinal integer,
    i_sqlstate dml_utils_data.non_null_text,
    i_error_message dml_utils_data.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
BEGIN
    -- The failed worker's transaction rolled back with its claim, so the leaf is
    -- still pending. Record the failure in this autonomous transaction (the
    -- coordinator calls it through pg_background) so an operator can see which
    -- leaf failed and why; a calculated leaf is never downgraded. A missing leaf is
    -- a no-op for the same reason: the coordinator's own error is what matters.
    UPDATE dml_utils_data.migration_run_leaf
    SET state                = 'failed',
        boundary_count       = 0,
        terminal_boundary_id = NULL,
        calculated_at        = NULL,
        error_sqlstate       = i_sqlstate,
        error_message        = i_error_message
    WHERE run_id = i_run_id
      AND leaf_ordinal = i_leaf_ordinal
      AND state <> 'calculated';
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.record_migration_leaf_error(
    bigint,
    integer,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) IS
    'Marks a partition-wise leaf failed with the worker''s SQLSTATE and message, '
        'so a resumed run can start at that leaf and an operator can see what '
        'failed. A calculated leaf is left untouched and a missing leaf is a no-op. '
        'SECURITY DEFINER so the invoker-owned coordinator can write the manifest '
        'in its own worker transaction.';
