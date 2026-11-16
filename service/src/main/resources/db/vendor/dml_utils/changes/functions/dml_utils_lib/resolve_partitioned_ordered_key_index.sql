CREATE OR REPLACE FUNCTION dml_utils_lib.resolve_partitioned_ordered_key_index(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_index_name dml_utils_data.non_null_text
)
    RETURNS oid
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_index_oid oid;
    l_relkind   text;
BEGIN
    PERFORM dml_utils_lib.assert_schema_exists(i_schema_name => i_schema_name);
    PERFORM dml_utils_lib.assert_table_exists(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name);

    SELECT i.indexrelid, c.relkind::text
    INTO l_index_oid, l_relkind
    FROM pg_catalog.pg_index AS i
             JOIN pg_catalog.pg_class AS ic ON ic.oid = i.indexrelid
             JOIN pg_catalog.pg_class AS c ON c.oid = i.indrelid
             JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = i_schema_name
      AND c.relname = i_table_name
      AND ic.relname = i_index_name;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'index %.% does not exist on table %.%',
            i_schema_name, i_index_name, i_schema_name, i_table_name
            USING ERRCODE = '42704';
    END IF;

    IF l_relkind <> 'p' THEN
        RAISE EXCEPTION 'partition-wise ordered-key chunking requires a partitioned table, but %.% has relkind %',
            i_schema_name, i_table_name, l_relkind
            USING ERRCODE = '0A000';
    END IF;

    -- Validate the parent index before the leaves, so ON ONLY reports the parent
    -- 55000 rather than a misleading missing-child 0A000.
    PERFORM 1
    FROM dml_utils_lib.ordered_key_attributes_for_index(
                 i_index_oid => l_index_oid,
                 i_schema_name => i_schema_name,
                 i_table_name => i_table_name,
                 i_index_name => i_index_name) AS a;

    RETURN l_index_oid;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.resolve_partitioned_ordered_key_index(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) IS
    'Resolves the named unique index on a declarative partitioned parent, checks '
        'the parent relkind and validates the parent index shape, returning its '
        'OID for the leaf walk. A missing index is 42704, a non-partitioned parent '
        'is 0A000 and an unusable parent index is 0A000/55000.';
