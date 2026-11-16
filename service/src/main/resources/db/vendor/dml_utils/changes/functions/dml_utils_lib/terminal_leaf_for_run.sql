CREATE OR REPLACE FUNCTION dml_utils_lib.terminal_leaf_for_run(
    i_run_id bigint,
    OUT o_leaf_ordinal integer,
    OUT o_leaf_schema_name text,
    OUT o_leaf_name text,
    OUT o_bigint_values bigint[],
    OUT o_text_values text[],
    OUT o_uuid_values uuid[],
    OUT o_timestamptz_values timestamptz[],
    OUT o_bytea_values bytea[]
)
    RETURNS record
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
BEGIN
    IF i_run_id IS NULL THEN
        RAISE EXCEPTION 'i_run_id must not be NULL'
            USING ERRCODE = '22023';
    END IF;

    -- Distinguish an unknown run and a run without a manifest from the
    -- legitimate "every leaf is empty" result (all-NULL OUT fields).
    IF NOT EXISTS (SELECT 1
                   FROM dml_utils_data.migration_run AS run
                   WHERE run.run_id = i_run_id)
    THEN
        RAISE EXCEPTION 'migration run % does not exist', i_run_id
            USING ERRCODE = 'P0002';
    END IF;
    IF NOT EXISTS (SELECT 1
                   FROM dml_utils_data.migration_run_leaf AS leaf
                   WHERE leaf.run_id = i_run_id)
    THEN
        RAISE EXCEPTION 'run % has no leaf manifest', i_run_id
            USING ERRCODE = '55000';
    END IF;

    -- The terminal belongs to the last non-empty leaf in manifest order. An
    -- empty leaf (boundary_count = 0) is calculated but has no chunk to end.
    SELECT leaf.leaf_ordinal,
           leaf.leaf_schema_name::text,
           leaf.leaf_name::text,
           (leaf.terminal_boundary_id).bigint_values,
           (leaf.terminal_boundary_id).text_values,
           (leaf.terminal_boundary_id).uuid_values,
           (leaf.terminal_boundary_id).timestamptz_values,
           (leaf.terminal_boundary_id).bytea_values
    INTO o_leaf_ordinal, o_leaf_schema_name, o_leaf_name,
        o_bigint_values, o_text_values, o_uuid_values, o_timestamptz_values,
        o_bytea_values
    FROM dml_utils_data.migration_run_leaf AS leaf
    WHERE leaf.run_id = i_run_id
      AND leaf.boundary_count > 0
    ORDER BY leaf.leaf_ordinal DESC
    LIMIT 1;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.terminal_leaf_for_run(bigint) IS
    'Returns the last non-empty manifest leaf of a partition-wise run with its '
        'stored high-water key arrays, or NULL fields when every leaf is empty. '
        'Raises P0002 for an unknown run and 55000 for a run with no leaf '
        'manifest, so an empty run is not confused with a missing one. Used by '
        'finalize_migration_boundaries to choose the single terminal boundary.';
