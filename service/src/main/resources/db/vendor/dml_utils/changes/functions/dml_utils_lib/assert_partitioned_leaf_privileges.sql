CREATE OR REPLACE FUNCTION dml_utils_lib.assert_partitioned_leaf_privileges(
    i_parent oid
)
    RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SECURITY INVOKER
AS
$$
DECLARE
    l_leaf_schema text;
    l_leaf_name   text;
BEGIN
    -- The caller addresses each leaf directly, so a missing per-leaf privilege
    -- must fail before a run or manifest row exists, not halfway through
    -- calculation.
    SELECT n.nspname::text, c.relname::text
    INTO l_leaf_schema, l_leaf_name
    FROM pg_catalog.pg_partition_tree(i_parent) AS tree
             JOIN pg_catalog.pg_class AS c ON c.oid = tree.relid
             JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE tree.isleaf
      AND NOT pg_catalog.has_table_privilege(c.oid, 'SELECT')
    ORDER BY n.nspname::text COLLATE pg_catalog."C",
             c.relname::text COLLATE pg_catalog."C",
             c.oid
    LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION 'permission denied for leaf %.%', l_leaf_schema, l_leaf_name
            USING ERRCODE = '42501';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_partitioned_leaf_privileges(oid) IS
    'Raises insufficient_privilege (42501) when the caller cannot SELECT any '
        'storage leaf of the partitioned parent, so a partition-wise run fails '
        'before creating a manifest row.';
