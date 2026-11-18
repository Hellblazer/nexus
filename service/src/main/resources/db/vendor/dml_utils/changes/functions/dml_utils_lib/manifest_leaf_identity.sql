CREATE OR REPLACE FUNCTION dml_utils_lib.manifest_leaf_identity(
    i_run_id bigint,
    i_leaf_ordinal integer,
    OUT o_chunk_by dml_utils_data.chunking_strategy,
    OUT o_leaf_schema_name text,
    OUT o_leaf_name text,
    OUT o_leaf_oid oid,
    OUT o_leaf_bound text,
    OUT o_relation_filepath text
)
    RETURNS record
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
BEGIN
    SELECT run.chunk_by,
           leaf.leaf_schema_name::text,
           leaf.leaf_name::text,
           leaf.leaf_oid,
           leaf.leaf_bound,
           leaf.relation_filepath
    INTO o_chunk_by,
        o_leaf_schema_name,
        o_leaf_name,
        o_leaf_oid,
        o_leaf_bound,
        o_relation_filepath
    FROM dml_utils_data.migration_run AS run
             JOIN dml_utils_data.migration_run_leaf AS leaf ON leaf.run_id = run.run_id
    WHERE run.run_id = i_run_id
      AND leaf.leaf_ordinal = i_leaf_ordinal;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'leaf ordinal % does not belong to run %', i_leaf_ordinal, i_run_id
            USING ERRCODE = '55000';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.manifest_leaf_identity(
    bigint,
    integer
    ) IS
    'Returns the stored identity of one partition-wise manifest leaf: chunk '
        'strategy, schema, name, OID, partition bound and blocks filepath. A '
        'missing run/leaf is object_not_in_prerequisite_state (55000).';
