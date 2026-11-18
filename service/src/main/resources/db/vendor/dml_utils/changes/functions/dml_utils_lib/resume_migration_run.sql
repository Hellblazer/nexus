CREATE OR REPLACE FUNCTION dml_utils_lib.resume_migration_run(
    i_run dml_utils_data.migration_run,
    i_driving_table_schema_name dml_utils_data.non_null_text,
    i_driving_table_name dml_utils_data.non_null_text,
    i_sql_text dml_utils_data.non_null_text,
    i_chunk_size dml_utils_data.positive_integer,
    i_threads dml_utils_data.positive_integer,
    i_driving_table_alias dml_utils_data.non_null_text,
    i_chunk_by dml_utils_data.chunking_strategy,
    i_sleep_ms dml_utils_data.non_negative_integer,
    i_boundary_key_index name,
    i_partition_wise dml_utils_data.non_null_boolean,
    OUT o_effective_sql_text text,
    OUT o_effective_schema_name text,
    OUT o_effective_table_name text,
    OUT o_effective_alias text,
    OUT o_effective_threads integer,
    OUT o_effective_sleep_ms integer,
    OUT o_chunk_by dml_utils_data.chunking_strategy,
    OUT o_partition_wise boolean
)
    RETURNS record
    LANGUAGE plpgsql
    VOLATILE
    SECURITY INVOKER
AS
$$
DECLARE
    l_live_columns name[];
    l_live_kinds   text[];
BEGIN
    -- A resumed run uses the SQL, chunk size, strategy, threads and driving
    -- table recorded when it was created, so an adjusted statement (for example
    -- to fix a bad execution plan) is applied via set_migration_run_sql_text
    -- rather than a changed call. A differing input is ignored, with a notice, so
    -- the boundaries and their chunk SQL are never silently redefined.
    o_effective_sql_text := i_run.sql_text;
    o_effective_schema_name := i_run.driving_table_schema_name::text;
    o_effective_table_name := i_run.driving_table_name::text;
    o_effective_alias := i_run.driving_table_alias::text;
    o_chunk_by := i_run.chunk_by;
    o_partition_wise := i_run.partition_wise;
    o_effective_threads := i_run.threads::integer;
    o_effective_sleep_ms := i_run.sleep_ms::integer;

    IF i_run.chunk_by IS DISTINCT FROM i_chunk_by THEN
        RAISE NOTICE 'run for label % already exists; using the stored chunk_by %',
            i_run.label, i_run.chunk_by;
    END IF;
    IF i_run.partition_wise IS DISTINCT FROM i_partition_wise::boolean THEN
        RAISE NOTICE 'run for label % already exists; using the stored partition_wise %',
            i_run.label, i_run.partition_wise;
    END IF;

    -- The stored strategy and descriptor win on resume, like the other stored
    -- inputs. The boundaries were built from the stored index, so fail closed if
    -- it is no longer usable or resolves to a different descriptor: a dropped,
    -- invalid, partial, reordered or differently-typed index would otherwise let
    -- the run continue over a key it no longer matches.
    IF i_run.chunk_by = 'ordered_key' THEN
        IF i_run.ordered_key_index IS DISTINCT FROM i_boundary_key_index THEN
            RAISE NOTICE 'run for label % already exists; using the stored ordered_key_index %',
                i_run.label, i_run.ordered_key_index;
        END IF;

        BEGIN
            -- One traversal validates the stored index (and, for a partition-wise
            -- run, every leaf child index) and yields the live descriptor, which
            -- must equal the stored one.
            IF i_run.partition_wise THEN
                SELECT pg_catalog.array_agg(a.column_name ORDER BY a.ordinality),
                       pg_catalog.array_agg(a.key_kind ORDER BY a.ordinality)
                INTO l_live_columns, l_live_kinds
                FROM dml_utils_lib.partitioned_ordered_key_attributes(
                             i_schema_name => o_effective_schema_name,
                             i_table_name => o_effective_table_name,
                             i_index_name => i_run.ordered_key_index) AS a;
            ELSE
                l_live_columns := dml_utils_lib.ordered_key_columns(
                        i_schema_name => o_effective_schema_name,
                        i_table_name => o_effective_table_name,
                        i_index_name => i_run.ordered_key_index);
                l_live_kinds := dml_utils_lib.ordered_key_kinds(
                        i_schema_name => o_effective_schema_name,
                        i_table_name => o_effective_table_name,
                        i_index_name => i_run.ordered_key_index);
            END IF;

            IF l_live_columns IS DISTINCT FROM i_run.ordered_key_columns
                OR l_live_kinds IS DISTINCT FROM i_run.ordered_key_kinds
            THEN
                RAISE EXCEPTION 'ordered-key index %.% no longer matches the stored key descriptor',
                    o_effective_schema_name, i_run.ordered_key_index
                    USING ERRCODE = '55000';
            END IF;
        EXCEPTION
            WHEN undefined_object
                OR feature_not_supported
                OR wrong_object_type
                OR invalid_parameter_value
                OR undefined_table
                OR invalid_schema_name THEN
                -- The resolver's specific code is useful for a new run, but a
                -- resume must fail with one stable code: the stored index is no
                -- longer usable for these boundaries. A lost leaf privilege
                -- (42501) is intentionally not normalized: the operator needs the
                -- privilege error.
                RAISE EXCEPTION 'ordered-key index %.% is no longer usable for run %: %',
                    o_effective_schema_name, i_run.ordered_key_index, i_run.run_id, SQLERRM
                    USING ERRCODE = '55000';
        END;
    ELSIF i_boundary_key_index IS NOT NULL THEN
        RAISE NOTICE 'run for label % already exists; ignoring i_boundary_key_index for chunk_by %',
            i_run.label, i_run.chunk_by;
    END IF;

    IF i_run.driving_table_alias IS DISTINCT FROM i_driving_table_alias THEN
        RAISE NOTICE 'run for label % already exists; using the stored driving table alias %',
            i_run.label, i_run.driving_table_alias;
    END IF;
    IF i_run.sql_text IS DISTINCT FROM i_sql_text THEN
        RAISE NOTICE 'run for label % already exists; using the stored sql_text', i_run.label;
    END IF;
    IF i_run.chunk_size IS DISTINCT FROM i_chunk_size THEN
        RAISE NOTICE 'run for label % already exists; using the stored chunk_size %',
            i_run.label, i_run.chunk_size;
    END IF;
    IF i_run.threads IS DISTINCT FROM i_threads::integer THEN
        RAISE NOTICE 'run for label % already exists; using the stored threads %',
            i_run.label, i_run.threads;
    END IF;
    IF i_run.sleep_ms IS DISTINCT FROM i_sleep_ms THEN
        RAISE NOTICE 'run for label % already exists; using the stored sleep_ms %',
            i_run.label, i_run.sleep_ms;
    END IF;
    IF i_run.driving_table_schema_name IS DISTINCT FROM i_driving_table_schema_name
        OR i_run.driving_table_name IS DISTINCT FROM i_driving_table_name
    THEN
        RAISE NOTICE 'run for label % already exists; using the stored driving table %.%',
            i_run.label, i_run.driving_table_schema_name, i_run.driving_table_name;
    END IF;

    -- A block run records the physical filepath its boundaries were computed
    -- against; if the heap was rewritten since, the stored block boundaries are
    -- stale, so fail closed rather than resume against unrelated data. A
    -- primary-key run stored a NULL filepath, so this is a no-op for it.
    PERFORM dml_utils_lib.assert_relation_filepath(
            i_schema_name => o_effective_schema_name,
            i_table_name => o_effective_table_name,
            i_expected_filepath => i_run.driving_table_relation_filepath);
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.resume_migration_run(
    dml_utils_data.migration_run,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    dml_utils_data.non_negative_integer,
    name,
    dml_utils_data.non_null_boolean
    ) IS
    'Resolves the effective inputs of a resumed run from its stored row, emits '
        'notices for differing call inputs, re-validates a stored ordered-key '
        'descriptor (failing closed with 55000) and asserts the stored blocks '
        'relation filepath. Returns the effective SQL/schema/table/alias/threads/'
        'sleep/strategy/partition-wise values.';
