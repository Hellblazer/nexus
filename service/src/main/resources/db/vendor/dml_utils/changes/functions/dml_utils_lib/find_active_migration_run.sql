CREATE OR REPLACE FUNCTION dml_utils_lib.find_active_migration_run(
    i_label dml_utils_data.non_null_text
)
    RETURNS SETOF dml_utils_data.migration_run
    LANGUAGE sql
    STABLE
    SECURITY INVOKER
AS
$$
SELECT run.run_id,
       run.label,
       run.sql_text,
       run.chunk_size,
       run.chunk_by,
       run.partition_wise,
       run.threads,
       run.sleep_ms,
       run.driving_table_schema_name,
       run.driving_table_name,
       run.driving_table_alias,
       run.driving_table_relation_filepath,
       run.ordered_key_index,
       run.ordered_key_columns,
       run.ordered_key_kinds,
       run.created_at,
       run.updated_at,
       run.started_at,
       run.boundaries_calculated_at,
       run.total_chunks,
       run.total_chunks_completed,
       run.completed_at,
       run.cancelled_at,
       run.archived_at
FROM dml_utils_data.migration_run AS run
WHERE run.label = i_label
  AND run.archived_at IS NULL;
$$;

COMMENT ON FUNCTION dml_utils_lib.find_active_migration_run(
    dml_utils_data.non_null_text
    ) IS
    'Returns the active (not archived) run for a label, or no row. Used by '
        'resolve_migration_run to decide between resume and creation.';
