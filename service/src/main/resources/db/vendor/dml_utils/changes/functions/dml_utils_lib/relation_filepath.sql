CREATE OR REPLACE FUNCTION dml_utils_lib.relation_filepath(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text
)
    RETURNS text
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_relation regclass;
    l_relkind  text;
    l_filepath text;
BEGIN
    -- Resolve the relation with the schema qualified explicitly, so the result
    -- does not depend on the caller's search_path.
    l_relation := pg_catalog.to_regclass(pg_catalog.format('%I.%I', i_schema_name, i_table_name));

    IF l_relation IS NULL THEN
        RAISE EXCEPTION 'relation %.% does not exist', i_schema_name, i_table_name
            USING ERRCODE = '42P01';
    END IF;

    -- pg_relation_filepath returns NULL for a relation without storage of its
    -- own (partitioned table, view, matview, foreign table). Returning that NULL
    -- would make assert_relation_filepath treat the run as a primary-key run and
    -- silently skip the heap-rewrite guard, so reject those kinds instead.
    SELECT c.relkind::text
    INTO l_relkind
    FROM pg_catalog.pg_class AS c
    WHERE c.oid = l_relation;

    IF l_relkind <> 'r' THEN
        RAISE EXCEPTION 'relation %.% has no physical heap (relkind %)',
            i_schema_name, i_table_name, l_relkind
            USING ERRCODE = '42809';
    END IF;

    -- pg_relation_filepath returns the path relative to the data directory (for
    -- example base/16384/12345). It identifies the physical file, so it changes
    -- when the heap is rewritten (VACUUM FULL, CLUSTER, pg_repack, a tablespace
    -- move) and is the fingerprint a block run records and re-checks on resume.
    l_filepath := pg_catalog.pg_relation_filepath(l_relation);

    RETURN l_filepath;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.relation_filepath IS
    'Returns the pg_relation_filepath of the given plain table (its physical '
        'file, relative to the data directory), or raises undefined_table (42P01) '
        'when it does not exist and wrong_object_type (42809) when the relation '
        'has no heap of its own (partitioned table, view, matview, foreign table). '
        'Used as the physical fingerprint of a block-chunked run: the filepath '
        'changes when the heap is rewritten.';
