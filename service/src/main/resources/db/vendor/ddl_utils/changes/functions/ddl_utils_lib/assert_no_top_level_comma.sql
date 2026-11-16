CREATE OR REPLACE FUNCTION ddl_utils_lib.assert_no_top_level_comma(
    i_value text,
    i_context ddl_utils.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    IMMUTABLE
    SECURITY INVOKER
AS
$$
BEGIN
    -- A NULL expression means "no DEFAULT clause" to add_columns; there is no
    -- comma to reject. Make the fail-open contract explicit rather than relying
    -- on has_top_level_comma's NULL handling.
    IF i_value IS NULL THEN
        RETURN;
    END IF;

    -- A top-level comma in a raw expression can append a second ALTER TABLE
    -- action; reject it. has_top_level_comma ignores commas nested in groups or
    -- string literals.
    IF ddl_utils_lib.has_top_level_comma(i_value) THEN
        RAISE EXCEPTION '%: a top-level comma is not allowed', i_context
            USING ERRCODE = '22023';
    END IF;
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.assert_no_top_level_comma IS
    'Raises 22023 when a value contains a top-level comma. A NULL value returns '
        'without error.';
