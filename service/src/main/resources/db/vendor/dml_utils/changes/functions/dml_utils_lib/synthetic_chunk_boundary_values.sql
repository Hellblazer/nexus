CREATE OR REPLACE FUNCTION dml_utils_lib.synthetic_chunk_boundary_values(
    i_key_kinds text[],
    i_chunk_size bigint
)
    RETURNS TABLE
            (
                o_is_final     boolean,
                o_start_values text[],
                o_end_values   text[]
            )
    LANGUAGE plpgsql
    IMMUTABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_start_values text[] := ARRAY []::text[];
    l_end_values   text[] := ARRAY []::text[];
    l_kind         text;
    l_start        text;
    l_end          text;
BEGIN
    -- The kinds must be one-dimensional, 1-based and one to three entries.
    -- cardinality alone does not catch the last two, and indexing a non-1-based
    -- or multidimensional array would read a slice or an out-of-range subscript.
    IF i_key_kinds IS NULL
        OR pg_catalog.array_ndims(i_key_kinds) IS DISTINCT FROM 1
        OR pg_catalog.array_lower(i_key_kinds, 1) IS DISTINCT FROM 1
        OR pg_catalog.cardinality(i_key_kinds) NOT BETWEEN 1 AND 3
    THEN
        RAISE EXCEPTION 'key kinds must be a one-dimensional, 1-based array of one to three entries'
            USING ERRCODE = '22023';
    END IF;

    -- A non-positive chunk size would make the bigint range empty or reversed.
    IF i_chunk_size IS NULL OR i_chunk_size <= 0 THEN
        RAISE EXCEPTION 'chunk size must be positive'
            USING ERRCODE = '22023';
    END IF;

    -- Build one representative start/end literal per position, in key order.
    -- Each kind gets a pair whose start sorts before its end: bigint spans one
    -- chunk (0 .. chunk_size), text uses a .. b, uuid uses the first two values
    -- of the all-zero form, timestamptz uses two consecutive days, and bytea uses
    -- the hex digits 00 and 01. The literals are synthetic; the explain path uses
    -- them only to render a valid, plausible chunk predicate.
    FOR l_position IN 1..pg_catalog.cardinality(i_key_kinds)
        LOOP
            l_kind := i_key_kinds[l_position];

            -- A NULL kind would fall through every comparison; reject it here so
            -- the message names the bad value rather than a silent ELSE.
            IF l_kind = 'bigint' THEN
                l_start := '0';
                l_end := i_chunk_size::text;
            ELSIF l_kind = 'text' THEN
                l_start := 'a';
                l_end := 'b';
            ELSIF l_kind = 'uuid' THEN
                l_start := '00000000-0000-0000-0000-000000000000';
                l_end := '00000000-0000-0000-0000-000000000001';
            ELSIF l_kind = 'timestamptz' THEN
                l_start := '2026-01-01T00:00:00Z';
                l_end := '2026-01-02T00:00:00Z';
            ELSIF l_kind = 'bytea' THEN
                l_start := '00';
                l_end := '01';
            ELSE
                RAISE EXCEPTION 'unsupported key kind %', l_kind
                    USING ERRCODE = '22023';
            END IF;

            l_start_values := l_start_values || l_start;
            l_end_values := l_end_values || l_end;
        END LOOP;

    -- Two rows: the non-final range (half-open) and the final range (inclusive).
    -- The values are identical; only the caller's i_is_final, and so the range
    -- operator render_chunk_sql emits, differs.
    RETURN QUERY
        SELECT false, l_start_values, l_end_values
        UNION ALL
        SELECT true, l_start_values, l_end_values;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.synthetic_chunk_boundary_values IS
    'Returns two representative chunk ranges, one per key kind and in key order: '
        'the non-final (o_is_final false) and final (o_is_final true) rows. '
        'bigint spans 0..chunk_size, text a..b, uuid the first two all-zero forms, '
        'timestamptz two consecutive days and bytea the hex digits 00 and 01. The '
        'values are synthetic, so the explain path can render and EXPLAIN a '
        'plausible chunk predicate without reading real boundaries.';
