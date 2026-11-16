CREATE OR REPLACE FUNCTION dml_utils_lib.populate_migration_boundaries(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_label dml_utils_data.non_null_text,
    i_sql_text dml_utils_data.non_null_text,
    i_chunk_size dml_utils_data.positive_integer,
    i_threads dml_utils_data.positive_integer DEFAULT 1,
    i_chunk_by dml_utils_data.chunking_strategy DEFAULT 'primary_key',
    i_driving_table_alias dml_utils_data.non_null_text DEFAULT 't',
    -- Delay in milliseconds after each completed chunk. 0 disables it.
    -- Negative values are rejected before any run is created.
    i_sleep_ms dml_utils_data.non_negative_integer DEFAULT 0,
    -- The unique index whose order an ordered_key run uses. Required for
    -- ordered_key and rejected for the other strategies.
    i_boundary_key_index name DEFAULT NULL,
    -- When true, the driving table is a declarative partitioned parent and this
    -- call only creates the run and its pending leaf manifest; the per-leaf
    -- boundaries are calculated separately, one committed transaction per leaf.
    i_partition_wise dml_utils_data.non_null_boolean DEFAULT FALSE
)
    RETURNS bigint
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
DECLARE
    l_key    record;
    l_run_id bigint;
BEGIN
    -- The index selects the ordered key, so it is required for that strategy and
    -- rejected for the others; the shared helper owns that rule.
    PERFORM dml_utils_lib.assert_boundary_key_index(
            i_chunk_by => i_chunk_by,
            i_boundary_key_index => i_boundary_key_index);

    PERFORM dml_utils_lib.assert_schema_exists(i_schema_name => i_schema_name);
    PERFORM dml_utils_lib.assert_table_exists(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name);

    -- This function is SECURITY DEFINER so the boundary INSERT/UPDATE can run as
    -- the owner, but the driving-table scan must still be authorized by the
    -- caller.
    PERFORM dml_utils_lib.assert_caller_can_select_relation(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name);

    PERFORM dml_utils_lib.assert_no_active_run_for_label(i_label => i_label);

    -- Resolve and validate the key before the run exists. A partition-wise parent
    -- is resolved the same way; the per-leaf scan applies that key to each leaf.
    IF i_partition_wise::boolean THEN
        PERFORM dml_utils_lib.assert_partitioned_table(
                i_schema_name => i_schema_name,
                i_table_name => i_table_name);
    END IF;

    SELECT o_key_columns,
           o_key_kinds,
           o_relation_filepath,
           o_ordered_key_index,
           o_ordered_key_columns,
           o_ordered_key_kinds
    INTO l_key
    FROM dml_utils_lib.resolve_migration_creation_key(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_chunk_by => i_chunk_by,
            i_boundary_key_index => i_boundary_key_index,
            i_partition_wise => i_partition_wise);

    -- started_at is the run's start: the boundary calculation begins here. It is
    -- written in this worker's transaction, so it commits with the run and the
    -- boundaries and survives a later failed chunk-processing attempt.
    -- The alias is a creation-time input, stored with the run so a resume
    -- renders with the same alias the first call used.
    INSERT INTO dml_utils_data.migration_run (label, sql_text, chunk_size, chunk_by,
                                              partition_wise, threads,
                                              sleep_ms,
                                              driving_table_schema_name, driving_table_name,
                                              driving_table_alias,
                                              driving_table_relation_filepath,
                                              ordered_key_index, ordered_key_columns,
                                              ordered_key_kinds,
                                              started_at)
    VALUES (i_label, i_sql_text, i_chunk_size, i_chunk_by, i_partition_wise::boolean, i_threads,
            i_sleep_ms,
            i_schema_name, i_table_name,
            i_driving_table_alias,
            l_key.o_relation_filepath,
            l_key.o_ordered_key_index,
            l_key.o_ordered_key_columns,
            l_key.o_ordered_key_kinds,
            pg_catalog.clock_timestamp())
    RETURNING run_id
        INTO l_run_id;

    IF i_partition_wise::boolean THEN
        -- Phase 1 only: snapshot the deterministic leaf manifest. The boundaries
        -- are calculated later, one leaf per committed transaction.
        PERFORM dml_utils_lib.snapshot_partition_leaf_manifest(
                i_run_id => l_run_id,
                i_schema_name => i_schema_name,
                i_table_name => i_table_name,
                i_chunk_by => i_chunk_by,
                i_boundary_key_index => i_boundary_key_index);
        RETURN l_run_id;
    END IF;

    PERFORM dml_utils_lib.populate_single_relation_boundaries(
            i_run_id => l_run_id,
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_chunk_by => i_chunk_by,
            i_key_columns => l_key.o_key_columns,
            i_key_kinds => l_key.o_key_kinds,
            i_chunk_size => i_chunk_size);

    RETURN l_run_id;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.populate_migration_boundaries IS
    'Creates a run and returns its id. resolve_migration_creation_key resolves '
        'the key and filepath, and for a single-relation run '
        'populate_single_relation_boundaries scans the driving table and inserts '
        'the contiguous 0..N boundaries in one statement. For i_partition_wise it '
        'instead validates the partitioned parent and calls '
        'snapshot_partition_leaf_manifest, which stores the deterministic pending '
        'leaf manifest and returns without boundaries or a boundaries_calculated_at '
        'milestone, so calculation can resume one leaf per transaction. SECURITY '
        'DEFINER so the metadata writes run as the owner, with an explicit '
        'effective-caller SELECT check on the parent and every leaf.';
