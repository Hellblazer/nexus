CREATE OR REPLACE FUNCTION ddl_utils_lib.quote_identifiers(
    i_values ddl_utils.non_empty_non_null_text_array,
    i_prefix text DEFAULT ''
)
    RETURNS text
    LANGUAGE plpgsql
    IMMUTABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_result text;
BEGIN
    -- i_prefix is a literal SQL clause prefix (for example 'DROP COLUMN '), not a
    -- value; each identifier is still quoted with %I. i_prefix is text rather than
    -- ddl_utils.non_null_text because an empty prefix is valid.
    --
    -- The domain allows blank elements and does not bound the length; reject
    -- both here so this helper cannot emit an empty identifier ("") or one that
    -- PostgreSQL silently truncates to NAMEDATALEN - 1 bytes when the generated
    -- SQL is parsed. The trim set matches the ddl_utils.non_null_text domain
    -- (004-create-domains.sql).
    IF EXISTS (SELECT 1
               FROM pg_catalog.unnest(i_values) AS t(value)
               WHERE pg_catalog.btrim(t.value, E' \t\n\r\f\013') = '') THEN
        RAISE EXCEPTION 'ddl_utils_lib.quote_identifiers: an identifier must not be blank'
            USING ERRCODE = '22023';
    END IF;

    IF EXISTS (SELECT 1
               FROM pg_catalog.unnest(i_values) AS t(value)
               WHERE pg_catalog.octet_length(t.value) > 63) THEN
        RAISE EXCEPTION 'ddl_utils_lib.quote_identifiers: an identifier must be at most 63 bytes'
            USING ERRCODE = '22023';
    END IF;

    l_result := (SELECT pg_catalog.string_agg(
                                pg_catalog.format('%s%I', i_prefix, t.value),
                                ', ' ORDER BY t.ordinality)
                 FROM pg_catalog.unnest(i_values) WITH ORDINALITY AS t(value, ordinality));

    RETURN l_result;
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.quote_identifiers IS
    'Joins an array of identifiers into a comma-separated list, each quoted '
        'with %I and optionally prefixed; blank or over-long identifiers are '
        'rejected.';
