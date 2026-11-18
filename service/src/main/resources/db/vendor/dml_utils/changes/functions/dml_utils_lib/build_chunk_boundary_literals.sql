CREATE OR REPLACE FUNCTION dml_utils_lib.build_chunk_boundary_literals(
    i_column_names text[],
    i_key_kinds text[],
    i_key_collations text[],
    i_start_values text[],
    i_end_values text[],
    OUT o_start_literals text[],
    OUT o_end_literals text[],
    OUT o_first_difference integer,
    OUT o_first_difference_position integer
)
    RETURNS record
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_column_count        integer;
    l_kind                text;
    l_start_less_than_end boolean;
    l_start_bigint        bigint;
    l_end_bigint          bigint;
    l_start_uuid          uuid;
    l_end_uuid            uuid;
    l_start_timestamptz   timestamptz;
    l_end_timestamptz     timestamptz;
    l_start_bytea         bytea;
    l_end_bytea           bytea;
BEGIN
    -- The column names, kinds and both boundary arrays are indexed from 1 below,
    -- so they must be one-dimensional with a lower bound of 1 and the same
    -- non-zero length. array_length of an empty/NULL array is NULL, which the
    -- length comparisons below reject.
    IF pg_catalog.array_ndims(i_column_names) IS DISTINCT FROM 1
        OR pg_catalog.array_lower(i_column_names, 1) IS DISTINCT FROM 1
        OR pg_catalog.array_ndims(i_key_kinds) IS DISTINCT FROM 1
        OR pg_catalog.array_lower(i_key_kinds, 1) IS DISTINCT FROM 1
        OR pg_catalog.array_ndims(i_start_values) IS DISTINCT FROM 1
        OR pg_catalog.array_lower(i_start_values, 1) IS DISTINCT FROM 1
        OR pg_catalog.array_ndims(i_end_values) IS DISTINCT FROM 1
        OR pg_catalog.array_lower(i_end_values, 1) IS DISTINCT FROM 1
        OR pg_catalog.array_length(i_column_names, 1) IS NULL
        OR pg_catalog.array_length(i_column_names, 1) IS DISTINCT FROM pg_catalog.array_length(i_key_kinds, 1)
        OR pg_catalog.array_length(i_column_names, 1) IS DISTINCT FROM pg_catalog.array_length(i_start_values, 1)
        OR pg_catalog.array_length(i_column_names, 1) IS DISTINCT FROM pg_catalog.array_length(i_end_values, 1)
    THEN
        RAISE EXCEPTION 'column names, kinds and boundary values must be one-dimensional, 1-based, same-length and non-empty'
            USING ERRCODE = '22023';
    END IF;

    -- Validate each boundary value and build its cast literal. The kind selects
    -- the explicit cast and is interpolated into the SQL, so it must be one of the
    -- known kinds; the values are quoted with %L.
    l_column_count := pg_catalog.array_length(i_column_names, 1);
    o_start_literals := ARRAY []::text[];
    o_end_literals := ARRAY []::text[];
    o_first_difference := 0;
    o_first_difference_position := 0;
    FOR l_position IN 1..l_column_count
        LOOP
            l_kind := i_key_kinds[l_position];

            -- A NULL kind would slip past NOT IN (NULL NOT IN (...) is NULL, not
            -- true), so guard it explicitly.
            IF l_kind IS NULL OR l_kind NOT IN ('bigint', 'text', 'uuid', 'timestamptz', 'bytea') THEN
                RAISE EXCEPTION 'unsupported key kind %', l_kind
                    USING ERRCODE = '22023';
            END IF;

            -- %L renders a NULL as an unquoted NULL, so a missing boundary value
            -- would render a predicate that silently matches no rows (or narrows a
            -- partial row) while the chunk is still marked complete; fail loudly.
            IF i_start_values[l_position] IS NULL OR i_end_values[l_position] IS NULL THEN
                RAISE EXCEPTION 'boundary % value for column % must not be NULL',
                    CASE WHEN i_start_values[l_position] IS NULL THEN 'start' ELSE 'end' END,
                    i_column_names[l_position]
                    USING ERRCODE = '22023';
            END IF;

            -- A non-NULL value that is not a valid literal for its kind would be
            -- emitted into the chunk SQL and only fail when a worker parses it;
            -- validate by casting here (and keep the typed value for the range check
            -- below), so the failure is not deferred. text accepts any value, so only
            -- bigint, uuid, timestamptz and bytea need a cast (a bytea value is its
            -- hex digits, decoded here). Catch the invalid-text and
            -- invalid-datetime conditions (22023 / 22P02 / 22007 / 22008 / 22009); an
            -- unexpected error is re-raised with its SQLSTATE.
            BEGIN
                IF l_kind = 'bigint' THEN
                    l_start_bigint := i_start_values[l_position]::bigint;
                    l_end_bigint := i_end_values[l_position]::bigint;
                    IF o_first_difference = 0 AND l_start_bigint IS DISTINCT FROM l_end_bigint THEN
                        o_first_difference := CASE WHEN l_start_bigint < l_end_bigint THEN -1 ELSE 1 END;
                        o_first_difference_position := l_position;
                    END IF;
                ELSIF l_kind = 'uuid' THEN
                    l_start_uuid := i_start_values[l_position]::uuid;
                    l_end_uuid := i_end_values[l_position]::uuid;
                    IF o_first_difference = 0 AND l_start_uuid IS DISTINCT FROM l_end_uuid THEN
                        o_first_difference := CASE WHEN l_start_uuid < l_end_uuid THEN -1 ELSE 1 END;
                        o_first_difference_position := l_position;
                    END IF;
                ELSIF l_kind = 'timestamptz' THEN
                    l_start_timestamptz := i_start_values[l_position]::timestamptz;
                    l_end_timestamptz := i_end_values[l_position]::timestamptz;
                    IF o_first_difference = 0
                        AND l_start_timestamptz IS DISTINCT FROM l_end_timestamptz
                    THEN
                        o_first_difference := CASE
                                                  WHEN l_start_timestamptz < l_end_timestamptz THEN -1
                                                  ELSE 1
                            END;
                        o_first_difference_position := l_position;
                    END IF;
                ELSIF l_kind = 'bytea' THEN
                    -- The value is hex digits; decode raises 22023 for a non-hex
                    -- digit or an odd length. bytea sorts by unsigned byte order
                    -- (byteacmp), the order of the key's btree and of the
                    -- predicate, so no collation applies.
                    l_start_bytea := pg_catalog.decode(i_start_values[l_position], 'hex');
                    l_end_bytea := pg_catalog.decode(i_end_values[l_position], 'hex');
                    IF o_first_difference = 0 AND l_start_bytea IS DISTINCT FROM l_end_bytea THEN
                        o_first_difference := CASE WHEN l_start_bytea < l_end_bytea THEN -1 ELSE 1 END;
                        o_first_difference_position := l_position;
                    END IF;
                ELSE
                    -- text must be ordered under the column's own collation, which
                    -- is what the predicate uses. The collation was resolved once by
                    -- prepare_chunk_sql_render: NULL means the column is not in the
                    -- catalog (compare under the default collation), otherwise it is
                    -- applied with an explicit COLLATE.
                    IF o_first_difference = 0
                        AND i_start_values[l_position] IS DISTINCT FROM i_end_values[l_position]
                    THEN
                        IF i_key_collations[l_position] IS NULL THEN
                            l_start_less_than_end :=
                                    i_start_values[l_position] < i_end_values[l_position];
                        ELSE
                            EXECUTE pg_catalog.format(
                                    'SELECT %L::text COLLATE %s < %L::text COLLATE %s',
                                    i_start_values[l_position], i_key_collations[l_position],
                                    i_end_values[l_position], i_key_collations[l_position])
                                INTO l_start_less_than_end;
                        END IF;

                        o_first_difference := CASE WHEN l_start_less_than_end THEN -1 ELSE 1 END;
                        o_first_difference_position := l_position;
                    END IF;
                END IF;
            EXCEPTION
                WHEN invalid_parameter_value
                    OR invalid_text_representation
                    OR invalid_datetime_format
                    OR datetime_field_overflow
                    OR SQLSTATE '22009' THEN
                    RAISE EXCEPTION
                        'boundary value for column % is not a valid %',
                        i_column_names[l_position], l_kind
                        USING ERRCODE = '22023';
            END;

            -- A bytea literal is rendered through decode of the normalized hex
            -- digits, never as a quoted '\x...' string: decode takes a plain hex
            -- literal, so the SQL does not depend on standard_conforming_strings
            -- or bytea_output, and it is a constant the planner folds, so the key's
            -- index serves the predicate.
            IF l_kind = 'bytea' THEN
                o_start_literals := o_start_literals || pg_catalog.format('pg_catalog.decode(%L, ''hex'')',
                                                                          pg_catalog.encode(l_start_bytea, 'hex'));
                o_end_literals := o_end_literals || pg_catalog.format('pg_catalog.decode(%L, ''hex'')',
                                                                      pg_catalog.encode(l_end_bytea, 'hex'));
            ELSE
                o_start_literals := o_start_literals || pg_catalog.format('%L::%s',
                                                                          i_start_values[l_position],
                                                                          l_kind);
                o_end_literals := o_end_literals || pg_catalog.format('%L::%s',
                                                                      i_end_values[l_position],
                                                                      l_kind);
            END IF;
        END LOOP;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.build_chunk_boundary_literals(
    text[],
    text[],
    text[],
    text[],
    text[]
    ) IS
    'Validates a chunk''s start/end boundary values, builds their typed SQL '
        'literals and returns the first differing column with its direction. A '
        'NULL value, an invalid literal for its kind or a malformed array is '
        'rejected with 22023. Shared by render_prepared_chunk_sql.';
