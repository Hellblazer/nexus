CREATE OR REPLACE FUNCTION dml_utils_lib.boundary_key_pack_expression(
    i_key_kinds text[]
)
    RETURNS text
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_bigint_terms      text[];
    l_text_terms        text[];
    l_uuid_terms        text[];
    l_timestamptz_terms text[];
    l_bytea_terms       text[];
    l_kind              text;
BEGIN
    IF pg_catalog.array_ndims(i_key_kinds) IS DISTINCT FROM 1
        OR pg_catalog.array_lower(i_key_kinds, 1) IS DISTINCT FROM 1
        OR pg_catalog.cardinality(i_key_kinds) NOT BETWEEN 1 AND 3
    THEN
        RAISE EXCEPTION 'key kinds must be one-dimensional, 1-based and have one to three entries'
            USING ERRCODE = '22023';
    END IF;

    l_bigint_terms := pg_catalog.array_fill('NULL'::text, ARRAY [pg_catalog.array_length(i_key_kinds, 1)]);
    l_text_terms := pg_catalog.array_fill('NULL'::text, ARRAY [pg_catalog.array_length(i_key_kinds, 1)]);
    l_uuid_terms := pg_catalog.array_fill('NULL'::text, ARRAY [pg_catalog.array_length(i_key_kinds, 1)]);
    l_timestamptz_terms := pg_catalog.array_fill('NULL'::text, ARRAY [pg_catalog.array_length(i_key_kinds, 1)]);
    l_bytea_terms := pg_catalog.array_fill('NULL'::text, ARRAY [pg_catalog.array_length(i_key_kinds, 1)]);
    FOR l_position IN 1..pg_catalog.array_length(i_key_kinds, 1)
        LOOP
            l_kind := i_key_kinds[l_position];

            -- A NULL kind would slip past NOT IN (NULL NOT IN (...) is NULL,
            -- not true), so guard it explicitly.
            IF l_kind IS NULL OR l_kind NOT IN ('bigint', 'text', 'uuid', 'timestamptz', 'bytea') THEN
                RAISE EXCEPTION 'unsupported key kind %', l_kind
                    USING ERRCODE = '22023';
            END IF;

            IF l_kind = 'bigint' THEN
                l_bigint_terms[l_position] := pg_catalog.format('id%s::bigint', l_position);
            ELSIF l_kind = 'text' THEN
                l_text_terms[l_position] := pg_catalog.format('id%s::text', l_position);
            ELSIF l_kind = 'uuid' THEN
                l_uuid_terms[l_position] := pg_catalog.format('id%s::uuid', l_position);
            ELSIF l_kind = 'timestamptz' THEN
                l_timestamptz_terms[l_position] := pg_catalog.format('id%s::timestamptz', l_position);
            ELSE
                l_bytea_terms[l_position] := pg_catalog.format('id%s::bytea', l_position);
            END IF;
        END LOOP;

    RETURN pg_catalog.format(
            'ROW(%s, %s, %s, %s, %s)::dml_utils_data.migration_key',
            CASE
                WHEN 'bigint' = ANY (i_key_kinds)
                    THEN pg_catalog.format('ARRAY[%s]::bigint[]',
                                           pg_catalog.array_to_string(l_bigint_terms, ', '))
                ELSE 'NULL::bigint[]'
                END,
            CASE
                WHEN 'text' = ANY (i_key_kinds)
                    THEN pg_catalog.format('ARRAY[%s]::text[]',
                                           pg_catalog.array_to_string(l_text_terms, ', '))
                ELSE 'NULL::text[]'
                END,
            CASE
                WHEN 'uuid' = ANY (i_key_kinds)
                    THEN pg_catalog.format('ARRAY[%s]::uuid[]',
                                           pg_catalog.array_to_string(l_uuid_terms, ', '))
                ELSE 'NULL::uuid[]'
                END,
            CASE
                WHEN 'timestamptz' = ANY (i_key_kinds)
                    THEN pg_catalog.format('ARRAY[%s]::timestamptz[]',
                                           pg_catalog.array_to_string(l_timestamptz_terms, ', '))
                ELSE 'NULL::timestamptz[]'
                END,
            CASE
                WHEN 'bytea' = ANY (i_key_kinds)
                    THEN pg_catalog.format('ARRAY[%s]::bytea[]',
                                           pg_catalog.array_to_string(l_bytea_terms, ', '))
                ELSE 'NULL::bytea[]'
                END);
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.boundary_key_pack_expression(text[]) IS
    'Builds the migration_key row expression for a one-to-three-column boundary '
        'key, placing each kind''s idN term in its position-aligned array. Shared '
        'by boundary_key_descriptor_sql.';
