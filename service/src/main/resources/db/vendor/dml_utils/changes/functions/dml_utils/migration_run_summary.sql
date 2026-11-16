CREATE OR REPLACE FUNCTION dml_utils.migration_run_summary(
    i_label dml_utils_data.non_null_text
)
    RETURNS TABLE
            (
                run_id                          bigint,
                label                           text,
                chunk_size                      integer,
                chunk_by                        text,
                threads                         integer,
                sleep_ms                        integer,
                driving_table_schema_name       text,
                driving_table_name              text,
                driving_table_relation_filepath text,
                created_at                      timestamptz,
                started_at                      timestamptz,
                boundaries_calculated_at        timestamptz,
                completed_at                    timestamptz,
                cancelled_at                    timestamptz,
                archived_at                     timestamptz,
                boundary_count                  bigint,
                completed_boundary_count        bigint,
                error_count                     bigint
            )
    LANGUAGE sql
    STABLE
    SECURITY INVOKER
AS
$$
SELECT r.run_id,
       r.label,
       r.chunk_size,
       r.chunk_by::text,
       r.threads,
       r.sleep_ms,
       r.driving_table_schema_name::text,
       r.driving_table_name::text,
       r.driving_table_relation_filepath,
       r.created_at,
       r.started_at,
       r.boundaries_calculated_at,
       r.completed_at,
       r.cancelled_at,
       r.archived_at,
       pg_catalog.count(b.boundary_no)     AS boundary_count,
       pg_catalog.count(b.completed_at)    AS completed_boundary_count,
       (SELECT pg_catalog.count(*)
        FROM dml_utils_data.migration_error AS e
        WHERE e.run_id = r.run_id)::bigint AS error_count
FROM dml_utils_data.migration_run AS r
         LEFT JOIN dml_utils_data.migration_boundary AS b ON b.run_id = r.run_id
WHERE r.label = i_label
GROUP BY r.run_id
ORDER BY r.run_id;
$$;

COMMENT ON FUNCTION dml_utils.migration_run_summary(dml_utils_data.non_null_text) IS
    'Returns one high-level row per run for the label, with its boundary and error counts.';
