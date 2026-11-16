CREATE OR REPLACE FUNCTION dml_utils_lib.ordered_key_leaf_indexes(
    i_parent_index_oid oid
)
    RETURNS TABLE
            (
                leaf_oid         oid,
                child_index_oid  oid,
                child_index_name text
            )
    LANGUAGE sql
    STABLE
    SECURITY INVOKER
AS
$$
    -- The index inheritance tree mirrors the table tree, so a multi-level partition's
-- storage-leaf index is not a direct child of the root index. Walk it once and
-- return one row per storage leaf's matching index.
WITH RECURSIVE index_tree AS (SELECT inh.inhrelid
                              FROM pg_catalog.pg_inherits AS inh
                              WHERE inh.inhparent = i_parent_index_oid
                              UNION ALL
                              SELECT inh.inhrelid
                              FROM pg_catalog.pg_inherits AS inh
                                       JOIN index_tree AS tree ON inh.inhparent = tree.inhrelid)
SELECT ci.indrelid,
       tree.inhrelid,
       ic.relname::text
FROM index_tree AS tree
         JOIN pg_catalog.pg_index AS ci ON ci.indexrelid = tree.inhrelid
         JOIN pg_catalog.pg_class AS ic ON ic.oid = tree.inhrelid
    -- The inheritance tree also contains the index of a sub-partitioned
    -- table; only an ordinary table is a storage leaf, and only its index
    -- takes part in the chunk range.
         JOIN pg_catalog.pg_class AS tc ON tc.oid = ci.indrelid
WHERE tc.relkind = 'r';
$$;

COMMENT ON FUNCTION dml_utils_lib.ordered_key_leaf_indexes(oid) IS
    'Walks a partitioned index''s inheritance tree once and returns one row per '
        'storage leaf with its matching descendant index OID and name.';
