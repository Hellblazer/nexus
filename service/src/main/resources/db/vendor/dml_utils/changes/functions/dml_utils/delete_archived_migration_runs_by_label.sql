CREATE OR REPLACE FUNCTION dml_utils.delete_archived_migration_runs(
    i_label dml_utils_data.non_null_text
)
    RETURNS bigint
    LANGUAGE sql
    VOLATILE
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
WITH deleted AS (
    DELETE FROM dml_utils_data.migration_run
        WHERE archived_at IS NOT NULL
            AND (completed_at IS NOT NULL OR cancelled_at IS NOT NULL)
            AND label = i_label
        RETURNING 1)
SELECT pg_catalog.count(*)::bigint
FROM deleted;
$$;

COMMENT ON FUNCTION dml_utils.delete_archived_migration_runs(dml_utils_data.non_null_text) IS
    'Deletes every terminal (completed or cancelled) archived migration run for '
        'the label with its boundaries and errors, returning the number of runs '
        'deleted. A non-terminal archived run is left in place; '
        'archive_migration_run cancels unfinished runs, so this is normally '
        'unreachable.';
