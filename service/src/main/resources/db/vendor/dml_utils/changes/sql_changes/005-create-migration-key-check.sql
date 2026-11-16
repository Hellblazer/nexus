CREATE OR REPLACE FUNCTION dml_utils_lib.migration_key_is_canonical(
    i_key dml_utils_data.migration_key
)
    RETURNS boolean
    LANGUAGE sql
    IMMUTABLE
    SECURITY INVOKER
    SET search_path = pg_catalog
AS
$$
    -- A canonical boundary key has an arity of one to three, every present array
-- shares that arity, is one-dimensional and 1-based, exactly one array holds a
-- non-NULL element at each index (so each position names one key column), and no
-- present array is empty or all-NULL. The arity is the greatest present array
-- length: greatest/least ignore NULLs, so absent kinds do not participate, and
-- coalescing to 0 makes an all-absent key false rather than NULL (a CHECK would
-- accept NULL).
WITH arity AS (SELECT coalesce(
                              greatest(pg_catalog.array_length((i_key).bigint_values, 1),
                                       pg_catalog.array_length((i_key).text_values, 1),
                                       pg_catalog.array_length((i_key).uuid_values, 1),
                                       pg_catalog.array_length((i_key).timestamptz_values, 1),
                                       pg_catalog.array_length((i_key).bytea_values, 1)), 0) AS n)
SELECT arity.n BETWEEN 1 AND 3
           -- Every present array is one-dimensional with a lower bound of 1.
           -- One-subscript indexing below (and in migration_key_values) assumes
           -- both; a non-1-based or multidimensional array would otherwise be
           -- read at the wrong subscript (or array_remove would raise).
           AND ((i_key).bigint_values IS NULL
        OR (pg_catalog.array_ndims((i_key).bigint_values) = 1
            AND pg_catalog.array_lower((i_key).bigint_values, 1) = 1))
           AND ((i_key).text_values IS NULL
        OR (pg_catalog.array_ndims((i_key).text_values) = 1
            AND pg_catalog.array_lower((i_key).text_values, 1) = 1))
           AND ((i_key).uuid_values IS NULL
        OR (pg_catalog.array_ndims((i_key).uuid_values) = 1
            AND pg_catalog.array_lower((i_key).uuid_values, 1) = 1))
           AND ((i_key).timestamptz_values IS NULL
        OR (pg_catalog.array_ndims((i_key).timestamptz_values) = 1
            AND pg_catalog.array_lower((i_key).timestamptz_values, 1) = 1))
           AND ((i_key).bytea_values IS NULL
        OR (pg_catalog.array_ndims((i_key).bytea_values) = 1
            AND pg_catalog.array_lower((i_key).bytea_values, 1) = 1))
           -- Every present array shares the arity; absent kinds do not participate.
           AND least(pg_catalog.array_length((i_key).bigint_values, 1),
                     pg_catalog.array_length((i_key).text_values, 1),
                     pg_catalog.array_length((i_key).uuid_values, 1),
                     pg_catalog.array_length((i_key).timestamptz_values, 1),
                     pg_catalog.array_length((i_key).bytea_values, 1))
           = greatest(pg_catalog.array_length((i_key).bigint_values, 1),
                      pg_catalog.array_length((i_key).text_values, 1),
                      pg_catalog.array_length((i_key).uuid_values, 1),
                      pg_catalog.array_length((i_key).timestamptz_values, 1),
                      pg_catalog.array_length((i_key).bytea_values, 1))
           -- Exactly one array holds a value at each index, compared only up to the
           -- arity (indexing past an array's end yields NULL, and those positions would
           -- otherwise couple through the next guard).
           AND (arity.n < 1
        OR pg_catalog.num_nonnulls((i_key).bigint_values[1],
                                   (i_key).text_values[1],
                                   (i_key).uuid_values[1],
                                   (i_key).timestamptz_values[1],
                                   (i_key).bytea_values[1]) = 1)
           AND (arity.n < 2
        OR pg_catalog.num_nonnulls((i_key).bigint_values[2],
                                   (i_key).text_values[2],
                                   (i_key).uuid_values[2],
                                   (i_key).timestamptz_values[2],
                                   (i_key).bytea_values[2]) = 1)
           AND (arity.n < 3
        OR pg_catalog.num_nonnulls((i_key).bigint_values[3],
                                   (i_key).text_values[3],
                                   (i_key).uuid_values[3],
                                   (i_key).timestamptz_values[3],
                                   (i_key).bytea_values[3]) = 1)
           -- A present array must hold at least one real value: array_length counts an
           -- all-NULL array as populated and reports an empty array as NULL, so both
           -- would otherwise slip through, yet neither carries a key value.
           AND ((i_key).bigint_values IS NULL
        OR pg_catalog.cardinality(
                   pg_catalog.array_remove((i_key).bigint_values, NULL::bigint)) >= 1)
           AND ((i_key).text_values IS NULL
        OR pg_catalog.cardinality(
                   pg_catalog.array_remove((i_key).text_values, NULL::text)) >= 1)
           AND ((i_key).uuid_values IS NULL
        OR pg_catalog.cardinality(
                   pg_catalog.array_remove((i_key).uuid_values, NULL::uuid)) >= 1)
           AND ((i_key).timestamptz_values IS NULL
        OR pg_catalog.cardinality(
                   pg_catalog.array_remove((i_key).timestamptz_values, NULL::timestamptz)) >= 1)
           AND ((i_key).bytea_values IS NULL
        OR pg_catalog.cardinality(
                   pg_catalog.array_remove((i_key).bytea_values, NULL::bytea)) >= 1)
FROM arity;
$$;

COMMENT ON FUNCTION dml_utils_lib.migration_key_is_canonical IS
    'True when a migration_key is a canonical boundary key: arity one to three, '
        'present arrays of equal length, exactly one array element non-NULL per '
        'index, and no empty or all-NULL present array.';
