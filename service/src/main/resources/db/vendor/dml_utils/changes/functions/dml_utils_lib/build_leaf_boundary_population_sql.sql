CREATE OR REPLACE FUNCTION dml_utils_lib.build_leaf_boundary_population_sql(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_key_columns name[],
    i_key_kinds text[],
    i_run_id bigint,
    i_chunk_size bigint,
    i_leaf_ordinal integer
)
    RETURNS text
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_descriptor record;
BEGIN
    -- The run id, chunk size and leaf ordinal are inlined (all integers, so no
    -- injection surface). A NULL run id renders an empty token, a non-positive
    -- chunk size renders a degenerate divisor and an ordinal below 1 violates
    -- the manifest key.
    IF i_run_id IS NULL OR i_chunk_size IS NULL OR i_chunk_size <= 0 THEN
        RAISE EXCEPTION 'run id must not be NULL and chunk size must be positive'
            USING ERRCODE = '22023';
    END IF;
    IF i_leaf_ordinal IS NULL OR i_leaf_ordinal < 1 THEN
        RAISE EXCEPTION 'leaf ordinal must be at least 1'
            USING ERRCODE = '22023';
    END IF;

    SELECT o_select_list,
           o_id_list,
           o_order_by,
           o_order_by_desc,
           o_pack_expression
    INTO l_descriptor
    FROM dml_utils_lib.boundary_key_descriptor_sql(
            i_key_columns => i_key_columns,
            i_key_kinds => i_key_kinds);

    -- One leaf's chunk starts, appended after every boundary already committed
    -- for the run. The base number comes from the existing rows (the snapshot
    -- before this statement), so concurrent-looking appends stay contiguous.
    -- There is deliberately no per-leaf terminal: the only terminal boundary is
    -- written by finalize_migration_boundaries from the last non-empty leaf.
    RETURN pg_catalog.format(
            $chunk$
        INSERT INTO dml_utils_data.migration_boundary
            (run_id, boundary_no, boundary_id, leaf_ordinal, completed_at)
        WITH numbered AS (
            SELECT
                %1$s,
                row_number() OVER (ORDER BY %2$s) AS rn
            FROM %3$I.%4$I
        ),
        starts AS MATERIALIZED (
            SELECT
                %5$s,
                (rn - 1) / %8$s::bigint AS leaf_boundary_no
            FROM numbered
            WHERE (rn - 1) %% %8$s::bigint = 0
        ),
        base AS (
            SELECT coalesce(max(boundary_no), -1) + 1 AS first_boundary_no
            FROM dml_utils_data.migration_boundary
            WHERE run_id = %7$s
        )
        SELECT
            %7$s,
            base.first_boundary_no + starts.leaf_boundary_no,
            %6$s AS boundary_id,
            %9$s,
            NULL::timestamptz
        FROM starts
                 CROSS JOIN base
        $chunk$,
            l_descriptor.o_select_list,
            l_descriptor.o_order_by,
            i_schema_name,
            i_table_name,
            l_descriptor.o_id_list,
            l_descriptor.o_pack_expression,
            i_run_id,
            i_chunk_size,
            i_leaf_ordinal);
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.build_leaf_boundary_population_sql(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    bigint,
    bigint,
    integer
    ) IS
    'Returns the statement that scans one leaf in boundary-key order and inserts '
        'its chunk starts (no per-leaf terminal) after the boundaries already '
        'committed for the run, tagged with the leaf ordinal. The caller supplies '
        'the key columns/kinds resolved once from the parent.';
