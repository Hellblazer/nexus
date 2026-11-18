CREATE OR REPLACE FUNCTION dml_utils_lib.leaf_partitions(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_chunk_by dml_utils_data.chunking_strategy
)
    RETURNS TABLE
            (
                leaf_ordinal      integer,
                leaf_schema_name  text,
                leaf_name         text,
                leaf_oid          oid,
                leaf_bound        text,
                relation_filepath text
            )
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
DECLARE
    l_parent oid;
BEGIN
    PERFORM dml_utils_lib.assert_schema_exists(i_schema_name => i_schema_name);
    PERFORM dml_utils_lib.assert_partitioned_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name);

    SELECT c.oid
    INTO l_parent
    FROM pg_catalog.pg_class AS c
             JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = i_schema_name
      AND c.relname = i_table_name;

    PERFORM dml_utils_lib.assert_partitioned_leaf_relations(i_parent => l_parent);
    PERFORM dml_utils_lib.assert_partitioned_leaf_privileges(i_parent => l_parent);

    -- The order is the manifest contract: partition bound, then schema, name and
    -- OID, all byte-wise, so every enumeration of the same tree yields the same
    -- ordinals. A leaf with no rows still gets a row: emptiness is a calculation
    -- result, not a reason to drop it from the snapshot.
    RETURN QUERY
        SELECT (pg_catalog.row_number() OVER (
            ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) COLLATE pg_catalog."C",
                n.nspname::text COLLATE pg_catalog."C",
                c.relname::text COLLATE pg_catalog."C",
                c.oid))::integer,
               n.nspname::text,
               c.relname::text,
               c.oid,
               pg_catalog.pg_get_expr(c.relpartbound, c.oid),
               CASE
                   WHEN i_chunk_by = 'blocks'
                       THEN pg_catalog.pg_relation_filepath(c.oid)
                   END
        FROM pg_catalog.pg_partition_tree(l_parent) AS tree
                 JOIN pg_catalog.pg_class AS c ON c.oid = tree.relid
                 JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE tree.isleaf
        ORDER BY 1;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.leaf_partitions(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy
    ) IS
    'Lists the storage leaves of a partitioned table in the deterministic '
        'manifest order (partition bound, schema, name, OID, byte-wise), with '
        'their OIDs, partition-bound expressions and, for chunk_by = blocks, '
        'physical relation filepaths. assert_partitioned_leaf_relations rejects a '
        'non-table leaf (42809) and assert_partitioned_leaf_privileges rejects a '
        'leaf the caller cannot read (42501). Every leaf is returned, including '
        'one with no rows.';
