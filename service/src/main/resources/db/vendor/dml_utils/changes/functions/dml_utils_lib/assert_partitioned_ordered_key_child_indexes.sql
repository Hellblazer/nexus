CREATE OR REPLACE FUNCTION dml_utils_lib.assert_partitioned_ordered_key_child_indexes(
    i_index_oid oid,
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_index_name dml_utils_data.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SECURITY INVOKER
AS
$$
DECLARE
    l_leaf record;
BEGIN
    -- Validate the parent index itself first, so a NULL/stale/wrong OID fails
    -- with an accurate 42704 instead of "every leaf has no matching child index".
    PERFORM dml_utils_lib.assert_ordered_key_index_usable(
            i_index_oid => i_index_oid,
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_index_name => i_index_name);

    -- Pair each storage leaf with its descendant child index in one walk (the
    -- index tree mirrors the table tree, so a multi-level leaf's index is not a
    -- direct child of the root).
    FOR l_leaf IN
        SELECT live.leaf_ordinal,
               live.leaf_schema_name,
               live.leaf_name,
               child.child_index_oid,
               child.child_index_name
        FROM dml_utils_lib.leaf_partitions(
                     i_schema_name => i_schema_name,
                     i_table_name => i_table_name,
                     i_chunk_by => 'ordered_key') AS live
                 LEFT JOIN dml_utils_lib.ordered_key_leaf_indexes(i_index_oid) AS child
                           ON child.leaf_oid = live.leaf_oid
        ORDER BY live.leaf_ordinal
        LOOP
            IF l_leaf.child_index_oid IS NULL THEN
                RAISE EXCEPTION 'leaf %.% has no matching child index for ordered key %.%',
                    l_leaf.leaf_schema_name, l_leaf.leaf_name, i_schema_name, i_index_name
                    USING ERRCODE = '0A000';
            END IF;

            -- Each child index must itself be usable (valid/ready/live and the same
            -- shape contract), so a broken or redefined leaf index fails before a
            -- run row exists.
            PERFORM 1
            FROM dml_utils_lib.ordered_key_attributes_for_index(
                         i_index_oid => l_leaf.child_index_oid,
                         i_schema_name => l_leaf.leaf_schema_name,
                         i_table_name => l_leaf.leaf_name,
                         i_index_name => l_leaf.child_index_name) AS a;
        END LOOP;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_partitioned_ordered_key_child_indexes(
    oid,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) IS
    'Validates the supplied parent index, then walks the storage leaves of a '
        'partition-wise ordered-key run and requires each leaf''s matching '
        'descendant child index to exist and satisfy the ordered-key contract. A '
        'missing child is 0A000; an unusable child is '
        '0A000/55000.';
