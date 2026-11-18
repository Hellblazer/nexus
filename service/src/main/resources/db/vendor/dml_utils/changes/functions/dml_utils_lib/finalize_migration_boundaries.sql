CREATE OR REPLACE FUNCTION dml_utils_lib.finalize_migration_boundaries(
    i_run_id bigint
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
DECLARE
    l_run          record;
    l_terminal     record;
    l_terminal_key dml_utils_data.migration_key;
    l_total_chunks bigint;
BEGIN
    IF i_run_id IS NULL THEN
        RAISE EXCEPTION 'run id must not be NULL'
            USING ERRCODE = '22023';
    END IF;

    -- Serialize finalization with leaf calculation on the run row.
    SELECT run.partition_wise,
           run.boundaries_calculated_at,
           run.cancelled_at
    INTO l_run
    FROM dml_utils_data.migration_run AS run
    WHERE run.run_id = i_run_id
        FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'migration run % does not exist', i_run_id
            USING ERRCODE = 'P0002';
    END IF;

    IF NOT l_run.partition_wise THEN
        RAISE EXCEPTION 'run % is not partition-wise', i_run_id
            USING ERRCODE = '22023';
    END IF;

    -- Idempotent: a finalized run keeps its terminal boundary and totals.
    IF l_run.boundaries_calculated_at IS NOT NULL OR l_run.cancelled_at IS NOT NULL THEN
        RETURN;
    END IF;

    IF EXISTS (SELECT 1
               FROM dml_utils_data.migration_run_leaf AS leaf
               WHERE leaf.run_id = i_run_id
                 AND leaf.state <> 'calculated')
    THEN
        RAISE EXCEPTION 'run % still has uncalculated leaves', i_run_id
            USING ERRCODE = '55000';
    END IF;

    SELECT o_leaf_ordinal,
           o_leaf_schema_name,
           o_leaf_name,
           o_bigint_values,
           o_text_values,
           o_uuid_values,
           o_timestamptz_values,
           o_bytea_values
    INTO l_terminal
    FROM dml_utils_lib.terminal_leaf_for_run(i_run_id => i_run_id);

    IF l_terminal.o_leaf_ordinal IS NOT NULL THEN
        l_terminal_key := ROW (l_terminal.o_bigint_values,
            l_terminal.o_text_values, l_terminal.o_uuid_values,
            l_terminal.o_timestamptz_values,
            l_terminal.o_bytea_values)::dml_utils_data.migration_key;

        l_total_chunks := dml_utils_lib.append_terminal_boundary(
                i_run_id => i_run_id,
                i_leaf_ordinal => l_terminal.o_leaf_ordinal,
                i_leaf_schema_name => l_terminal.o_leaf_schema_name,
                i_leaf_name => l_terminal.o_leaf_name,
                i_terminal_key => l_terminal_key);
    ELSE
        -- Every leaf is empty: no terminal boundary and no chunks.
        l_total_chunks := 0;
    END IF;

    UPDATE dml_utils_data.migration_run
    SET boundaries_calculated_at = pg_catalog.clock_timestamp(),
        total_chunks             = l_total_chunks
    WHERE run_id = i_run_id
      AND boundaries_calculated_at IS NULL;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.finalize_migration_boundaries(bigint) IS
    'Writes the single terminal boundary from the last non-empty leaf and sets '
        'boundaries_calculated_at and total_chunks, once. terminal_leaf_for_run '
        'picks the leaf and append_terminal_boundary writes the boundary. '
        'Idempotent (does nothing for a finalized or cancelled run) and raises '
        '55000 while any leaf is not calculated. SECURITY DEFINER so the '
        'invoker-owned coordinator can write metadata and read the terminal leaf.';
