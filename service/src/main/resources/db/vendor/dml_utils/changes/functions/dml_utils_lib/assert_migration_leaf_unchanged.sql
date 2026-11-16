CREATE OR REPLACE FUNCTION dml_utils_lib.assert_migration_leaf_unchanged(
    i_run_id bigint,
    i_leaf_ordinal integer
)
    RETURNS void
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_leaf record;
BEGIN
    SELECT o_chunk_by,
           o_leaf_schema_name,
           o_leaf_name,
           o_leaf_oid,
           o_leaf_bound,
           o_relation_filepath
    INTO l_leaf
    FROM dml_utils_lib.manifest_leaf_identity(
            i_run_id => i_run_id,
            i_leaf_ordinal => i_leaf_ordinal);

    PERFORM dml_utils_lib.assert_live_leaf_matches_manifest(
            i_chunk_by => l_leaf.o_chunk_by,
            i_leaf_schema_name => l_leaf.o_leaf_schema_name,
            i_leaf_name => l_leaf.o_leaf_name,
            i_leaf_oid => l_leaf.o_leaf_oid,
            i_leaf_bound => l_leaf.o_leaf_bound,
            i_relation_filepath => l_leaf.o_relation_filepath);
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_migration_leaf_unchanged(
    bigint,
    integer
    ) IS
    'Raises object_not_in_prerequisite_state (55000) when a partition-wise run''s '
        'manifest leaf no longer matches the live relation: missing, replaced '
        '(different OID), detached or rebound (different partition bound), not a '
        'plain table, or (for blocks) a rewritten heap. Uses '
        'manifest_leaf_identity and assert_live_leaf_matches_manifest so the '
        'stored and live views are each built in one place.';
