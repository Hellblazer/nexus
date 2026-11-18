CREATE OR REPLACE FUNCTION dml_utils_lib.format_boundary_population_sql(
    i_select_list text,
    i_order_by text,
    i_schema_name text,
    i_table_name text,
    i_id_list text,
    i_pack_expression text,
    i_order_by_desc text,
    i_run_id bigint,
    i_chunk_size bigint
)
    RETURNS text
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
BEGIN
    -- Boundaries are picked by row position, so the computation never needs
    -- min()/max() on the key: PostgreSQL defines those aggregates for the integer
    -- and text types but not for uuid.
    --
    -- The shape matters on production-sized tables:
    --   * numbered is referenced once, so it is inlined and the row_number()
    --     WindowAgg streams over the boundary-key index instead of sorting or
    --     materialising all rows.
    --   * count(*) OVER () is deliberately not used: a full-partition window
    --     aggregate buffers every row (spilling to disk for large tables) just to
    --     recover the row count. The terminal high-water boundary instead comes
    --     from a backward index scan (ORDER BY the key DESC LIMIT 1) and its
    --     boundary_no is the last chunk start plus one.
    --   * only the (N / chunk_size) start rows are materialised.
    RETURN pg_catalog.format(
            $chunk$
        INSERT INTO dml_utils_data.migration_boundary (run_id, boundary_no, boundary_id, completed_at)
        WITH numbered AS (
            SELECT
                %1$s,
                row_number() OVER (ORDER BY %2$s) AS rn
            FROM %3$I.%4$I
        ),
        starts AS MATERIALIZED (
            SELECT
                %5$s,
                (rn - 1) / %9$s::bigint AS boundary_no
            FROM numbered
            WHERE (rn - 1) %% %9$s::bigint = 0
        )
        SELECT
            %8$s,
            boundary_no,
            %6$s AS boundary_id,
            NULL::timestamptz AS completed_at
        FROM starts
        UNION ALL
        SELECT
            %8$s,
            (SELECT max(boundary_no) + 1 FROM starts),
            terminal.boundary_id,
            NULL::timestamptz AS completed_at
        FROM (
            SELECT
                %6$s AS boundary_id
            FROM (
                SELECT %1$s
                FROM %3$I.%4$I
                ORDER BY %7$s
                LIMIT 1
            ) AS last_row
        ) AS terminal
        $chunk$,
            i_select_list,
            i_order_by,
            i_schema_name,
            i_table_name,
            i_id_list,
            i_pack_expression,
            i_order_by_desc,
            i_run_id,
            i_chunk_size);
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.format_boundary_population_sql(
    text,
    text,
    text,
    text,
    text,
    text,
    text,
    bigint,
    bigint
    ) IS
    'Returns the single-relation boundary-population statement from the key '
        'descriptor fragments. The statement streams the boundary key in index '
        'order, materialises only the chunk starts and takes the terminal from a '
        'backward index scan. Shared by build_boundary_population_sql so execute '
        'and explain run the identical SQL.';
