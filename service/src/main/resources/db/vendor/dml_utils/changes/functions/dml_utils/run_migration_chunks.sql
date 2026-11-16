CREATE OR REPLACE FUNCTION dml_utils.run_migration_chunks(
    i_sql_text dml_utils_data.non_null_text,
    i_driving_table_schema_name dml_utils_data.non_null_text,
    i_driving_table_name dml_utils_data.non_null_text,
    i_label dml_utils_data.non_null_text,
    i_chunk_size dml_utils_data.positive_integer DEFAULT 1000,
    i_threads dml_utils_data.positive_integer DEFAULT 1,
    i_driving_table_alias dml_utils_data.non_null_text DEFAULT 't',
    i_chunk_by dml_utils_data.chunking_strategy DEFAULT 'primary_key',
    -- How long pg_background_wait_any waits for a worker before returning so this
    -- coordinator can re-check. 1ms keeps per-chunk detection latency low; a
    -- caller whose chunks take non-trivial time can raise it to cut coordinator
    -- CPU, at the cost of up to ~50ms added detection latency. positive_integer,
    -- so 0 is rejected: pg_background_wait_any treats a non-positive timeout as
    -- "block forever", which would stop this coordinator re-checking its state.
    i_poll_timeout_ms dml_utils_data.positive_integer DEFAULT 1,
    -- Milliseconds to wait after each successfully completed chunk before
    -- launching another. This seeds a new run; a resumed run uses its stored
    -- value. 0 disables the delay.
    i_sleep_ms dml_utils_data.non_negative_integer DEFAULT 0,
    -- The unique index whose order an ordered_key run uses. Required for
    -- ordered_key; rejected for a new run of the other strategies and ignored
    -- with a notice when resuming an existing run.
    i_boundary_key_index name DEFAULT NULL,
    -- When true, the driving table is a declarative partitioned parent: the run
    -- and its leaf manifest are created first, each leaf's boundaries are
    -- calculated in its own worker/transaction, and every chunk is rendered
    -- against its leaf.
    i_partition_wise dml_utils_data.non_null_boolean DEFAULT FALSE
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
BEGIN
    -- The coordinator lives in dml_utils_lib.coordinate_migration_chunks, shared
    -- with run_migration_chunks_committing. A function cannot COMMIT, so this call
    -- passes i_commit = false and the whole run stays in the caller's transaction,
    -- which keeps this statement's snapshot (and the vacuum horizon) for the run.
    CALL dml_utils_lib.coordinate_migration_chunks(
            i_sql_text => i_sql_text,
            i_driving_table_schema_name => i_driving_table_schema_name,
            i_driving_table_name => i_driving_table_name,
            i_label => i_label,
            i_chunk_size => i_chunk_size,
            i_threads => i_threads,
            i_driving_table_alias => i_driving_table_alias,
            i_chunk_by => i_chunk_by,
            i_poll_timeout_ms => i_poll_timeout_ms,
            i_sleep_ms => i_sleep_ms,
            i_boundary_key_index => i_boundary_key_index,
            i_partition_wise => i_partition_wise,
            i_commit => false);
END;
$$;

COMMENT ON FUNCTION dml_utils.run_migration_chunks IS
    'Runs the chunk SQL for every chunk of the driving table (primary_key: fixed '
        'rows in primary-key order; ordered_key: fixed rows in the order of '
        'i_boundary_key_index, a unique index on the driving table; blocks: '
        'half-open physical ctid ranges), up to i_threads pg_background workers at '
        'a time, resuming an active run for the label and recording its completion. '
        'A resumed run uses the stored sql_text, chunk size, strategy, key, threads, '
        'sleep_ms, driving table and alias. i_boundary_key_index is required for '
        'ordered_key and rejected for a new run of the other strategies; a resumed '
        'run keeps its stored key and ignores a differing index with a notice. '
        'After each successfully '
        'completed chunk it waits the run''s sleep_ms before launching another '
        '(except after the final chunk); the sleep is cancellation-aware. On a '
        'failed chunk it stops launching, lets the in-flight chunks commit, drains '
        'and detaches them, and re-raises the error; the failure is recorded (best '
        'effort) in dml_utils_data.migration_error first. A boundary already claimed '
        'by another coordinator (P0C01) is treated as benign. A run cancelled with '
        'dml_utils.cancel_migration_run stops launching, cancels the in-flight '
        'workers, marks the run and its remaining boundaries cancelled and '
        'returns; a cancelled run is terminal. With i_partition_wise the parent is '
        'declarative partitioned: prepare_partition_wise_execution first finishes '
        'one-leaf-per-transaction boundary calculation, finalizes, then processes '
        'each chunk against its boundary''s leaf via next_migration_chunk. The live '
        'tree is compared with the manifest before calculation and processing, and '
        'any change fails closed with object_not_in_prerequisite_state (55000). '
        'The call is one statement, so its snapshot holds the vacuum horizon until '
        'it returns and VACUUM cannot reclaim the rows of chunks already committed; '
        'run_migration_chunks_committing does the same work without that.';
