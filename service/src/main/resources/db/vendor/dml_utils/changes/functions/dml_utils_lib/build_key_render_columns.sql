CREATE OR REPLACE FUNCTION dml_utils_lib.build_key_render_columns(
    i_table_alias dml_utils_data.non_null_text,
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_key_columns name[],
    i_key_kinds text[],
    OUT o_column_names text[],
    OUT o_key_collations text[]
)
    RETURNS record
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_column_count integer;
    l_kind         text;
    l_collation    text;
BEGIN
    -- The key columns and kinds must be one-dimensional, 1-based and the same
    -- non-empty length. A NULL or empty columns array makes array_length NULL,
    -- which the length comparison below rejects.
    IF pg_catalog.array_ndims(i_key_columns) IS DISTINCT FROM 1
        OR pg_catalog.array_lower(i_key_columns, 1) IS DISTINCT FROM 1
        OR pg_catalog.array_ndims(i_key_kinds) IS DISTINCT FROM 1
        OR pg_catalog.array_lower(i_key_kinds, 1) IS DISTINCT FROM 1
        OR pg_catalog.array_length(i_key_columns, 1) IS NULL
        OR pg_catalog.array_length(i_key_columns, 1) IS DISTINCT FROM pg_catalog.array_length(i_key_kinds, 1)
    THEN
        RAISE EXCEPTION 'key columns and kinds must be one-dimensional, 1-based, same-length and non-empty'
            USING ERRCODE = '22023';
    END IF;

    l_column_count := pg_catalog.array_length(i_key_columns, 1);
    o_column_names := ARRAY []::text[];
    o_key_collations := ARRAY []::text[];
    FOR l_position IN 1..l_column_count
        LOOP
            l_kind := i_key_kinds[l_position];

            -- A NULL kind would slip past NOT IN (NULL NOT IN (...) is NULL,
            -- not true), so guard it explicitly.
            IF l_kind IS NULL OR l_kind NOT IN ('bigint', 'text', 'uuid', 'timestamptz', 'bytea') THEN
                RAISE EXCEPTION 'unsupported key kind %', l_kind
                    USING ERRCODE = '22023';
            END IF;

            -- A NULL or empty column name would render a degenerate %I token and
            -- only fail later when a worker parses the chunk SQL; reject it here.
            IF i_key_columns[l_position] IS NULL
                OR i_key_columns[l_position] = ''
            THEN
                RAISE EXCEPTION 'key column name must not be NULL or empty at position %',
                    l_position
                    USING ERRCODE = '22023';
            END IF;

            o_column_names := o_column_names || pg_catalog.format('%I.%I',
                                                                  i_table_alias,
                                                                  i_key_columns[l_position]);

            -- Precompute the text column's collation once (the other kinds do not
            -- order by collation). NULL means the column is not in the catalog (a
            -- renderer unit test with a synthetic table), so
            -- render_prepared_chunk_sql falls back to the default operator;
            -- 'default' is the default collation; any other value is the quoted
            -- collation name.
            IF l_kind = 'text' THEN
                l_collation := NULL;
                SELECT CASE
                           WHEN a.attcollation = 0 THEN 'default'
                           ELSE pg_catalog.quote_ident(c.collname)
                           END
                INTO l_collation
                FROM pg_catalog.pg_attribute AS a
                         LEFT JOIN pg_catalog.pg_collation AS c
                                   ON c.oid = a.attcollation
                WHERE a.attrelid = pg_catalog.to_regclass(
                        pg_catalog.format('%I.%I', i_schema_name, i_table_name))
                  AND a.attname = i_key_columns[l_position];
                -- array_append, not `||`: concatenating a NULL element keeps the
                -- array aligned by position (a plain `array || NULL` would not add
                -- an element).
                o_key_collations := pg_catalog.array_append(o_key_collations, l_collation);
            ELSE
                o_key_collations := pg_catalog.array_append(o_key_collations, NULL);
            END IF;
        END LOOP;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.build_key_render_columns(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[]
    ) IS
    'Validates a chunk render key and returns the alias-qualified quoted column '
        'names plus the collation to use for each text column (NULL when the '
        'column is not in the catalog). Shared by prepare_chunk_sql_render so the '
        'render context is built once per run.';
