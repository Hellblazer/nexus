CREATE OR REPLACE FUNCTION dml_utils_lib.assert_live_leaf_matches_manifest(
    i_chunk_by dml_utils_data.chunking_strategy,
    i_leaf_schema_name text,
    i_leaf_name text,
    i_leaf_oid oid,
    i_leaf_bound text,
    i_relation_filepath text
)
    RETURNS void
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_live_oid      oid;
    l_live_relkind  text;
    l_live_bound    text;
    l_live_filepath text;
BEGIN
    SELECT c.oid,
           c.relkind::text,
           pg_catalog.pg_get_expr(c.relpartbound, c.oid),
           pg_catalog.pg_relation_filepath(c.oid)
    INTO l_live_oid,
        l_live_relkind,
        l_live_bound,
        l_live_filepath
    FROM pg_catalog.pg_class AS c
             JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = i_leaf_schema_name
      AND c.relname = i_leaf_name;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'leaf %.% no longer exists', i_leaf_schema_name, i_leaf_name
            USING ERRCODE = '55000';
    END IF;

    -- A dropped and recreated leaf or a rename keeps the name but changes the
    -- OID; a detach clears relpartbound. Either makes the stored ranges stale.
    IF l_live_oid IS DISTINCT FROM i_leaf_oid THEN
        RAISE EXCEPTION 'leaf %.% was replaced (stored oid %, live oid %)',
            i_leaf_schema_name, i_leaf_name, i_leaf_oid, l_live_oid
            USING ERRCODE = '55000';
    END IF;
    IF l_live_relkind <> 'r' THEN
        RAISE EXCEPTION 'leaf %.% is no longer an ordinary table (relkind %)',
            i_leaf_schema_name, i_leaf_name, l_live_relkind
            USING ERRCODE = '55000';
    END IF;
    IF l_live_bound IS DISTINCT FROM i_leaf_bound THEN
        RAISE EXCEPTION 'leaf %.% partition bound changed (stored %, live %)',
            i_leaf_schema_name, i_leaf_name, i_leaf_bound, l_live_bound
            USING ERRCODE = '55000';
    END IF;

    -- A blocks run addresses the heap by ctid, so a rewrite (VACUUM FULL,
    -- CLUSTER, pg_repack, tablespace move) invalidates its ranges. A key run
    -- never records a per-leaf filepath.
    IF i_chunk_by = 'blocks'
        AND l_live_filepath IS DISTINCT FROM i_relation_filepath
    THEN
        RAISE EXCEPTION 'leaf %.% heap changed (stored filepath %, live filepath %)',
            i_leaf_schema_name, i_leaf_name, i_relation_filepath, l_live_filepath
            USING ERRCODE = '55000';
    END IF;
    IF i_chunk_by <> 'blocks' AND i_relation_filepath IS NOT NULL THEN
        RAISE EXCEPTION 'leaf %.% records a filepath for a key-strategy run',
            i_leaf_schema_name, i_leaf_name
            USING ERRCODE = '55000';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_live_leaf_matches_manifest(
    dml_utils_data.chunking_strategy,
    text,
    text,
    oid,
    text,
    text
    ) IS
    'Compares one manifest leaf identity with the live relation and raises '
        'object_not_in_prerequisite_state (55000) when it is missing, replaced '
        '(different OID), detached or rebound (different partition bound), not a '
        'plain table, or (for blocks) a rewritten heap.';
