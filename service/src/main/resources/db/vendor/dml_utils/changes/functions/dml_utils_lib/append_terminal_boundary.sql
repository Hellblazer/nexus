CREATE OR REPLACE FUNCTION dml_utils_lib.append_terminal_boundary(
    i_run_id bigint,
    i_leaf_ordinal integer,
    i_leaf_schema_name text,
    i_leaf_name text,
    i_terminal_key dml_utils_data.migration_key
)
    RETURNS bigint
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS
$$
DECLARE
    l_caller      name;
    l_boundary_no bigint;
BEGIN
    -- The terminal scan runs in the definer context, so the effective caller
    -- must still be allowed to read the leaf, as for leaf calculation.
    l_caller := COALESCE(NULLIF(pg_catalog.current_setting('role', true), 'none'),
                         session_user)::name;
    IF NOT pg_catalog.has_table_privilege(
            l_caller,
            pg_catalog.format('%I.%I', i_leaf_schema_name, i_leaf_name),
            'SELECT')
    THEN
        RAISE EXCEPTION 'caller lacks SELECT privilege on leaf %.%',
            i_leaf_schema_name, i_leaf_name
            USING ERRCODE = '42501';
    END IF;

    -- Re-check the terminal leaf: the tree may have changed since its
    -- calculation (or since the last leaf committed).
    PERFORM dml_utils_lib.assert_migration_leaf_unchanged(
            i_run_id => i_run_id,
            i_leaf_ordinal => i_leaf_ordinal);

    IF i_terminal_key IS NULL THEN
        RAISE EXCEPTION 'leaf % of run % has no terminal snapshot',
            i_leaf_ordinal, i_run_id
            USING ERRCODE = '55000';
    END IF;

    SELECT coalesce(max(boundary.boundary_no), -1) + 1
    INTO l_boundary_no
    FROM dml_utils_data.migration_boundary AS boundary
    WHERE boundary.run_id = i_run_id;

    INSERT INTO dml_utils_data.migration_boundary
        (run_id, boundary_no, boundary_id, leaf_ordinal, completed_at)
    VALUES (i_run_id, l_boundary_no, i_terminal_key, i_leaf_ordinal, NULL);

    -- The terminal is not a chunk, so the chunk count is its boundary_no.
    RETURN l_boundary_no;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.append_terminal_boundary(
    bigint,
    integer,
    text,
    text,
    dml_utils_data.migration_key
    ) IS
    'Appends the single terminal boundary for a partition-wise run after the '
        'last committed boundary, tagged with the terminal leaf, and returns its '
        'boundary number (the run''s chunk count). Re-checks the caller''s SELECT '
        'privilege and the terminal leaf identity, and rejects a missing snapshot '
        'with 55000.';
