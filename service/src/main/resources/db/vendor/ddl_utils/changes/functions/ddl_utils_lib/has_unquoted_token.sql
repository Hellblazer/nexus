CREATE OR REPLACE FUNCTION ddl_utils_lib.has_unquoted_token(
    i_value text,
    i_tokens ddl_utils.non_empty_non_null_text_array,
    i_top_level_only ddl_utils.non_null_boolean
)
    RETURNS boolean
    LANGUAGE plpgsql
    IMMUTABLE
    SECURITY INVOKER
AS
$func$
DECLARE
    -- Dollar-quote tags: the first character may not be a digit.
    l_tag_first_chars constant text    := 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_';
    l_tag_chars       constant text    := 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_';
    l_length                   integer;
    l_pos                      integer := 1;
    l_scan                     integer;
    l_depth                    integer := 0;
    l_block_depth              integer := 0;
    l_state                    text    := 'code';
    l_dollar_tag               text;
    l_char                     text;
    l_next                     text;
    l_prev                     text;
    l_token                    text;
BEGIN
    IF i_value IS NULL THEN
        RETURN false;
    END IF;

    -- An empty token would match at every position, so reject it rather than
    -- returning true for any value.
    IF EXISTS (SELECT 1
               FROM pg_catalog.unnest(i_tokens) AS t(token)
               WHERE pg_catalog.btrim(t.token) = '') THEN
        RAISE EXCEPTION 'ddl_utils_lib.has_unquoted_token: a token must not be blank'
            USING ERRCODE = '22023';
    END IF;

    -- One left-to-right scan. Strings, dollar-quoted literals, quoted
    -- identifiers and comments are skipped as opaque text. A token in code
    -- state is reported when i_top_level_only is false, or when it appears at
    -- bracket depth 0. A comment start is still skipped when it is not one of
    -- i_tokens, so `--` inside a fragment does not hide a later token.
    l_length := pg_catalog.length(i_value);

    WHILE l_pos <= l_length
        LOOP
            l_char := pg_catalog.substr(i_value, l_pos, 1);
            l_next := pg_catalog.substr(i_value, l_pos + 1, 1);
            l_prev := pg_catalog.substr(i_value, l_pos - 1, 1);

            IF l_state = 'code' THEN
                IF l_char = '-' AND l_next = '-' THEN
                    IF (NOT i_top_level_only OR l_depth = 0)
                        AND '--' = ANY (i_tokens) THEN
                        RETURN true;
                    END IF;
                    l_state := 'line_comment';
                    l_pos := l_pos + 2;
                ELSIF l_char = '/' AND l_next = '*' THEN
                    IF (NOT i_top_level_only OR l_depth = 0)
                        AND '/*' = ANY (i_tokens) THEN
                        RETURN true;
                    END IF;
                    l_state := 'block_comment';
                    l_block_depth := 1;
                    l_pos := l_pos + 2;
                ELSIF l_char = '*' AND l_next = '/' THEN
                    IF (NOT i_top_level_only OR l_depth = 0)
                        AND '*/' = ANY (i_tokens) THEN
                        RETURN true;
                    END IF;
                    l_pos := l_pos + 1;
                    -- Only treat e/E as the E-string prefix when it does not continue an
                    -- identifier (for example a name ending in e).
                ELSIF (l_char = 'e' OR l_char = 'E')
                    AND l_next = ''''
                    AND (l_pos = 1 OR pg_catalog.strpos(l_tag_chars, l_prev) = 0) THEN
                    l_state := 'estring';
                    l_pos := l_pos + 2;
                ELSIF l_char = '''' THEN
                    l_state := 'string';
                    l_pos := l_pos + 1;
                ELSIF l_char = '"' THEN
                    l_state := 'identifier';
                    l_pos := l_pos + 1;
                ELSIF l_char = '$' AND l_next = '$' THEN
                    l_dollar_tag := '$$';
                    l_state := 'dollar';
                    l_pos := l_pos + 2;
                ELSIF l_char = '$' AND pg_catalog.strpos(l_tag_first_chars, l_next) > 0 THEN
                    l_scan := l_pos + 1;
                    l_dollar_tag := '$';
                    WHILE l_scan <= l_length
                        AND
                          pg_catalog.strpos(l_tag_chars, pg_catalog.substr(i_value, l_scan, 1)) > 0
                        LOOP
                            l_dollar_tag := l_dollar_tag || pg_catalog.substr(i_value, l_scan, 1);
                            l_scan := l_scan + 1;
                        END LOOP;
                    IF pg_catalog.substr(i_value, l_scan, 1) = '$' THEN
                        l_dollar_tag := l_dollar_tag || '$';
                        l_state := 'dollar';
                        l_pos := l_scan + 1;
                    ELSE
                        l_pos := l_pos + 1;
                    END IF;
                ELSIF l_char IN ('(', '[', '{') THEN
                    l_depth := l_depth + 1;
                    l_pos := l_pos + 1;
                ELSIF l_char IN (')', ']', '}') THEN
                    l_depth := greatest(l_depth - 1, 0);
                    l_pos := l_pos + 1;
                ELSIF l_char = ',' AND l_depth = 0 AND ',' = ANY (i_tokens) THEN
                    RETURN true;
                ELSIF l_char = ';'
                    AND (NOT i_top_level_only OR l_depth = 0)
                    AND ';' = ANY (i_tokens) THEN
                    RETURN true;
                ELSE
                    -- Tokens other than a comma/semicolon/comment marker; the
                    -- check is generic so the caller can pass a punctuation set.
                    FOREACH l_token IN ARRAY i_tokens::text[]
                        LOOP
                            IF pg_catalog.substr(i_value, l_pos, pg_catalog.length(l_token)) = l_token
                                AND (NOT i_top_level_only OR l_depth = 0) THEN
                                RETURN true;
                            END IF;
                        END LOOP;
                    l_pos := l_pos + 1;
                END IF;
            ELSIF l_state = 'string' THEN
                -- Standard string: '' is an escaped quote, backslash is literal.
                IF l_char = '''' AND l_next = '''' THEN
                    l_pos := l_pos + 2;
                ELSIF l_char = '''' THEN
                    l_state := 'code';
                    l_pos := l_pos + 1;
                ELSE
                    l_pos := l_pos + 1;
                END IF;
            ELSIF l_state = 'estring' THEN
                -- E-string: backslash escapes the next character; '' also works.
                IF l_char = '\' THEN
                    l_pos := l_pos + 2;
                ELSIF l_char = '''' AND l_next = '''' THEN
                    l_pos := l_pos + 2;
                ELSIF l_char = '''' THEN
                    l_state := 'code';
                    l_pos := l_pos + 1;
                ELSE
                    l_pos := l_pos + 1;
                END IF;
            ELSIF l_state = 'identifier' THEN
                IF l_char = '"' AND l_next = '"' THEN
                    l_pos := l_pos + 2;
                ELSIF l_char = '"' THEN
                    l_state := 'code';
                    l_pos := l_pos + 1;
                ELSE
                    l_pos := l_pos + 1;
                END IF;
            ELSIF l_state = 'line_comment' THEN
                -- A line comment ends at LF or CR (bare CR is a line ending on some
                -- platforms and would otherwise swallow the rest of the value).
                IF l_char = chr(10) OR l_char = chr(13) THEN
                    l_state := 'code';
                END IF;
                l_pos := l_pos + 1;
            ELSIF l_state = 'block_comment' THEN
                -- PostgreSQL block comments nest, so track the depth.
                IF l_char = '/' AND l_next = '*' THEN
                    l_block_depth := l_block_depth + 1;
                    l_pos := l_pos + 2;
                ELSIF l_char = '*' AND l_next = '/' THEN
                    l_block_depth := l_block_depth - 1;
                    l_pos := l_pos + 2;
                    IF l_block_depth = 0 THEN
                        l_state := 'code';
                    END IF;
                ELSE
                    l_pos := l_pos + 1;
                END IF;
            ELSIF l_state = 'dollar' THEN
                IF l_char = '$'
                    AND pg_catalog.substr(i_value, l_pos, pg_catalog.length(l_dollar_tag)) =
                        l_dollar_tag THEN
                    l_state := 'code';
                    l_pos := l_pos + pg_catalog.length(l_dollar_tag);
                ELSE
                    l_pos := l_pos + 1;
                END IF;
            END IF;
        END LOOP;

    RETURN false;
END;
$func$;

COMMENT ON FUNCTION ddl_utils_lib.has_unquoted_token IS
    'Returns whether a value contains one of the given tokens in code (outside '
        'strings, quoted identifiers, dollar-quoted literals and comments), '
        'optionally only at bracket depth 0. A NULL value returns false.';
