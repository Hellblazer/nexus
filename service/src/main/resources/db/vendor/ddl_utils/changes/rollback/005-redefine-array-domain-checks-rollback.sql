ALTER DOMAIN ddl_utils.non_empty_non_null_text_array
    DROP CONSTRAINT non_empty_non_null_text_array_check;
ALTER DOMAIN ddl_utils.non_empty_non_null_text_array
    ADD CONSTRAINT non_empty_non_null_text_array_check CHECK (
        value IS NOT NULL
            AND pg_catalog.cardinality(value) >= 1
            AND pg_catalog.array_ndims(value) = 1
            AND pg_catalog.array_position(value, NULL) IS NULL
        );

ALTER DOMAIN ddl_utils.non_empty_non_null_boolean_array
    DROP CONSTRAINT non_empty_non_null_boolean_array_check;
ALTER DOMAIN ddl_utils.non_empty_non_null_boolean_array
    ADD CONSTRAINT non_empty_non_null_boolean_array_check CHECK (
        value IS NOT NULL
            AND pg_catalog.cardinality(value) >= 1
            AND pg_catalog.array_ndims(value) = 1
            AND pg_catalog.array_position(value, NULL) IS NULL
        );
