CREATE OR REPLACE FUNCTION dml_utils.set_migration_run_sql_text(
    i_label dml_utils_data.non_null_text,
    i_sql_text dml_utils_data.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
BEGIN
    -- Fail fast on a malformed template: the next run_migration_chunks call
    -- renders the stored SQL, so reject one it could not use.
    PERFORM dml_utils_lib.assert_chunking_template(i_sql_text => i_sql_text);

    -- Only an unfinished run can have its SQL adjusted: a completed run's
    -- boundaries are already processed and must not be redefined.
    UPDATE dml_utils_data.migration_run
    SET sql_text = i_sql_text
    WHERE label = i_label
      AND archived_at IS NULL
      AND completed_at IS NULL
      AND cancelled_at IS NULL;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'no unfinished migration run for label %', i_label
            USING ERRCODE = 'P0002';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils.set_migration_run_sql_text IS
    'Replaces the recorded sql_text of the unfinished run for the label, so the '
        'next run_migration_chunks call uses the adjusted SQL. A run already being '
        'processed keeps the SQL it read at its start, so adjust the SQL between '
        'runs rather than while a coordinator is processing.';
