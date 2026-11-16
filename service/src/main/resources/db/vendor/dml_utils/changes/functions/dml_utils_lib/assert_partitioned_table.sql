CREATE OR REPLACE FUNCTION dml_utils_lib.assert_partitioned_table(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_relkind text;
BEGIN
    -- Resolve the relation with the schema qualified explicitly, so the result
    -- does not depend on the caller's search_path.
    SELECT c.relkind::text
    INTO l_relkind
    FROM pg_catalog.pg_class AS c
             JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = i_schema_name
      AND c.relname = i_table_name;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'relation %.% does not exist', i_schema_name, i_table_name
            USING ERRCODE = '42P01';
    END IF;

    -- Partition-wise chunking walks the partition tree, so the driving table
    -- must be the declarative parent (relkind 'p'). A plain table has no leaves
    -- to enumerate; a view, matview or foreign table has no partition tree.
    IF l_relkind <> 'p' THEN
        RAISE EXCEPTION 'partition-wise chunking requires a partitioned table, but %.% has relkind %',
            i_schema_name, i_table_name, l_relkind
            USING ERRCODE = '42809';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_partitioned_table(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) IS
    'Raises undefined_table (42P01) when the relation does not exist, or '
        'wrong_object_type (42809) when it is not a declarative partitioned '
        'table (relkind p), so a non-partitioned driving table is rejected '
        'before any leaf is enumerated.';
