CREATE OR REPLACE FUNCTION dml_utils_lib.assert_plain_heap(
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

    -- Block chunking needs a single physical heap: relkind 'r' is an ordinary
    -- table. 'p' is a partitioned table (no storage of its own, so ctid is
    -- per-partition) and 'f' is a foreign table; neither has a heap whose blocks
    -- could be chunked. 'm' (matview) and 'v' (view) are likewise unsupported.
    IF l_relkind <> 'r' THEN
        RAISE EXCEPTION 'block chunking requires a plain table, but %.% has relkind %',
            i_schema_name, i_table_name, l_relkind
            USING ERRCODE = '42809';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_plain_heap IS
    'Raises undefined_table (42P01) when the relation does not exist, or '
        'wrong_object_type (42809) when it is not a plain table (relkind r), so '
        'block chunking is rejected for partitioned, foreign, view and matview '
        'relations that have no single physical heap.';
