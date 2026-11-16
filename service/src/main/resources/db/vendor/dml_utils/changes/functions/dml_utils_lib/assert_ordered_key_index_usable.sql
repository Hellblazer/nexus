CREATE OR REPLACE FUNCTION dml_utils_lib.assert_ordered_key_index_usable(
    i_index_oid oid,
    i_schema_name text,
    i_table_name text,
    i_index_name text
)
    RETURNS void
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_index           record;
    l_table_oid       oid;
    l_named_index_oid oid;
BEGIN
    SELECT i.indrelid,
           i.indnkeyatts,
           i.indisunique,
           i.indisvalid,
           i.indisready,
           i.indislive,
           i.indpred,
           i.indexprs,
           i.indoption,
           am.amname
    INTO l_index
    FROM pg_catalog.pg_index AS i
             JOIN pg_catalog.pg_class AS ic ON ic.oid = i.indexrelid
             JOIN pg_catalog.pg_am AS am ON am.oid = ic.relam
    WHERE i.indexrelid = i_index_oid;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'index %.% does not exist', i_schema_name, i_index_name
            USING ERRCODE = '42704';
    END IF;

    -- The OID and the names must describe the same relation, so a caller cannot
    -- validate one index while the error messages name another table.
    l_table_oid := pg_catalog.to_regclass(
            pg_catalog.format('%I.%I', i_schema_name, i_table_name));
    IF l_table_oid IS NULL THEN
        RAISE EXCEPTION 'table %.% does not exist', i_schema_name, i_table_name
            USING ERRCODE = '42P01';
    END IF;
    IF l_index.indrelid IS DISTINCT FROM l_table_oid THEN
        RAISE EXCEPTION 'index %.% does not belong to table %.%',
            i_schema_name, i_index_name, i_schema_name, i_table_name
            USING ERRCODE = '42704';
    END IF;

    -- The index name must also resolve to the supplied OID, otherwise the
    -- diagnostics can name an index that was never validated.
    l_named_index_oid := pg_catalog.to_regclass(
            pg_catalog.format('%I.%I', i_schema_name, i_index_name));
    IF l_named_index_oid IS DISTINCT FROM i_index_oid THEN
        RAISE EXCEPTION 'index %.% does not match the supplied oid %',
            i_schema_name, i_index_name, i_index_oid
            USING ERRCODE = '42704';
    END IF;

    -- An exclusion constraint can only be backed by gist/spgist, never btree,
    -- so this also rejects one (with the same 0A000).
    IF l_index.amname <> 'btree' THEN
        RAISE EXCEPTION 'ordered-key index %.% must be a btree index, not %',
            i_schema_name, i_index_name, l_index.amname
            USING ERRCODE = '0A000';
    END IF;

    IF NOT l_index.indisunique THEN
        RAISE EXCEPTION 'ordered-key index %.% must be unique',
            i_schema_name, i_index_name
            USING ERRCODE = '0A000';
    END IF;

    -- A failed concurrent build leaves an index that is present but unusable;
    -- the stored boundaries would be meaningless, so fail closed.
    IF NOT (l_index.indisvalid AND l_index.indisready AND l_index.indislive) THEN
        RAISE EXCEPTION 'ordered-key index %.% is not valid, ready and live',
            i_schema_name, i_index_name
            USING ERRCODE = '55000';
    END IF;

    IF l_index.indpred IS NOT NULL THEN
        RAISE EXCEPTION 'ordered-key index %.% must not be partial',
            i_schema_name, i_index_name
            USING ERRCODE = '0A000';
    END IF;

    IF l_index.indexprs IS NOT NULL THEN
        RAISE EXCEPTION 'ordered-key index %.% must index simple columns, not expressions',
            i_schema_name, i_index_name
            USING ERRCODE = '0A000';
    END IF;

    IF l_index.indnkeyatts NOT BETWEEN 1 AND 3 THEN
        RAISE EXCEPTION 'ordered-key index %.% has % key columns; one to three are supported',
            i_schema_name, i_index_name, l_index.indnkeyatts
            USING ERRCODE = '22023';
    END IF;

    -- indoption holds one entry per key column; any non-zero bit is DESC or
    -- NULLS FIRST, which the lexicographic ASC/NULLS LAST predicate cannot
    -- express.
    IF EXISTS (SELECT 1
               FROM pg_catalog.unnest(l_index.indoption) AS o(option)
               WHERE o.option <> 0)
    THEN
        RAISE EXCEPTION 'ordered-key index %.% must be ascending with NULLS LAST',
            i_schema_name, i_index_name
            USING ERRCODE = '0A000';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_ordered_key_index_usable(
    oid,
    text,
    text,
    text
    ) IS
    'Validates the index-level ordered-key contract: the named table exists, the '
        'OID exists, the OID belongs to that table and is the index the name '
        'resolves to, the index is a unique valid/ready/live btree, is not partial '
        'or expression-based, has one to three key columns and is ascending with '
        'NULLS LAST. The per-column checks live in ordered_key_column_attributes.';
