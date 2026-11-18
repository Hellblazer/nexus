CREATE OR REPLACE FUNCTION dml_utils_lib.resolve_migration_creation_key(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_chunk_by dml_utils_data.chunking_strategy,
    i_boundary_key_index name,
    i_partition_wise dml_utils_data.non_null_boolean,
    OUT o_key_columns name[],
    OUT o_key_kinds text[],
    OUT o_relation_filepath text,
    OUT o_ordered_key_index name,
    OUT o_ordered_key_columns name[],
    OUT o_ordered_key_kinds text[]
)
    RETURNS record
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_parent_index oid;
BEGIN
    -- A range run reads its key columns and kinds from the primary key or the
    -- named ordered-key index. A non-partition-wise block run instead records the
    -- driving table's physical filepath so a resume can detect a heap rewrite,
    -- and the blocks<->filepath check on migration_run requires exactly one of
    -- the two. A partition-wise block run records one filepath per leaf in the
    -- manifest, so the run-level filepath stays NULL.
    IF i_chunk_by = 'blocks' THEN
        IF NOT i_partition_wise::boolean THEN
            PERFORM dml_utils_lib.assert_plain_heap(
                    i_schema_name => i_schema_name,
                    i_table_name => i_table_name);
            o_relation_filepath := dml_utils_lib.relation_filepath(
                    i_schema_name => i_schema_name,
                    i_table_name => i_table_name);
        END IF;
    ELSIF i_chunk_by = 'ordered_key' THEN
        IF i_partition_wise::boolean THEN
            -- Validate the parent index shape and take its descriptor now. The
            -- leaf child indexes are checked against the manifest after it is
            -- inserted, so the validated set is exactly the stored set.
            l_parent_index := pg_catalog.to_regclass(
                    pg_catalog.format('%I.%I', i_schema_name, i_boundary_key_index));
            IF l_parent_index IS NULL THEN
                RAISE EXCEPTION 'index %.% does not exist on table %.%',
                    i_schema_name, i_boundary_key_index, i_schema_name, i_table_name
                    USING ERRCODE = '42704';
            END IF;

            SELECT pg_catalog.array_agg(a.column_name ORDER BY a.ordinality),
                   pg_catalog.array_agg(a.key_kind ORDER BY a.ordinality)
            INTO o_key_columns, o_key_kinds
            FROM dml_utils_lib.ordered_key_attributes_for_index(
                         i_index_oid => l_parent_index,
                         i_schema_name => i_schema_name,
                         i_table_name => i_table_name,
                         i_index_name => i_boundary_key_index) AS a;
        ELSE
            PERFORM dml_utils_lib.assert_usable_ordered_key(
                    i_schema_name => i_schema_name,
                    i_table_name => i_table_name,
                    i_index_name => i_boundary_key_index);
            o_key_columns := dml_utils_lib.ordered_key_columns(
                    i_schema_name => i_schema_name,
                    i_table_name => i_table_name,
                    i_index_name => i_boundary_key_index);
            o_key_kinds := dml_utils_lib.ordered_key_kinds(
                    i_schema_name => i_schema_name,
                    i_table_name => i_table_name,
                    i_index_name => i_boundary_key_index);
        END IF;
    ELSE
        o_key_columns := dml_utils_lib.primary_key_columns(
                i_schema_name => i_schema_name,
                i_table_name => i_table_name);
        o_key_kinds := dml_utils_lib.primary_key_kinds(
                i_schema_name => i_schema_name,
                i_table_name => i_table_name);
    END IF;

    -- The run-row constraint requires the ordered descriptor all-or-nothing; build
    -- the three values once so the INSERT has no mixed-type CASE expressions.
    IF i_chunk_by = 'ordered_key' THEN
        o_ordered_key_index := i_boundary_key_index;
        o_ordered_key_columns := o_key_columns;
        o_ordered_key_kinds := o_key_kinds;
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.resolve_migration_creation_key(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name,
    dml_utils_data.non_null_boolean
    ) IS
    'Resolves the key descriptor and filepath a new run stores: the primary key '
        'for primary_key, the ordered descriptor for ordered_key (validating the '
        'parent index for a partition-wise run) and the relation filepath for a '
        'single-relation blocks run. Partition-wise blocks leaves the run-level '
        'filepath NULL.';
