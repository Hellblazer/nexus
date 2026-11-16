CREATE OR REPLACE FUNCTION dml_utils_lib.record_migration_error(
    i_run_id bigint,
    i_boundary_no bigint,
    i_sqlstate dml_utils_data.non_null_text,
    i_message dml_utils_data.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
BEGIN
    INSERT INTO dml_utils_data.migration_error (run_id, boundary_no, sqlstate, message)
    VALUES (i_run_id, i_boundary_no, i_sqlstate, i_message);
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.record_migration_error IS
    'Records one failed chunk worker for a run and boundary; intended to run in a '
        'pg_background worker so the row commits autonomously.';
