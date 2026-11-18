CREATE OR REPLACE FUNCTION dml_utils_lib.assert_partitioned_ordered_key_leaves(
    i_run_id bigint,
    i_index_name dml_utils_data.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SECURITY INVOKER
AS
$$
DECLARE
    l_partition_wise boolean;
    l_chunk_by       dml_utils_data.chunking_strategy;
    l_schema_name    text;
    l_table_name     text;
    l_run_index      name;
    l_parent_index   oid;
    l_leaf           record;
BEGIN
    SELECT run.partition_wise,
           run.chunk_by,
           run.driving_table_schema_name::text,
           run.driving_table_name::text,
           run.ordered_key_index
    INTO l_partition_wise, l_chunk_by, l_schema_name, l_table_name, l_run_index
    FROM dml_utils_data.migration_run AS run
    WHERE run.run_id = i_run_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'migration run % does not exist', i_run_id
            USING ERRCODE = 'P0002';
    END IF;
    IF NOT l_partition_wise OR l_chunk_by <> 'ordered_key' THEN
        RAISE EXCEPTION 'run % is not a partition-wise ordered-key run', i_run_id
            USING ERRCODE = '22023';
    END IF;

    -- The run row stores the index the boundaries were computed from; a caller
    -- must not validate a different unique index on the same table.
    IF l_run_index IS DISTINCT FROM i_index_name::name THEN
        RAISE EXCEPTION 'run % uses ordered key index %, not %',
            i_run_id, l_run_index, i_index_name
            USING ERRCODE = '22023';
    END IF;

    -- Resolve the index on the run's driving table, not merely by name, so a
    -- same-schema relation with that name cannot be validated by mistake.
    SELECT ic.oid
    INTO l_parent_index
    FROM pg_catalog.pg_index AS i
             JOIN pg_catalog.pg_class AS ic ON ic.oid = i.indexrelid
             JOIN pg_catalog.pg_class AS c ON c.oid = i.indrelid
             JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = l_schema_name
      AND c.relname = l_table_name
      AND ic.relname = i_index_name;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'index %.% does not exist on table %.%',
            l_schema_name, i_index_name, l_schema_name, l_table_name
            USING ERRCODE = '42704';
    END IF;

    -- Pair the manifest rows with the descendant child indexes in one walk, so
    -- the set validated is exactly the set stored for the run.
    FOR l_leaf IN
        SELECT leaf.leaf_ordinal,
               leaf.leaf_schema_name,
               leaf.leaf_name,
               child.child_index_oid,
               child.child_index_name
        FROM dml_utils_data.migration_run_leaf AS leaf
                 LEFT JOIN dml_utils_lib.ordered_key_leaf_indexes(l_parent_index) AS child
                           ON child.leaf_oid = leaf.leaf_oid
        WHERE leaf.run_id = i_run_id
        ORDER BY leaf.leaf_ordinal
        LOOP
            IF l_leaf.child_index_oid IS NULL THEN
                RAISE EXCEPTION 'leaf %.% has no matching child index for ordered key %.%',
                    l_leaf.leaf_schema_name, l_leaf.leaf_name, l_schema_name, i_index_name
                    USING ERRCODE = '0A000';
            END IF;

            PERFORM 1
            FROM dml_utils_lib.ordered_key_attributes_for_index(
                         i_index_oid => l_leaf.child_index_oid,
                         i_schema_name => l_leaf.leaf_schema_name,
                         i_table_name => l_leaf.leaf_name,
                         i_index_name => l_leaf.child_index_name) AS a;
        END LOOP;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_partitioned_ordered_key_leaves(
    bigint,
    dml_utils_data.non_null_text
    ) IS
    'Validates the ordered-key child index of every leaf already snapshotted in a '
        'run''s manifest, walking multi-level index inheritance once. Raises 22023 '
        'when the supplied index is not the run''s stored ordered_key_index, 42704 '
        'for a missing parent index, 0A000 for a leaf without a matching child '
        'index, and 0A000/55000 for an unusable child index.';
