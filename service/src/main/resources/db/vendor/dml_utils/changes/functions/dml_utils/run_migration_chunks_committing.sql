CREATE OR REPLACE PROCEDURE dml_utils.run_migration_chunks_committing(
    i_sql_text dml_utils_data.non_null_text,
    i_driving_table_schema_name dml_utils_data.non_null_text,
    i_driving_table_name dml_utils_data.non_null_text,
    i_label dml_utils_data.non_null_text,
    i_chunk_size dml_utils_data.positive_integer DEFAULT 1000,
    i_threads dml_utils_data.positive_integer DEFAULT 1,
    i_driving_table_alias dml_utils_data.non_null_text DEFAULT 't',
    i_chunk_by dml_utils_data.chunking_strategy DEFAULT 'primary_key',
    -- See run_migration_chunks: how long one wait for a worker lasts before the
    -- coordinator re-checks.
    i_poll_timeout_ms dml_utils_data.positive_integer DEFAULT 1,
    -- See run_migration_chunks: the delay after each completed chunk.
    i_sleep_ms dml_utils_data.non_negative_integer DEFAULT 0,
    -- See run_migration_chunks: the unique index an ordered_key run uses.
    i_boundary_key_index name DEFAULT NULL,
    -- See run_migration_chunks: the driving table is a declarative partitioned
    -- parent.
    i_partition_wise dml_utils_data.non_null_boolean DEFAULT FALSE
)
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
BEGIN
    -- A procedure that COMMITs cannot run in a transaction block or be called from
    -- a function; PostgreSQL then fails the first COMMIT with 2D000
    -- (invalid_transaction_termination). Take that failure here, before the run
    -- exists, so a misplaced CALL changes nothing. At top level this COMMIT ends
    -- an empty transaction and is harmless.
    COMMIT;

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
            i_commit => true);
END;
$$;

COMMENT ON PROCEDURE dml_utils.run_migration_chunks_committing IS
    'Same parameters, defaults and behaviour as run_migration_chunks (labels, '
        'resume, threads, sleep_ms, partition-wise, ordered_key, error recording, '
        'cancellation), except that it COMMITs after every launch, wait and settle '
        'step, so the coordinator never holds one snapshot across chunks and VACUUM can '
        'reclaim the rows of every chunk already committed while the run goes on; '
        'run_migration_chunks is one statement and holds the vacuum horizon until it '
        'returns. Because it COMMITs it must be CALLed at top level: not inside an '
        'explicit transaction block (BEGIN ... COMMIT), not from a function, and not '
        'through a client that wraps the call in a transaction; otherwise the first '
        'COMMIT fails with invalid_transaction_termination (2D000) before the run is '
        'created.';
