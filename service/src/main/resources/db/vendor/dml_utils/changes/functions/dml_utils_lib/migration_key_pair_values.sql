CREATE OR REPLACE FUNCTION dml_utils_lib.migration_key_pair_values(
    i_start_key dml_utils_data.migration_key,
    i_end_key dml_utils_data.migration_key,
    i_key_kinds text[],
    OUT o_start_values text[],
    OUT o_end_values text[]
)
    RETURNS record
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_kind  text;
    l_start text;
    l_end   text;
BEGIN
    -- Flatten both boundary keys of a chunk in one pass: one text value per
    -- boundary-key column for the start and the end, in key order. Reject a NULL,
    -- empty, over-long, non-1-dimensional or non-1-based kinds array, as
    -- migration_key_values does, since indexing a non-1-based or multidimensional
    -- array would read a slice or an out-of-range subscript.
    IF i_key_kinds IS NULL
        OR pg_catalog.array_ndims(i_key_kinds) IS DISTINCT FROM 1
        OR pg_catalog.array_lower(i_key_kinds, 1) IS DISTINCT FROM 1
        OR pg_catalog.cardinality(i_key_kinds) NOT BETWEEN 1 AND 3
    THEN
        RAISE EXCEPTION 'key kinds must be a one-dimensional, 1-based array of one to three entries'
            USING ERRCODE = '22023';
    END IF;

    o_start_values := ARRAY []::text[];
    o_end_values := ARRAY []::text[];
    FOR l_position IN 1..pg_catalog.cardinality(i_key_kinds)
        LOOP
            l_kind := i_key_kinds[l_position];

            -- A NULL kind would make every comparison below NULL (not true) and
            -- fall through to a NULL branch; reject it explicitly.
            IF l_kind IS NULL OR l_kind NOT IN ('bigint', 'text', 'uuid', 'timestamptz', 'bytea') THEN
                RAISE EXCEPTION 'unsupported key kind %', l_kind
                    USING ERRCODE = '22023';
            END IF;

            -- Exactly one value array is populated per position; an absent array
            -- or a NULL element yields a NULL here, which keeps the result
            -- position-aligned with the key instead of shifting later values.
            -- render_chunk_sql rejects the NULL when it renders the predicate.
            CASE l_kind
                WHEN 'bigint' THEN l_start := (i_start_key).bigint_values[l_position]::text;
                                   l_end := (i_end_key).bigint_values[l_position]::text;
                WHEN 'text' THEN l_start := (i_start_key).text_values[l_position];
                                 l_end := (i_end_key).text_values[l_position];
                WHEN 'uuid' THEN l_start := (i_start_key).uuid_values[l_position]::text;
                                 l_end := (i_end_key).uuid_values[l_position]::text;
                WHEN 'timestamptz' THEN l_start := (i_start_key).timestamptz_values[l_position]::text;
                                        l_end := (i_end_key).timestamptz_values[l_position]::text;
                -- Hex digits, so the text does not depend on bytea_output.
                WHEN 'bytea' THEN l_start := pg_catalog.encode((i_start_key).bytea_values[l_position], 'hex');
                                  l_end := pg_catalog.encode((i_end_key).bytea_values[l_position], 'hex');
                END CASE;

            o_start_values := o_start_values || l_start;
            o_end_values := o_end_values || l_end;
        END LOOP;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.migration_key_pair_values IS
    'Flattens a chunk''s two position-aligned migration_key boundaries (start and '
        'end) into one text value per boundary-key column each, in key order, using '
        'the key kinds from primary_key_kinds. One call serves both bounds where '
        'migration_key_values would be called twice.';
