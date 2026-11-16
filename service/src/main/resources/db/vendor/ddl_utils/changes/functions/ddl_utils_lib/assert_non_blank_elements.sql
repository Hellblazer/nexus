CREATE OR REPLACE FUNCTION ddl_utils_lib.assert_non_blank_elements(
    i_values ddl_utils.non_empty_text_array,
    i_context ddl_utils.non_null_text,
    i_label ddl_utils.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
BEGIN
    -- The loop below assumes subscript 1; enforce it here so a non-1-based
    -- array cannot skip element 0 or report a misleading position.
    PERFORM ddl_utils_lib.assert_one_based(
            i_values => i_values,
            i_context => i_context);

    -- The array domains allow blank elements; reject them so a blank name, type
    -- or default cannot produce an empty identifier or malformed SQL. The trim
    -- set must stay in step with the ddl_utils.non_null_text domain
    -- (004-create-domains.sql). NULL elements are allowed (a NULL default means
    -- no DEFAULT clause) and are skipped.
    FOR l_index IN 1..pg_catalog.cardinality(i_values)
        LOOP
            IF i_values[l_index] IS NOT NULL
                AND pg_catalog.btrim(i_values[l_index], E' \t\n\r\f\013') = '' THEN
                RAISE EXCEPTION '%: % at position % is blank',
                    i_context, i_label, l_index
                    USING ERRCODE = '22023';
            END IF;
        END LOOP;
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.assert_non_blank_elements IS
    'Raises 22023 when a text array is not 1-based or has a blank element '
        '(NULL elements are skipped).';
