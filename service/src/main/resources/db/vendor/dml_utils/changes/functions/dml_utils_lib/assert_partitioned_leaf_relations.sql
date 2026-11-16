CREATE OR REPLACE FUNCTION dml_utils_lib.assert_partitioned_leaf_relations(
    i_parent oid
)
    RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SECURITY INVOKER
AS
$$
DECLARE
    l_leaf_schema  text;
    l_leaf_name    text;
    l_leaf_relkind text;
BEGIN
    -- pg_partition_tree expands a sub-partitioned table to its storage leaves.
    -- A leaf that is not an ordinary table (typically a foreign table) has no
    -- heap, so it cannot be chunked or addressed like one.
    SELECT n.nspname::text, c.relname::text, c.relkind::text
    INTO l_leaf_schema, l_leaf_name, l_leaf_relkind
    FROM pg_catalog.pg_partition_tree(i_parent) AS tree
             JOIN pg_catalog.pg_class AS c ON c.oid = tree.relid
             JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE tree.isleaf
      AND c.relkind <> 'r'
    ORDER BY n.nspname::text COLLATE pg_catalog."C",
             c.relname::text COLLATE pg_catalog."C",
             c.oid
    LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION 'leaf %.% has relkind %, not an ordinary table',
            l_leaf_schema, l_leaf_name, l_leaf_relkind
            USING ERRCODE = '42809';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_partitioned_leaf_relations(oid) IS
    'Raises wrong_object_type (42809) when any storage leaf of the partitioned '
        'parent is not an ordinary table, so a foreign-table leaf is rejected.';
