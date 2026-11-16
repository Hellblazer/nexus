CREATE OR REPLACE FUNCTION dml_utils_lib.snapshot_partition_leaf_manifest(
    i_run_id bigint,
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_chunk_by dml_utils_data.chunking_strategy,
    i_boundary_key_index name
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
DECLARE
    l_run         record;
    l_caller      name;
    l_leaf_schema text;
    l_leaf_name   text;
BEGIN
    IF i_run_id IS NULL THEN
        RAISE EXCEPTION 'i_run_id must not be NULL'
            USING ERRCODE = '22023';
    END IF;

    -- The run row is the authority for the driving table and strategy. A
    -- SECURITY DEFINER function must not let a caller attach another relation's
    -- leaves (or choose a different strategy) for an arbitrary run id.
    SELECT run.partition_wise,
           run.boundaries_calculated_at,
           run.cancelled_at,
           run.driving_table_schema_name,
           run.driving_table_name,
           run.chunk_by,
           run.ordered_key_index
    INTO l_run
    FROM dml_utils_data.migration_run AS run
    WHERE run.run_id = i_run_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'migration run % does not exist', i_run_id
            USING ERRCODE = 'P0002';
    END IF;

    IF NOT l_run.partition_wise THEN
        RAISE EXCEPTION 'run % is not partition-wise', i_run_id
            USING ERRCODE = '22023';
    END IF;

    IF l_run.boundaries_calculated_at IS NOT NULL OR l_run.cancelled_at IS NOT NULL THEN
        RAISE EXCEPTION 'run % is already finalized or cancelled', i_run_id
            USING ERRCODE = '55000';
    END IF;

    IF EXISTS (SELECT 1
               FROM dml_utils_data.migration_run_leaf AS leaf
               WHERE leaf.run_id = i_run_id)
    THEN
        RAISE EXCEPTION 'run % already has a leaf manifest', i_run_id
            USING ERRCODE = '55000';
    END IF;

    IF i_schema_name <> l_run.driving_table_schema_name
        OR i_table_name <> l_run.driving_table_name
    THEN
        RAISE EXCEPTION 'run % drives %.%, not %.%',
            i_run_id,
            l_run.driving_table_schema_name, l_run.driving_table_name,
            i_schema_name, i_table_name
            USING ERRCODE = '22023';
    END IF;

    IF i_chunk_by IS DISTINCT FROM l_run.chunk_by THEN
        RAISE EXCEPTION 'run % uses chunk_by %, not %',
            i_run_id, l_run.chunk_by, i_chunk_by
            USING ERRCODE = '22023';
    END IF;

    IF l_run.chunk_by = 'ordered_key' THEN
        IF i_boundary_key_index IS NULL THEN
            RAISE EXCEPTION 'i_boundary_key_index is required for an ordered_key run'
                USING ERRCODE = '22023';
        END IF;
        IF i_boundary_key_index <> l_run.ordered_key_index THEN
            RAISE EXCEPTION 'run % uses ordered key index %, not %',
                i_run_id, l_run.ordered_key_index, i_boundary_key_index
                USING ERRCODE = '22023';
        END IF;
    ELSIF i_boundary_key_index IS NOT NULL THEN
        RAISE EXCEPTION 'i_boundary_key_index is only valid for an ordered_key run'
            USING ERRCODE = '22023';
    END IF;

    l_caller := COALESCE(NULLIF(pg_catalog.current_setting('role', true), 'none'),
                         session_user)::name;

    -- Phase 1 only: snapshot the deterministic leaf manifest. The boundaries are
    -- calculated later, one leaf per committed transaction, so a failure
    -- preserves the leaves already done. Every leaf is inserted, including one
    -- with no rows; emptiness is a calculation result.
    INSERT INTO dml_utils_data.migration_run_leaf
    (run_id, leaf_ordinal, leaf_schema_name, leaf_name, leaf_oid,
     leaf_bound, relation_filepath, state)
    SELECT i_run_id,
           live.leaf_ordinal,
           live.leaf_schema_name::dml_utils_data.non_null_text,
           live.leaf_name::dml_utils_data.non_null_text,
           live.leaf_oid,
           live.leaf_bound,
           live.relation_filepath,
           'pending'
    FROM dml_utils_lib.leaf_partitions(
                 i_schema_name => l_run.driving_table_schema_name,
                 i_table_name => l_run.driving_table_name,
                 i_chunk_by => l_run.chunk_by) AS live;

    -- The manifest is now the exact leaf set; validate each stored leaf's
    -- ordered-key child index (a leaf attached between the descriptor read and
    -- the insert is not in this set, so it cannot slip through).
    IF l_run.chunk_by = 'ordered_key' THEN
        PERFORM dml_utils_lib.assert_partitioned_ordered_key_leaves(
                i_run_id => i_run_id,
                i_index_name => l_run.ordered_key_index);
    END IF;

    -- Use the snapshotted OID rather than re-resolving the name: a concurrently
    -- dropped/renamed leaf must not turn this privilege failure into 42P01, and
    -- the deterministic order makes the reported leaf stable.
    SELECT leaf.leaf_schema_name, leaf.leaf_name
    INTO l_leaf_schema, l_leaf_name
    FROM dml_utils_data.migration_run_leaf AS leaf
    WHERE leaf.run_id = i_run_id
      AND NOT pg_catalog.has_table_privilege(l_caller, leaf.leaf_oid, 'SELECT')
    ORDER BY leaf.leaf_schema_name, leaf.leaf_name, leaf.leaf_oid
    LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION 'caller lacks SELECT privilege on leaf %.%',
            l_leaf_schema, l_leaf_name
            USING ERRCODE = '42501';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.snapshot_partition_leaf_manifest(
    bigint,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name
    ) IS
    'Snapshots the deterministic leaf manifest for a new partition-wise run: one '
        'pending row per storage leaf with its identity, bound and blocks filepath. '
        'The run row is the authority for the driving table, strategy and ordered '
        'key index (a mismatch, a non-partition-wise run, a finalized/cancelled '
        'run or an existing manifest is rejected; 22023/55000/P0002). It then '
        'validates every ordered-key child index and requires the effective caller '
        'to SELECT every stored leaf (42501). SECURITY DEFINER so the manifest '
        'write runs as the owner.';
