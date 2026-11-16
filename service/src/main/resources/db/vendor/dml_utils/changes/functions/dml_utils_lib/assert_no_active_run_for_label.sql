CREATE OR REPLACE FUNCTION dml_utils_lib.assert_no_active_run_for_label(
    i_label dml_utils_data.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
BEGIN
    IF EXISTS (SELECT 1
               FROM dml_utils_data.migration_run
               WHERE label = i_label
                 AND archived_at IS NULL)
    THEN
        RAISE EXCEPTION 'an active run for label % already exists', i_label
            USING ERRCODE = '23505';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_no_active_run_for_label IS
    'Raises unique_violation (23505) when a not-archived migration run already '
        'exists for the label.';
