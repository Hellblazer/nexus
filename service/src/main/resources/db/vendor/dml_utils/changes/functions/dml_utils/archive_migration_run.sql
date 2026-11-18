CREATE OR REPLACE FUNCTION dml_utils.archive_migration_run(
    i_label dml_utils_data.non_null_text
)
    RETURNS bigint
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
DECLARE
    l_run_id bigint;
BEGIN
    -- An archived run is terminal, so cancel an unfinished one first. This also
    -- marks its unclaimed boundaries, and makes the delete helper's terminal
    -- filter an invariant rather than a way to strand abandoned runs forever.
    PERFORM dml_utils.cancel_migration_run(i_label => i_label);

    UPDATE dml_utils_data.migration_run
    SET archived_at = pg_catalog.now()
    WHERE label = i_label
      AND archived_at IS NULL
    RETURNING run_id
        INTO l_run_id;

    RETURN l_run_id;
END;
$$;

COMMENT ON FUNCTION dml_utils.archive_migration_run IS
    'Archives the active (not archived) migration run for the label, if any, so '
        'the label can be reused; an unfinished run is cancelled first, so every '
        'archived run is terminal. Returns the archived run_id, or NULL when '
        'there was no active run.';
