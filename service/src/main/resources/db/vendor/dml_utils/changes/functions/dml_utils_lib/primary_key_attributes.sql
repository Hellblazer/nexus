CREATE OR REPLACE FUNCTION dml_utils_lib.primary_key_attributes(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text
)
    RETURNS TABLE
            (
                ordinality  integer,
                column_name name,
                column_oid  oid,
                column_type pg_catalog.regtype,
                key_kind    text
            )
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_key_column_count smallint;
BEGIN
    -- Validate the schema/table exist and that the primary key is one to three
    -- columns. This is the base reader (primary_key_columns delegates here), so
    -- it performs the checks itself rather than calling back into a helper.
    PERFORM dml_utils_lib.assert_schema_exists(i_schema_name => i_schema_name);
    PERFORM dml_utils_lib.assert_table_exists(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name);

    SELECT i.indnkeyatts
    INTO l_key_column_count
    FROM pg_catalog.pg_index AS i
             JOIN pg_catalog.pg_class AS c ON c.oid = i.indrelid
             JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE i.indisprimary
      AND n.nspname = i_schema_name
      AND c.relname = i_table_name;

    IF l_key_column_count IS NULL THEN
        RAISE EXCEPTION 'table %.% has no primary key', i_schema_name, i_table_name
            USING ERRCODE = '22023';
    END IF;

    IF l_key_column_count > 3 THEN
        RAISE EXCEPTION 'table %.% has a % column primary key; at most 3 are supported',
            i_schema_name, i_table_name, l_key_column_count
            USING ERRCODE = '22023';
    END IF;

    -- indkey lists the key columns in key order; unnest WITH ORDINALITY keeps
    -- that order (attnum order would not, for example a primary key declared as
    -- (b, a)). column_oid is the column's actual type (atttypid), which a
    -- function signature must match; key_kind is the collapsed boundary kind
    -- (the integer family becomes bigint) used to pack migration_key.
    RETURN QUERY
        SELECT k.ordinality::integer,
               a.attname,
               a.atttypid,
               a.atttypid::pg_catalog.regtype,
               CASE
                   WHEN a.atttypid IN ('smallint'::pg_catalog.regtype,
                                       'integer'::pg_catalog.regtype,
                                       'bigint'::pg_catalog.regtype)
                       THEN 'bigint'
                   WHEN a.atttypid = 'text'::pg_catalog.regtype THEN 'text'
                   WHEN a.atttypid = 'uuid'::pg_catalog.regtype THEN 'uuid'
                   WHEN a.atttypid = 'timestamptz'::pg_catalog.regtype THEN 'timestamptz'
                   WHEN a.atttypid = 'bytea'::pg_catalog.regtype THEN 'bytea'
                   ELSE NULL
                   END AS key_kind
        FROM pg_catalog.pg_index AS i
                 JOIN pg_catalog.pg_class AS c ON c.oid = i.indrelid
                 JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                 JOIN pg_catalog.unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinality)
                      ON true
                 JOIN pg_catalog.pg_attribute AS a
                      ON a.attrelid = c.oid AND a.attnum = k.attnum
        WHERE i.indisprimary
          AND n.nspname = i_schema_name
          AND c.relname = i_table_name
          AND k.ordinality <= i.indnkeyatts
        ORDER BY k.ordinality;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.primary_key_attributes IS
    'Returns the primary-key columns in key order with, per column, its ordinal '
        'position, name, type oid, type and collapsed boundary kind (bigint, text, '
        'uuid, timestamptz or bytea; NULL for an unsupported type). Validates the '
        'schema/table and the one-to-three-column limit.';
