-- The 004 CHECKs relied on AND short-circuiting before array_position.
-- PostgreSQL does not guarantee boolean evaluation order, so redefine both
-- domains with an explicit CASE. The accepted value set is unchanged, so the
-- validating ADD CONSTRAINT passes for existing data; 004 is left in place for
-- upgrade compatibility and this changeset supersedes its constraint bodies.
ALTER DOMAIN ddl_utils.non_empty_non_null_text_array
    DROP CONSTRAINT non_empty_non_null_text_array_check;
ALTER DOMAIN ddl_utils.non_empty_non_null_text_array
    ADD CONSTRAINT non_empty_non_null_text_array_check CHECK (
        CASE
            WHEN value IS NULL THEN FALSE
            WHEN pg_catalog.cardinality(value) < 1 THEN FALSE
            WHEN pg_catalog.array_ndims(value) IS DISTINCT FROM 1 THEN FALSE
            ELSE pg_catalog.array_position(value, NULL) IS NULL
            END
        );

ALTER DOMAIN ddl_utils.non_empty_non_null_boolean_array
    DROP CONSTRAINT non_empty_non_null_boolean_array_check;
ALTER DOMAIN ddl_utils.non_empty_non_null_boolean_array
    ADD CONSTRAINT non_empty_non_null_boolean_array_check CHECK (
        CASE
            WHEN value IS NULL THEN FALSE
            WHEN pg_catalog.cardinality(value) < 1 THEN FALSE
            WHEN pg_catalog.array_ndims(value) IS DISTINCT FROM 1 THEN FALSE
            ELSE pg_catalog.array_position(value, NULL) IS NULL
            END
        );
