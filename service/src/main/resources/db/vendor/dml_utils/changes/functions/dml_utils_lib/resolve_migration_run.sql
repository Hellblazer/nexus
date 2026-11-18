CREATE OR REPLACE FUNCTION dml_utils_lib.resolve_migration_run(
    i_sql_text dml_utils_data.non_null_text,
    i_driving_table_schema_name dml_utils_data.non_null_text,
    i_driving_table_name dml_utils_data.non_null_text,
    i_label dml_utils_data.non_null_text,
    i_chunk_size dml_utils_data.positive_integer,
    i_threads dml_utils_data.positive_integer,
    i_driving_table_alias dml_utils_data.non_null_text,
    i_chunk_by dml_utils_data.chunking_strategy DEFAULT 'primary_key',
    -- Initial delay for a new run; a resumed run uses its stored value.
    i_sleep_ms dml_utils_data.non_negative_integer DEFAULT 0,
    -- The unique index whose order an ordered_key run uses. Required for a new
    -- ordered_key run; ignored with a notice when resuming an existing run, where
    -- the stored strategy and index win.
    i_boundary_key_index name DEFAULT NULL,
    -- When true, the driving table is a declarative partitioned parent and the
    -- run's boundaries are calculated leaf by leaf. The stored flag wins on resume.
    i_partition_wise dml_utils_data.non_null_boolean DEFAULT FALSE,
    OUT o_run_id bigint,
    OUT o_already_completed boolean,
    OUT o_effective_sql_text text,
    OUT o_effective_schema_name text,
    OUT o_effective_table_name text,
    OUT o_effective_alias text,
    OUT o_effective_threads integer,
    OUT o_effective_sleep_ms integer,
    OUT o_chunk_by dml_utils_data.chunking_strategy,
    OUT o_partition_wise boolean,
    OUT o_key_columns name[],
    OUT o_key_kinds text[]
)
    RETURNS record
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
DECLARE
    l_run       dml_utils_data.migration_run%ROWTYPE;
    l_effective record;
    l_key       record;
BEGIN
    IF i_chunk_by IS NULL THEN
        RAISE EXCEPTION 'i_chunk_by must not be NULL'
            USING ERRCODE = '22023';
    END IF;

    -- Reuse the active run for the label when there is one; otherwise create it
    -- by running populate_migration_boundaries in a worker so it commits
    -- autonomously and the boundaries become visible to the processing workers.
    SELECT run_id,
           label,
           sql_text,
           chunk_size,
           chunk_by,
           partition_wise,
           threads,
           sleep_ms,
           driving_table_schema_name,
           driving_table_name,
           driving_table_alias,
           driving_table_relation_filepath,
           ordered_key_index,
           ordered_key_columns,
           ordered_key_kinds,
           created_at,
           updated_at,
           started_at,
           boundaries_calculated_at,
           total_chunks,
           total_chunks_completed,
           completed_at,
           cancelled_at,
           archived_at
    INTO l_run
    FROM dml_utils_lib.find_active_migration_run(i_label => i_label);

    IF FOUND AND l_run.completed_at IS NOT NULL THEN
        RAISE NOTICE 'run for label % already completed at %', i_label, l_run.completed_at;
        o_run_id := l_run.run_id;
        o_already_completed := true;
        RETURN;
    END IF;

    -- A run that is not already complete is resumed or created below.
    o_already_completed := false;

    -- The stored thread count wins for a resumed run. Each chunk is one
    -- background worker; max_worker_processes is a cluster-wide ceiling, not the
    -- free capacity for this call (other workers may hold slots), so this is a
    -- cheap early sanity check, not a capacity guarantee. A launch that still
    -- fails partway leaves the launched chunks committed and the run resumable,
    -- which the caller retries.
    o_effective_threads := CASE WHEN FOUND THEN l_run.threads::integer ELSE i_threads::integer END;
    o_effective_sleep_ms := CASE WHEN FOUND THEN l_run.sleep_ms::integer ELSE i_sleep_ms::integer END;
    IF o_effective_threads > pg_catalog.current_setting('max_worker_processes')::integer THEN
        RAISE EXCEPTION 'threads (%) exceeds max_worker_processes (%)',
            o_effective_threads, pg_catalog.current_setting('max_worker_processes')
            USING ERRCODE = '22023';
    END IF;

    IF FOUND THEN
        SELECT effective.o_effective_sql_text,
               effective.o_effective_schema_name,
               effective.o_effective_table_name,
               effective.o_effective_alias,
               effective.o_effective_threads,
               effective.o_effective_sleep_ms,
               effective.o_chunk_by,
               effective.o_partition_wise
        INTO l_effective
        FROM dml_utils_lib.resume_migration_run(
                     i_run => l_run,
                     i_driving_table_schema_name => i_driving_table_schema_name,
                     i_driving_table_name => i_driving_table_name,
                     i_sql_text => i_sql_text,
                     i_chunk_size => i_chunk_size,
                     i_threads => i_threads,
                     i_driving_table_alias => i_driving_table_alias,
                     i_chunk_by => i_chunk_by,
                     i_sleep_ms => i_sleep_ms,
                     i_boundary_key_index => i_boundary_key_index,
                     i_partition_wise => i_partition_wise) AS effective;
        o_run_id := l_run.run_id;

        o_effective_sql_text := l_effective.o_effective_sql_text;
        o_effective_schema_name := l_effective.o_effective_schema_name;
        o_effective_table_name := l_effective.o_effective_table_name;
        o_effective_alias := l_effective.o_effective_alias;
        o_effective_threads := l_effective.o_effective_threads;
        o_effective_sleep_ms := l_effective.o_effective_sleep_ms;
        o_chunk_by := l_effective.o_chunk_by;
        o_partition_wise := l_effective.o_partition_wise;
    ELSE
        o_run_id := dml_utils_lib.create_migration_run(
                i_schema_name => i_driving_table_schema_name,
                i_table_name => i_driving_table_name,
                i_label => i_label,
                i_sql_text => i_sql_text,
                i_chunk_size => i_chunk_size,
                i_threads => i_threads,
                i_chunk_by => i_chunk_by,
                i_driving_table_alias => i_driving_table_alias,
                i_sleep_ms => i_sleep_ms,
                i_boundary_key_index => i_boundary_key_index,
                i_partition_wise => i_partition_wise);

        o_effective_sql_text := i_sql_text;
        o_effective_schema_name := i_driving_table_schema_name::text;
        o_effective_table_name := i_driving_table_name::text;
        o_effective_alias := i_driving_table_alias::text;
        o_effective_threads := i_threads::integer;
        o_effective_sleep_ms := i_sleep_ms::integer;
        o_chunk_by := i_chunk_by;
        o_partition_wise := i_partition_wise::boolean;
    END IF;

    -- Resolve the key the coordinator uses to read boundary values. The run row
    -- stores the ordered descriptor and the primary-key run resolves its real
    -- primary key; the block renderer uses a placeholder column name.
    IF o_chunk_by = 'blocks' THEN
        o_key_columns := ARRAY ['ctid']::name[];
        o_key_kinds := ARRAY ['bigint'];
    ELSE
        SELECT key.o_key_columns,
               key.o_key_kinds
        INTO l_key
        FROM dml_utils_lib.migration_run_boundary_key(i_run_id => o_run_id) AS key;

        o_key_columns := l_key.o_key_columns;
        o_key_kinds := l_key.o_key_kinds;
        IF o_key_columns IS NULL OR o_key_kinds IS NULL THEN
            RAISE EXCEPTION 'run % has no stored boundary key', o_run_id
                USING ERRCODE = '55000';
        END IF;
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.resolve_migration_run IS
    'Resolves or creates the active run for the label and returns the inputs the '
        'coordinator should use: the run id, whether it was already complete, the '
        'effective sql_text/schema/table/alias/threads/sleep_ms and chunking '
        'strategy (stored values for a resumed run), whether the run is '
        'partition-wise, and the boundary key columns and kinds (the stored '
        'ordered-key descriptor for ordered_key, the real primary key for '
        'primary_key, or a single bigint block number for blocks). '
        'find_active_migration_run, resume_migration_run and create_migration_run '
        'own the lookup, resume validation and autonomous creation. A resumed run '
        'fails closed if the driving table''s physical filepath changed, and a '
        'resumed ordered-key run re-validates its stored index and descriptor, '
        'raising object_not_in_prerequisite_state (55000) if the index no longer '
        'matches.';
