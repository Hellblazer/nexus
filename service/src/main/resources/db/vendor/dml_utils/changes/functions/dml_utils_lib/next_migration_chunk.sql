CREATE OR REPLACE FUNCTION dml_utils_lib.next_migration_chunk(
    i_run_id bigint,
    i_partition_wise boolean,
    i_key_kinds text[],
    i_in_flight_boundaries bigint[],
    i_default_schema_name text,
    i_default_table_name text,
    OUT o_boundary_no bigint,
    OUT o_chunk_schema_name text,
    OUT o_chunk_table_name text,
    OUT o_start_values text[],
    OUT o_end_values text[],
    OUT o_is_final boolean
)
    RETURNS record
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_start_leaf_ordinal   integer;
    l_end_leaf_ordinal     integer;
    l_terminal_bigint      bigint[];
    l_terminal_text        text[];
    l_terminal_uuid        uuid[];
    l_terminal_timestamptz timestamptz[];
    l_terminal_bytea       bytea[];
    l_terminal_key         dml_utils_data.migration_key;
BEGIN
    -- Extract the per-position key values of the start and end boundaries in one
    -- call. Each kind populates a different position-aligned array of
    -- migration_key; the helper flattens both back into a value list each, in key
    -- order.
    SELECT b.boundary_no,
           b.leaf_ordinal,
           next.leaf_ordinal,
           leaf.leaf_schema_name::text,
           leaf.leaf_name::text,
           (leaf.terminal_boundary_id).bigint_values,
           (leaf.terminal_boundary_id).text_values,
           (leaf.terminal_boundary_id).uuid_values,
           (leaf.terminal_boundary_id).timestamptz_values,
           (leaf.terminal_boundary_id).bytea_values,
           pair.o_start_values,
           pair.o_end_values,
           next.boundary_no = last.boundary_no
    INTO o_boundary_no, l_start_leaf_ordinal, l_end_leaf_ordinal,
        o_chunk_schema_name, o_chunk_table_name, l_terminal_bigint,
        l_terminal_text, l_terminal_uuid, l_terminal_timestamptz, l_terminal_bytea,
        o_start_values, o_end_values, o_is_final
    FROM dml_utils_data.migration_boundary AS b
             JOIN dml_utils_data.migration_boundary AS next
                  ON next.run_id = b.run_id
                      AND next.boundary_no = b.boundary_no + 1
        -- The leaf row is immutable once the run is finalized, so it rides along
        -- with the boundary instead of a second lookup per launch.
             LEFT JOIN dml_utils_data.migration_run_leaf AS leaf
                       ON leaf.run_id = b.run_id
                           AND leaf.leaf_ordinal = b.leaf_ordinal
             CROSS JOIN LATERAL (
        SELECT max(boundary_no) AS boundary_no
        FROM dml_utils_data.migration_boundary
        WHERE run_id = b.run_id
        ) AS last
             CROSS JOIN LATERAL dml_utils_lib.migration_key_pair_values(
            i_start_key => b.boundary_id,
            i_end_key => next.boundary_id,
            i_key_kinds => i_key_kinds) AS pair
    WHERE b.run_id = i_run_id
      AND b.completed_at IS NULL
      AND b.cancelled_at IS NULL
      AND b.boundary_no <> ALL (i_in_flight_boundaries)
    ORDER BY b.boundary_no
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    IF i_partition_wise THEN
        IF o_chunk_schema_name IS NULL THEN
            RAISE EXCEPTION 'partition tree changed for run %: leaf ordinal % is missing from the manifest',
                i_run_id, l_start_leaf_ordinal
                USING ERRCODE = '55000';
        END IF;

        -- The next boundary belongs to a different leaf, so this is the last
        -- chunk of this leaf and the next leaf's start is not a valid upper
        -- bound for it. End it at the leaf's calculation-time high-water
        -- snapshot and make the bound inclusive, so deletes since calculation
        -- cannot regress it.
        IF l_end_leaf_ordinal IS DISTINCT FROM l_start_leaf_ordinal THEN
            l_terminal_key := ROW (l_terminal_bigint, l_terminal_text,
                l_terminal_uuid,
                l_terminal_timestamptz, l_terminal_bytea)::dml_utils_data.migration_key;
            IF l_terminal_key IS NULL THEN
                RAISE EXCEPTION 'leaf ordinal % of run % has no terminal snapshot',
                    l_start_leaf_ordinal, i_run_id
                    USING ERRCODE = '55000';
            END IF;
            o_end_values := dml_utils_lib.migration_key_values(
                    i_key => l_terminal_key,
                    i_key_kinds => i_key_kinds);
            o_is_final := true;
        END IF;
    ELSE
        o_chunk_schema_name := i_default_schema_name;
        o_chunk_table_name := i_default_table_name;
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.next_migration_chunk(
    bigint,
    boolean,
    text[],
    bigint[],
    text,
    text
    ) IS
    'Selects the next unclaimed boundary for a run (not in flight), extracts its '
        'start and end keys and, for a partition-wise run, ends the last chunk of '
        'each leaf at the manifest high-water snapshot with an inclusive bound. '
        'Returns no boundary number when nothing is left to launch. A missing '
        'leaf or terminal snapshot raises 55000.';
