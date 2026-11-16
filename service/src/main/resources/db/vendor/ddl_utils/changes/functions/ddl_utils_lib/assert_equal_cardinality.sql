CREATE OR REPLACE FUNCTION ddl_utils_lib.assert_equal_cardinality(
    i_a anyarray,
    i_b anyarray,
    i_context ddl_utils.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    IMMUTABLE
    SECURITY INVOKER
AS
$$
BEGIN
    -- Two NULL arrays both have a NULL cardinality, so IS DISTINCT FROM alone
    -- would accept them; reject a NULL array explicitly (cardinality is
    -- undefined for it).
    IF i_a IS NULL OR i_b IS NULL
        OR pg_catalog.cardinality(i_a) IS DISTINCT FROM pg_catalog.cardinality(i_b) THEN
        RAISE EXCEPTION '%: arrays must have the same length (% vs %)',
            i_context, pg_catalog.cardinality(i_a), pg_catalog.cardinality(i_b)
            USING ERRCODE = '22023';
    END IF;
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.assert_equal_cardinality IS
    'Raises 22023 when two arrays do not have the same number of elements.';
