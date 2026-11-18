CREATE OR REPLACE FUNCTION dml_utils.set_migration_run_function(
    i_label dml_utils_data.non_null_text,
    i_function_schema_name dml_utils_data.non_null_text,
    i_function_name dml_utils_data.non_null_text,
    -- Plain text, not non_null_text: an omitted filter is NULL and means no
    -- filter, so any filter currently on the run is removed. non_null_text would
    -- reject the DEFAULT NULL before the body.
    i_filter text DEFAULT NULL
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
DECLARE
    l_driving_table_alias     text;
    l_function_chunk_template text;
BEGIN
    -- The run's driving table (immutable) decides which primary-key signature the
    -- function must match. Read it from the stored run and build the template in
    -- one statement, so the domain-typed columns are passed straight to the
    -- non_null_text arguments with no hidden domain-to-text cast into a local.
    -- With no matching run the function is never evaluated and NOT FOUND below
    -- raises P0002.
    --
    -- FOR UPDATE locks the selected run for the rest of the transaction, so the
    -- set_migration_run_sql_text call below cannot be retargeted by a concurrent
    -- session that archives this run and creates a new one for the same label:
    -- archiving this row blocks on the lock until this transaction ends.
    SELECT driving_table_alias::text,
           dml_utils_lib.build_function_chunk_template(
                   i_table_schema_name => driving_table_schema_name,
                   i_table_name => driving_table_name,
                   i_function_schema_name => i_function_schema_name,
                   i_function_name => i_function_name,
                   i_filter => i_filter)
    INTO l_driving_table_alias, l_function_chunk_template
    FROM dml_utils_data.migration_run
    WHERE label = i_label
      AND archived_at IS NULL
      AND completed_at IS NULL
      AND cancelled_at IS NULL
        FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'no unfinished migration run for label %', i_label
            USING ERRCODE = 'P0002';
    END IF;

    -- The generated template always references the engine's default alias t,
    -- while render_prepared_chunk_sql uses the run's stored alias. A run created
    -- with a non-default alias would therefore persist a template that cannot
    -- resolve its driving table.
    IF l_driving_table_alias <> 't' THEN
        RAISE EXCEPTION 'set_migration_run_function requires the default driving-table alias t, but run % uses %',
            i_label, l_driving_table_alias
            USING ERRCODE = '0A000';
    END IF;

    -- Reuse the base mutator: it validates the template and only updates an
    -- unfinished, unarchived run.
    PERFORM dml_utils.set_migration_run_sql_text(
            i_label => i_label,
            i_sql_text => l_function_chunk_template);
END;
$$;

COMMENT ON FUNCTION dml_utils.set_migration_run_function IS
    'Replaces the recorded sql_text of the unfinished run for the label with the '
        'template that calls the given function over the run''s driving table, so '
        'the next run_function_over_table call uses the adjusted function. A '
        'non-NULL i_filter is ANDed onto each chunk''s range predicate; a NULL '
        'i_filter removes any filter from the stored template.';
