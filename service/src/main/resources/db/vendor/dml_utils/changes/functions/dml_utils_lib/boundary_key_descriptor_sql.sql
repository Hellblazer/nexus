CREATE OR REPLACE FUNCTION dml_utils_lib.boundary_key_descriptor_sql(
    i_key_columns name[],
    i_key_kinds text[]
)
    RETURNS TABLE
            (
                o_select_list     text,
                o_id_list         text,
                o_order_by        text,
                o_order_by_desc   text,
                o_pack_expression text
            )
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_key_select_list text;
    l_key_id_list     text;
    l_order_by        text;
    l_order_by_desc   text;
    l_pack_expression text;
BEGIN
    -- The boundary-key columns and kinds must be one-dimensional, 1-based and
    -- the same non-empty length of one to three. A different lower bound, a
    -- multidimensional array, an empty array or a length mismatch would index
    -- the wrong element or render a degenerate statement.
    IF pg_catalog.array_ndims(i_key_columns) IS DISTINCT FROM 1
        OR pg_catalog.array_lower(i_key_columns, 1) IS DISTINCT FROM 1
        OR pg_catalog.array_ndims(i_key_kinds) IS DISTINCT FROM 1
        OR pg_catalog.array_lower(i_key_kinds, 1) IS DISTINCT FROM 1
        OR pg_catalog.cardinality(i_key_kinds) NOT BETWEEN 1 AND 3
        OR pg_catalog.cardinality(i_key_columns) IS DISTINCT FROM pg_catalog.cardinality(i_key_kinds)
    THEN
        RAISE EXCEPTION 'boundary-key columns and kinds must be one-dimensional, 1-based and of the same non-empty length of one to three'
            USING ERRCODE = '22023';
    END IF;

    -- Build, position by position: the SELECT list that aliases each key column
    -- as id1, id2, ..., the matching id list and the ORDER BY. All identifiers
    -- are catalog names quoted with %I/quote_ident.
    l_key_select_list := '';
    l_key_id_list := '';
    l_order_by := '';
    l_order_by_desc := '';
    FOR l_position IN 1..pg_catalog.array_length(i_key_kinds, 1)
        LOOP
        -- A NULL or empty column name would render a degenerate %I token;
        -- reject it before building the statement. The kind itself is
        -- validated by boundary_key_pack_expression below.
            IF i_key_columns[l_position] IS NULL
                OR i_key_columns[l_position] = ''
            THEN
                RAISE EXCEPTION 'boundary-key column name must not be NULL or empty at position %',
                    l_position
                    USING ERRCODE = '22023';
            END IF;

            -- A repeated column renders a non-unique boundary key (the same
            -- column aliased and ordered twice), so reject it here where the
            -- key contract is enforced.
            IF pg_catalog.array_position(i_key_columns, i_key_columns[l_position]) IS DISTINCT FROM l_position
            THEN
                RAISE EXCEPTION 'boundary-key column % is repeated at position %',
                    i_key_columns[l_position], l_position
                    USING ERRCODE = '22023';
            END IF;

            l_key_select_list := l_key_select_list || pg_catalog.format('%s%s AS id%s',
                                                                        CASE WHEN l_position > 1 THEN ', ' ELSE '' END,
                                                                        pg_catalog.quote_ident(i_key_columns[l_position]),
                                                                        l_position);
            l_key_id_list := l_key_id_list || pg_catalog.format('%sid%s',
                                                                CASE WHEN l_position > 1 THEN ', ' ELSE '' END,
                                                                l_position);
            l_order_by := l_order_by || pg_catalog.format('%s%s',
                                                          CASE WHEN l_position > 1 THEN ', ' ELSE '' END,
                                                          pg_catalog.quote_ident(i_key_columns[l_position]));
            -- DESC applies per ORDER BY term, so the backward scan needs it on
            -- every column (a trailing DESC would leave the earlier columns ASC).
            -- Order by the SELECT's idN aliases, not the raw column names: this
            -- is a top-level ORDER BY over the aliased select, and PostgreSQL
            -- prefers an output alias over an input column, so a key column
            -- literally named id1 at another position would otherwise resolve
            -- to the wrong column. The aliases are the key columns in key order.
            l_order_by_desc := l_order_by_desc || pg_catalog.format('%sid%s DESC',
                                                                    CASE WHEN l_position > 1 THEN ', ' ELSE '' END,
                                                                    l_position);
        END LOOP;

    l_pack_expression := dml_utils_lib.boundary_key_pack_expression(
            i_key_kinds => i_key_kinds);

    RETURN QUERY
        SELECT l_key_select_list,
               l_key_id_list,
               l_order_by,
               l_order_by_desc,
               l_pack_expression;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.boundary_key_descriptor_sql(
    name[],
    text[]
    ) IS
    'Validates a one-to-three-column boundary key and returns the SQL fragments '
        'that read, order and pack it (idN aliases, ascending ORDER BY, descending '
        'ORDER BY and the migration_key row expression from '
        'boundary_key_pack_expression). Shared by the single-relation and per-leaf '
        'boundary builders so both render the same key logic.';
