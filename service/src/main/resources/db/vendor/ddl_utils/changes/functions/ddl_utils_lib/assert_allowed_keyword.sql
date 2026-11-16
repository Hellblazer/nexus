CREATE OR REPLACE FUNCTION ddl_utils_lib.assert_allowed_keyword(
    i_value ddl_utils.non_null_text,
    i_allowed text[],
    i_context ddl_utils.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    IMMUTABLE
    SECURITY INVOKER
AS
$$
BEGIN
    -- The comparison is case-insensitive on both sides, and EXISTS (rather than
    -- = ANY) makes a NULL or empty allow-list (and NULL elements) reject the
    -- value instead of making the predicate NULL and skipping the check. The
    -- message does not echo i_allowed: array_to_string is STABLE, which would
    -- stop this function being IMMUTABLE.
    IF NOT EXISTS (SELECT 1
                   FROM pg_catalog.unnest(i_allowed) AS allowed(keyword)
                   WHERE pg_catalog.lower(allowed.keyword) = pg_catalog.lower(i_value)) THEN
        RAISE EXCEPTION '%: % is not an allowed keyword', i_context, i_value
            USING ERRCODE = '22023';
    END IF;
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.assert_allowed_keyword IS
    'Raises 22023 when a value is not one of a case-insensitive keyword allow-list.';
