CREATE OR REPLACE FUNCTION dml_utils_lib.ordered_key_attributes(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_index_name dml_utils_data.non_null_text
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
    l_index record;
    l_hint  text;
BEGIN
    -- Resolve the named index on the driving table and reject a partitioned or
    -- otherwise non-plain table here; the shape and per-column contract checks are
    -- shared with the partition-wise path in ordered_key_attributes_for_index.
    PERFORM dml_utils_lib.assert_schema_exists(i_schema_name => i_schema_name);
    PERFORM dml_utils_lib.assert_table_exists(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name);

    SELECT i.indexrelid, c.relkind
    INTO l_index
    FROM pg_catalog.pg_index AS i
             JOIN pg_catalog.pg_class AS ic ON ic.oid = i.indexrelid
             JOIN pg_catalog.pg_class AS c ON c.oid = i.indrelid
             JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = i_schema_name
      AND c.relname = i_table_name
      AND ic.relname = i_index_name;

    IF NOT FOUND THEN
        -- Index names are unique within a schema, so a miss usually means the
        -- index belongs to another table or the name is misspelled. Resolve the
        -- actual table for a hint, only on the failure path.
        SELECT pg_catalog.format('index %I.%I exists on table %I.%I',
                                 n.nspname, ic.relname, n.nspname, c.relname)
        INTO l_hint
        FROM pg_catalog.pg_class AS ic
                 JOIN pg_catalog.pg_namespace AS n ON n.oid = ic.relnamespace
                 JOIN pg_catalog.pg_index AS i ON i.indexrelid = ic.oid
                 JOIN pg_catalog.pg_class AS c ON c.oid = i.indrelid
        WHERE n.nspname = i_schema_name
          AND ic.relname = i_index_name;

        IF l_hint IS NULL THEN
            RAISE EXCEPTION 'index %.% does not exist on table %.%',
                i_schema_name, i_index_name, i_schema_name, i_table_name
                USING ERRCODE = '42704';
        ELSE
            RAISE EXCEPTION 'index %.% does not exist on table %.%',
                i_schema_name, i_index_name, i_schema_name, i_table_name
                USING ERRCODE = '42704', HINT = l_hint;
        END IF;
    END IF;

    -- A partitioned parent has no single heap or row order of its own; the
    -- partition-wise ordered path validates the parent index and every child
    -- index (partitioned_ordered_key_attributes).
    IF l_index.relkind <> 'r' THEN
        RAISE EXCEPTION 'ordered-key chunking requires a plain table, but %.% has relkind %',
            i_schema_name, i_table_name, l_index.relkind
            USING ERRCODE = '0A000';
    END IF;

    RETURN QUERY
        SELECT a.ordinality,
               a.column_name,
               a.column_oid,
               a.column_type,
               a.key_kind
        FROM dml_utils_lib.ordered_key_attributes_for_index(
                     i_index_oid => l_index.indexrelid,
                     i_schema_name => i_schema_name,
                     i_table_name => i_table_name,
                     i_index_name => i_index_name) AS a;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.ordered_key_attributes IS
    'Resolves a unique btree index on a plain driving table to its ordered-key '
        'columns, in index order, with each column''s ordinal position, name, type '
        'oid, type and collapsed boundary kind (bigint, text, uuid, timestamptz or bytea). '
        'The single validator for the ordered-key contract: it rejects a missing '
        'index (42704), a missing schema/table (3F000/42P01), a partitioned or '
        'otherwise non-plain table (0A000), a non-btree or non-unique index, an '
        'invalid index (55000), and a partial, expression, descending, NULLS FIRST, '
        'wrong-arity, unsupported-type, nullable, non-default-opclass or '
        'collation-mismatched key.';
