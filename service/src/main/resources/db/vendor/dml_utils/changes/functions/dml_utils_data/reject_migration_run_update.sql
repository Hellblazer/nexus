CREATE OR REPLACE FUNCTION dml_utils_data.reject_migration_run_update()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
BEGIN
    -- label, chunk_size, chunk_by, partition_wise, the ordered-key descriptor and
    -- the driving table are set when the run is created and must never change:
    -- together they identify the run and the boundaries computed from it.
    -- driving_table_relation_filepath is fixed at boundary-calculation time for a
    -- block run, so it is immutable too.
    IF NEW.label IS DISTINCT FROM OLD.label
        OR NEW.chunk_size IS DISTINCT FROM OLD.chunk_size
        OR NEW.chunk_by IS DISTINCT FROM OLD.chunk_by
        OR NEW.partition_wise IS DISTINCT FROM OLD.partition_wise
        OR NEW.ordered_key_index IS DISTINCT FROM OLD.ordered_key_index
        OR NEW.ordered_key_columns IS DISTINCT FROM OLD.ordered_key_columns
        OR NEW.ordered_key_kinds IS DISTINCT FROM OLD.ordered_key_kinds
        OR NEW.driving_table_schema_name IS DISTINCT FROM OLD.driving_table_schema_name
        OR NEW.driving_table_name IS DISTINCT FROM OLD.driving_table_name
        OR NEW.driving_table_relation_filepath IS DISTINCT FROM OLD.driving_table_relation_filepath
    THEN
        RAISE EXCEPTION 'migration_run label, chunk_size, chunk_by, partition_wise, ordered key and driving table are immutable'
            USING ERRCODE = '22023';
    END IF;

    -- total_chunks cannot be set at INSERT (the run row exists before its
    -- boundaries), so populate_migration_boundaries writes it in the same
    -- statement that sets boundaries_calculated_at. Freeze it from then on: the
    -- boundaries it counts are never recomputed, so a later change would
    -- invalidate the completed <= total invariant. total_chunks_completed stays
    -- mutable; the chunk workers advance it.
    IF NEW.total_chunks IS DISTINCT FROM OLD.total_chunks
        AND OLD.boundaries_calculated_at IS NOT NULL
    THEN
        RAISE EXCEPTION 'migration_run total_chunks is immutable once the boundaries are calculated'
            USING ERRCODE = '22023';
    END IF;

    -- boundaries_calculated_at is the milestone the total_chunks freeze keys on;
    -- clearing or moving it would let a later statement rewrite total_chunks and
    -- break the completed <= total invariant.
    IF OLD.boundaries_calculated_at IS NOT NULL
        AND NEW.boundaries_calculated_at IS DISTINCT FROM OLD.boundaries_calculated_at
    THEN
        RAISE EXCEPTION 'migration_run.boundaries_calculated_at is immutable once set'
            USING ERRCODE = '22023';
    END IF;

    -- cancelled_at is set once, by cancel_migration_run or the coordinator; a
    -- second cancellation is a no-op, and the timestamp must not be rewritten.
    IF OLD.cancelled_at IS NOT NULL
        AND NEW.cancelled_at IS DISTINCT FROM OLD.cancelled_at
    THEN
        RAISE EXCEPTION 'migration_run.cancelled_at is immutable once set'
            USING ERRCODE = '22023';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION dml_utils_data.reject_migration_run_update IS
    'BEFORE UPDATE trigger that rejects any change to migration_run.label, '
        'migration_run.chunk_size, migration_run.chunk_by, '
        'migration_run.partition_wise, the ordered-key descriptor '
        '(ordered_key_index, ordered_key_columns, ordered_key_kinds), the driving '
        'table schema/name or the driving table relation filepath. It also '
        'rejects a change to migration_run.total_chunks once '
        'boundaries_calculated_at is set (the value is written with that '
        'milestone), rejects clearing or moving boundaries_calculated_at once it '
        'is set, and rejects rewriting a migration_run.cancelled_at that is '
        'already set; migration_run.total_chunks_completed stays mutable.';
