CREATE DOMAIN ddl_utils.non_negative_integer AS integer
    CONSTRAINT non_negative_integer_check CHECK (value IS NOT NULL AND value >= 0);

COMMENT ON DOMAIN ddl_utils.non_negative_integer IS
    'integer that is NOT NULL and greater than or equal to 0.';

-- Trim ASCII whitespace, including vertical tab via the octal escape \013:
-- PostgreSQL has no \v escape in E-strings (it reads \v as the letter v).
CREATE DOMAIN ddl_utils.non_null_text AS text
    CONSTRAINT non_null_text_check CHECK (
        value IS NOT NULL AND pg_catalog.btrim(value, E' \t\n\r\f\013') <> ''
        );

COMMENT ON DOMAIN ddl_utils.non_null_text IS
    'text that is NOT NULL and not blank.';

CREATE DOMAIN ddl_utils.non_null_boolean AS boolean
    CONSTRAINT non_null_boolean_check CHECK (value IS NOT NULL);

COMMENT ON DOMAIN ddl_utils.non_null_boolean IS
    'boolean that is NOT NULL.';

-- Unlike the non_null variants, this domain allows NULL elements: callers that
-- need element-level guarantees must use non_empty_non_null_text_array.
CREATE DOMAIN ddl_utils.non_empty_text_array AS text[]
    CONSTRAINT non_empty_text_array_check CHECK (
        value IS NOT NULL AND pg_catalog.cardinality(value) >= 1
        );

COMMENT ON DOMAIN ddl_utils.non_empty_text_array IS
    'text array with at least one element; elements may be NULL.';

-- array_position is undefined for multidimensional arrays (it raises rather
-- than returning a result), so require a one-dimensional array first; the AND
-- short-circuits, so array_position is never evaluated for a multidimension.
CREATE DOMAIN ddl_utils.non_empty_non_null_text_array AS text[]
    CONSTRAINT non_empty_non_null_text_array_check CHECK (
        value IS NOT NULL
            AND pg_catalog.cardinality(value) >= 1
            AND pg_catalog.array_ndims(value) = 1
            AND pg_catalog.array_position(value, NULL) IS NULL
        );

COMMENT ON DOMAIN ddl_utils.non_empty_non_null_text_array IS
    'one-dimensional text array with at least one element and no NULL elements.';

CREATE DOMAIN ddl_utils.non_empty_non_null_boolean_array AS boolean[]
    CONSTRAINT non_empty_non_null_boolean_array_check CHECK (
        value IS NOT NULL
            AND pg_catalog.cardinality(value) >= 1
            AND pg_catalog.array_ndims(value) = 1
            AND pg_catalog.array_position(value, NULL) IS NULL
        );

COMMENT ON DOMAIN ddl_utils.non_empty_non_null_boolean_array IS
    'one-dimensional boolean array with at least one element and no NULL elements.';
