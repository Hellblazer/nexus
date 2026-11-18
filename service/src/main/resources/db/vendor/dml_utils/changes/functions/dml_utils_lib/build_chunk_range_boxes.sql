CREATE OR REPLACE FUNCTION dml_utils_lib.build_chunk_range_boxes(
    i_column_names text[],
    i_start_literals text[],
    i_end_literals text[],
    i_first_difference_position integer,
    i_is_final boolean
)
    RETURNS text[]
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_column_count integer;
    l_boxes        text[];
    l_box          text;
BEGIN
    IF i_is_final IS NULL THEN
        RAISE EXCEPTION 'i_is_final must not be NULL'
            USING ERRCODE = '22023';
    END IF;

    l_column_count := pg_catalog.array_length(i_column_names, 1);

    -- Emit the chunk as a disjunction of axis-aligned boxes that exactly tiles the
    -- lexicographic range [start, end). Each box is a prefix of equalities plus a
    -- range on the next column, so PostgreSQL estimates it column by column and
    -- consumes it as a tight index range. A single row-value comparison instead
    -- multiplies the two bounds' selectivities independently, which overestimates
    -- a mid-table chunk enough to prefer a sequential scan of the whole table.
    --
    -- Let d be the first column whose bounds differ. The boxes are the lower tail
    -- (c_d = start_d and the suffix >= the lower suffix), the middle
    -- (start_d < c_d < end_d) and the upper tail (c_d = end_d and the suffix < the
    -- upper suffix, inclusive on the last column for the final chunk): 2*(n-d)+1
    -- boxes in all. A key that differs only in its last column is a single scalar
    -- range, and a key whose bounds are all equal is a single row.
    l_boxes := ARRAY []::text[];
    IF i_first_difference_position = 0 THEN
        -- All columns equal: exactly one row (a final chunk only; the caller
        -- rejects it otherwise).
        l_box := '';
        FOR l_position IN 1..l_column_count
            LOOP
                l_box := l_box || pg_catalog.format('%s%s = %s',
                                                    CASE WHEN l_position > 1 THEN ' AND ' ELSE '' END,
                                                    i_column_names[l_position],
                                                    i_start_literals[l_position]);
            END LOOP;
        l_boxes := l_boxes || l_box;
    ELSIF i_first_difference_position = l_column_count THEN
        -- Only the last column differs: one box, a half-open (final: closed) scalar
        -- range after the fixed prefix.
        l_box := '';
        FOR l_position IN 1..l_column_count - 1
            LOOP
                l_box := l_box || pg_catalog.format('%s = %s AND ',
                                                    i_column_names[l_position],
                                                    i_start_literals[l_position]);
            END LOOP;
        l_box := l_box || pg_catalog.format('%s >= %s AND %s %s %s',
                                            i_column_names[l_column_count],
                                            i_start_literals[l_column_count],
                                            i_column_names[l_column_count],
                                            CASE WHEN i_is_final THEN '<=' ELSE '<' END,
                                            i_end_literals[l_column_count]);
        l_boxes := l_boxes || l_box;
    ELSE
        -- Lower tail: c_d = start_d and the suffix >= the lower suffix. One box per
        -- suffix column: c_j > start_j, and >= start_n on the last column.
        FOR l_j IN i_first_difference_position + 1..l_column_count
            LOOP
                l_box := '';
                FOR l_position IN 1..i_first_difference_position
                    LOOP
                        l_box := l_box || pg_catalog.format('%s = %s AND ',
                                                            i_column_names[l_position],
                                                            i_start_literals[l_position]);
                    END LOOP;
                FOR l_position IN i_first_difference_position + 1..l_j - 1
                    LOOP
                        l_box := l_box || pg_catalog.format('%s = %s AND ',
                                                            i_column_names[l_position],
                                                            i_start_literals[l_position]);
                    END LOOP;
                IF l_j < l_column_count THEN
                    l_box := l_box || pg_catalog.format('%s > %s',
                                                        i_column_names[l_j],
                                                        i_start_literals[l_j]);
                ELSE
                    l_box := l_box || pg_catalog.format('%s >= %s',
                                                        i_column_names[l_j],
                                                        i_start_literals[l_j]);
                END IF;
                l_boxes := l_boxes || l_box;
            END LOOP;

        -- Middle: start_d < c_d < end_d, after the fixed prefix.
        l_box := '';
        FOR l_position IN 1..i_first_difference_position - 1
            LOOP
                l_box := l_box || pg_catalog.format('%s = %s AND ',
                                                    i_column_names[l_position],
                                                    i_start_literals[l_position]);
            END LOOP;
        l_box := l_box || pg_catalog.format('%s > %s AND %s < %s',
                                            i_column_names[i_first_difference_position],
                                            i_start_literals[i_first_difference_position],
                                            i_column_names[i_first_difference_position],
                                            i_end_literals[i_first_difference_position]);
        l_boxes := l_boxes || l_box;

        -- Upper tail: c_d = end_d and the suffix < the upper suffix. One box per
        -- suffix column: c_j < end_j, and <= end_n on the last column for the
        -- final chunk (so the captured maximum row is processed).
        FOR l_j IN i_first_difference_position + 1..l_column_count
            LOOP
                l_box := '';
                FOR l_position IN 1..i_first_difference_position - 1
                    LOOP
                        l_box := l_box || pg_catalog.format('%s = %s AND ',
                                                            i_column_names[l_position],
                                                            i_start_literals[l_position]);
                    END LOOP;
                l_box := l_box || pg_catalog.format('%s = %s AND ',
                                                    i_column_names[i_first_difference_position],
                                                    i_end_literals[i_first_difference_position]);
                FOR l_position IN i_first_difference_position + 1..l_j - 1
                    LOOP
                        l_box := l_box || pg_catalog.format('%s = %s AND ',
                                                            i_column_names[l_position],
                                                            i_end_literals[l_position]);
                    END LOOP;
                IF l_j < l_column_count THEN
                    l_box := l_box || pg_catalog.format('%s < %s',
                                                        i_column_names[l_j],
                                                        i_end_literals[l_j]);
                ELSE
                    l_box := l_box || pg_catalog.format('%s %s %s',
                                                        i_column_names[l_j],
                                                        CASE WHEN i_is_final THEN '<=' ELSE '<' END,
                                                        i_end_literals[l_j]);
                END IF;
                l_boxes := l_boxes || l_box;
            END LOOP;
    END IF;

    RETURN l_boxes;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.build_chunk_range_boxes(
    text[],
    text[],
    text[],
    integer,
    boolean
    ) IS
    'Builds the axis-aligned box disjunction that exactly tiles a lexicographic '
        '[start, end) chunk range. i_first_difference_position is the first column '
        'whose bounds differ (0 when all are equal); i_is_final makes the last '
        'column''s upper bound inclusive. Shared by render_prepared_chunk_sql.';
