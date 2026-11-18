-- Caller grants for the dml_utils objects. The changeset is runOnChange, so
-- keep the statements idempotent.
GRANT USAGE ON SCHEMA dml_utils TO dml_utils_caller;
GRANT USAGE ON SCHEMA dml_utils_lib TO dml_utils_caller;
GRANT USAGE ON SCHEMA dml_utils_data TO dml_utils_caller;

-- PostgreSQL grants USAGE on types and domains to PUBLIC by default. Revoke
-- that and grant it only to the caller, so the application's types follow the
-- same least-privilege rule as its tables and routines.
REVOKE USAGE ON DOMAIN dml_utils_data.non_null_text FROM public;
GRANT USAGE ON DOMAIN dml_utils_data.non_null_text TO dml_utils_caller;
REVOKE USAGE ON DOMAIN dml_utils_data.positive_integer FROM public;
GRANT USAGE ON DOMAIN dml_utils_data.positive_integer TO dml_utils_caller;
REVOKE USAGE ON DOMAIN dml_utils_data.non_negative_integer FROM public;
GRANT USAGE ON DOMAIN dml_utils_data.non_negative_integer TO dml_utils_caller;
REVOKE USAGE ON DOMAIN dml_utils_data.non_null_boolean FROM public;
GRANT USAGE ON DOMAIN dml_utils_data.non_null_boolean TO dml_utils_caller;
REVOKE USAGE ON TYPE dml_utils_data.migration_key FROM public;
GRANT USAGE ON TYPE dml_utils_data.migration_key TO dml_utils_caller;
REVOKE USAGE ON TYPE dml_utils_data.chunking_strategy FROM public;
GRANT USAGE ON TYPE dml_utils_data.chunking_strategy TO dml_utils_caller;

-- The default ACLs for future objects are installed by 007-default-privileges,
-- before the functions include, so every routine is created already protected
-- instead of becoming world-executable until this changeset runs.

-- The caller can read metadata but never write it directly. The invoker-owned
-- coordinator and chunk worker delegate every metadata write to the owner-owned
-- SECURITY DEFINER helpers below. REVOKE ALL also removes TRUNCATE, REFERENCES
-- and TRIGGER, which would otherwise still satisfy a SELECT-only grant.
REVOKE ALL ON dml_utils_data.migration_run FROM public;
REVOKE ALL ON dml_utils_data.migration_run FROM dml_utils_caller;
GRANT SELECT ON dml_utils_data.migration_run TO dml_utils_caller;
REVOKE ALL ON dml_utils_data.migration_boundary FROM public;
REVOKE ALL ON dml_utils_data.migration_boundary FROM dml_utils_caller;
GRANT SELECT ON dml_utils_data.migration_boundary TO dml_utils_caller;
REVOKE ALL ON dml_utils_data.migration_error FROM public;
REVOKE ALL ON dml_utils_data.migration_error FROM dml_utils_caller;
GRANT SELECT ON dml_utils_data.migration_error TO dml_utils_caller;
REVOKE ALL ON dml_utils_data.migration_run_leaf FROM public;
REVOKE ALL ON dml_utils_data.migration_run_leaf FROM dml_utils_caller;
GRANT SELECT ON dml_utils_data.migration_run_leaf TO dml_utils_caller;

-- Table row types are part of the public API (for example
-- resume_migration_run(dml_utils_data.migration_run, ...)), so revoke their
-- implicit PUBLIC USAGE and grant it explicitly like the other types.
REVOKE USAGE ON TYPE dml_utils_data.migration_run FROM public;
GRANT USAGE ON TYPE dml_utils_data.migration_run TO dml_utils_caller;
REVOKE USAGE ON TYPE dml_utils_data.migration_boundary FROM public;
GRANT USAGE ON TYPE dml_utils_data.migration_boundary TO dml_utils_caller;
REVOKE USAGE ON TYPE dml_utils_data.migration_error FROM public;
GRANT USAGE ON TYPE dml_utils_data.migration_error TO dml_utils_caller;
REVOKE USAGE ON TYPE dml_utils_data.migration_run_leaf FROM public;
GRANT USAGE ON TYPE dml_utils_data.migration_run_leaf TO dml_utils_caller;

-- Metadata write helpers called by the invoker-owned coordinator/worker.
-- Note: these are callable by the caller because the same role runs the
-- invoker-owned SQL; this centralizes writes but is not a hard authorization
-- boundary against a malicious chunk template.
REVOKE EXECUTE ON FUNCTION dml_utils_lib.claim_migration_boundary(bigint, bigint)
    FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.claim_migration_boundary(bigint, bigint)
    TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.complete_migration_boundary(bigint, bigint, timestamptz)
    FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.complete_migration_boundary(bigint, bigint, timestamptz)
    TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.complete_migration_run(bigint)
    FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.complete_migration_run(bigint)
    TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.mark_migration_run_cancelled(bigint)
    FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.mark_migration_run_cancelled(bigint)
    TO dml_utils_caller;

-- Trigger functions are invoked by the trigger machinery, not by callers, so
-- revoke PUBLIC EXECUTE and grant it to no one.
REVOKE EXECUTE ON FUNCTION dml_utils_data.set_updated_at() FROM public;
REVOKE EXECUTE ON FUNCTION dml_utils_data.reject_migration_run_update() FROM public;
REVOKE EXECUTE ON FUNCTION dml_utils_data.reject_migration_boundary_update() FROM public;
REVOKE EXECUTE ON FUNCTION dml_utils_data.check_migration_run_leaf() FROM public;
REVOKE EXECUTE ON FUNCTION dml_utils_data.check_migration_boundary_leaf() FROM public;

-- Functions grant EXECUTE to PUBLIC by default; revoke it and grant only to
-- the caller role, so execution is explicit.
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_schema_exists(
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_schema_exists(
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_table_exists(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_table_exists(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_plain_heap(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_plain_heap(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_partitioned_table(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_partitioned_table(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.leaf_partitions(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.leaf_partitions(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.primary_key_columns(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.primary_key_columns(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.primary_key_attributes(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.primary_key_attributes(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.primary_key_kinds(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.primary_key_kinds(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.ordered_key_attributes(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.ordered_key_attributes(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.ordered_key_columns(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.ordered_key_columns(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.ordered_key_kinds(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.ordered_key_kinds(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_usable_ordered_key(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_usable_ordered_key(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.ordered_key_attributes_for_index(
    oid,
    text,
    text,
    text
    ) FROM public;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.ordered_key_leaf_indexes(oid) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.ordered_key_leaf_indexes(oid) TO dml_utils_caller;
GRANT EXECUTE ON FUNCTION dml_utils_lib.ordered_key_attributes_for_index(
    oid,
    text,
    text,
    text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.partitioned_ordered_key_attributes(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.partitioned_ordered_key_attributes(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.partitioned_ordered_key_columns(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.partitioned_ordered_key_columns(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.partitioned_ordered_key_kinds(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.partitioned_ordered_key_kinds(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_partitioned_ordered_key_leaves(
    bigint,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_partitioned_ordered_key_leaves(
    bigint,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_boundary_key_index(
    dml_utils_data.chunking_strategy,
    name
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_boundary_key_index(
    dml_utils_data.chunking_strategy,
    name
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.migration_key_values(
    dml_utils_data.migration_key,
    text[]
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.migration_key_values(
    dml_utils_data.migration_key,
    text[]
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.migration_key_pair_values(
    dml_utils_data.migration_key,
    dml_utils_data.migration_key,
    text[]
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.migration_key_pair_values(
    dml_utils_data.migration_key,
    dml_utils_data.migration_key,
    text[]
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.migration_key_is_canonical(
    dml_utils_data.migration_key
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.migration_key_is_canonical(
    dml_utils_data.migration_key
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.build_function_chunk_template(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.build_function_chunk_template(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_chunking_template(
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_chunking_template(
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.resolve_migration_run(
    dml_utils_data.non_null_text,
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
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.resolve_migration_run(
    dml_utils_data.non_null_text,
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
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.prepare_chunk_sql_render(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[]
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.prepare_chunk_sql_render(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[]
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.render_prepared_chunk_sql(
    text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    text[],
    text[],
    text[],
    text[],
    text[],
    boolean
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.render_prepared_chunk_sql(
    text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    text[],
    text[],
    text[],
    text[],
    text[],
    boolean
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.render_chunk_sql(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    text[],
    text[],
    boolean
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.render_chunk_sql(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    text[],
    text[],
    boolean
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_no_active_run_for_label(
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_no_active_run_for_label(
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.populate_migration_boundaries(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    dml_utils_data.chunking_strategy,
    dml_utils_data.non_null_text,
    dml_utils_data.non_negative_integer,
    name,
    dml_utils_data.non_null_boolean
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.populate_migration_boundaries(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    dml_utils_data.chunking_strategy,
    dml_utils_data.non_null_text,
    dml_utils_data.non_negative_integer,
    name,
    dml_utils_data.non_null_boolean
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.migration_run_calculation_lock_key(bigint) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.migration_run_calculation_lock_key(bigint) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_migration_tree_unchanged(bigint) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_migration_tree_unchanged(bigint) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.calculate_migration_run_boundaries(bigint) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.calculate_migration_run_boundaries(bigint) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.record_migration_leaf_error(
    bigint,
    integer,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.record_migration_leaf_error(
    bigint,
    integer,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.process_migration_chunk(
    bigint,
    bigint,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.process_migration_chunk(
    bigint,
    bigint,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.record_migration_error(
    bigint,
    bigint,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.record_migration_error(
    bigint,
    bigint,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.build_boundary_population_sql(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    bigint,
    bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.build_boundary_population_sql(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    bigint,
    bigint
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.build_block_boundary_population_sql(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    bigint,
    bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.build_block_boundary_population_sql(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    bigint,
    bigint
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.boundary_key_descriptor_sql(
    name[],
    text[]
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.boundary_key_descriptor_sql(
    name[],
    text[]
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.build_leaf_boundary_population_sql(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    bigint,
    bigint,
    integer
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.build_leaf_boundary_population_sql(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    bigint,
    bigint,
    integer
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.build_block_leaf_boundary_population_sql(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    bigint,
    bigint,
    integer
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.build_block_leaf_boundary_population_sql(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    bigint,
    bigint,
    integer
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.migration_run_boundary_key(bigint) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.migration_run_boundary_key(bigint) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_migration_leaf_unchanged(
    bigint,
    integer
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_migration_leaf_unchanged(
    bigint,
    integer
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.leaf_terminal_boundary_id(
    bigint,
    integer
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.leaf_terminal_boundary_id(
    bigint,
    integer
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.calculate_migration_leaf(bigint) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.calculate_migration_leaf(bigint) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.finalize_migration_boundaries(bigint) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.finalize_migration_boundaries(bigint) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.render_block_chunk_sql(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    bigint,
    bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.render_block_chunk_sql(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    bigint,
    bigint
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.relation_filepath(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.relation_filepath(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_relation_filepath(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_relation_filepath(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.synthetic_chunk_boundary_values(
    text[],
    bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.synthetic_chunk_boundary_values(
    text[],
    bigint
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.explain_query_plan(
    text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.explain_query_plan(
    text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.explain_chunk_plans(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    bigint,
    dml_utils_data.chunking_strategy
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.explain_chunk_plans(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    bigint,
    dml_utils_data.chunking_strategy
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.explain_chunk_plan(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    text[],
    text[],
    boolean
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.explain_chunk_plan(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    text[],
    text[],
    boolean
    ) TO dml_utils_caller;

REVOKE EXECUTE ON FUNCTION dml_utils.run_migration_chunks(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    dml_utils_data.positive_integer,
    dml_utils_data.non_negative_integer,
    name,
    dml_utils_data.non_null_boolean
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.run_migration_chunks(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    dml_utils_data.positive_integer,
    dml_utils_data.non_negative_integer,
    name,
    dml_utils_data.non_null_boolean
    ) TO dml_utils_caller;
-- Procedures: the shared coordinator and its caller-facing committing entry point.
REVOKE EXECUTE ON PROCEDURE dml_utils_lib.coordinate_migration_chunks(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    dml_utils_data.positive_integer,
    dml_utils_data.non_negative_integer,
    name,
    dml_utils_data.non_null_boolean,
    boolean
    ) FROM public;
GRANT EXECUTE ON PROCEDURE dml_utils_lib.coordinate_migration_chunks(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    dml_utils_data.positive_integer,
    dml_utils_data.non_negative_integer,
    name,
    dml_utils_data.non_null_boolean,
    boolean
    ) TO dml_utils_caller;
REVOKE EXECUTE ON PROCEDURE dml_utils.run_migration_chunks_committing(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    dml_utils_data.positive_integer,
    dml_utils_data.non_negative_integer,
    name,
    dml_utils_data.non_null_boolean
    ) FROM public;
GRANT EXECUTE ON PROCEDURE dml_utils.run_migration_chunks_committing(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    dml_utils_data.positive_integer,
    dml_utils_data.non_negative_integer,
    name,
    dml_utils_data.non_null_boolean
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.run_function_over_table(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    text,
    text,
    dml_utils_data.chunking_strategy,
    dml_utils_data.positive_integer,
    dml_utils_data.non_negative_integer
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.run_function_over_table(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    text,
    text,
    dml_utils_data.chunking_strategy,
    dml_utils_data.positive_integer,
    dml_utils_data.non_negative_integer
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.explain_migration_chunks(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.explain_migration_chunks(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.explain_migration_chunk(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    text[],
    text[],
    boolean,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.explain_migration_chunk(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    text[],
    text[],
    boolean,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.explain_function_over_table(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    text,
    dml_utils_data.chunking_strategy
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.explain_function_over_table(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    text,
    dml_utils_data.chunking_strategy
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.explain_function_over_table_chunk(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    text[],
    text[],
    boolean,
    text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.explain_function_over_table_chunk(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    text[],
    text[],
    boolean,
    text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.set_migration_run_function(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.set_migration_run_function(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.set_migration_run_sql_text(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.set_migration_run_sql_text(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.set_migration_run_threads(
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.set_migration_run_threads(
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.set_migration_run_sleep_ms(
    dml_utils_data.non_null_text,
    dml_utils_data.non_negative_integer
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.set_migration_run_sleep_ms(
    dml_utils_data.non_null_text,
    dml_utils_data.non_negative_integer
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.archive_migration_run(
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.archive_migration_run(
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.cancel_migration_run(
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.cancel_migration_run(
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.delete_archived_migration_runs() FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.delete_archived_migration_runs() TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.delete_archived_migration_runs(
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.delete_archived_migration_runs(
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.migration_run_summary(
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.migration_run_summary(
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.migration_errors(
    bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.migration_errors(
    bigint
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils.migration_boundaries(
    bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils.migration_boundaries(
    bigint
    ) TO dml_utils_caller;

-- Render/boundary-build helpers split out of the public engine routines.
REVOKE EXECUTE ON FUNCTION dml_utils_lib.build_key_render_columns(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[]
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.build_key_render_columns(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[]
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.build_chunk_boundary_literals(
    text[], text[], text[], text[], text[]
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.build_chunk_boundary_literals(
    text[], text[], text[], text[], text[]
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.build_chunk_range_boxes(
    text[], text[], text[], integer, boolean
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.build_chunk_range_boxes(
    text[], text[], text[], integer, boolean
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.boundary_key_pack_expression(text[])
    FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.boundary_key_pack_expression(text[])
    TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.format_boundary_population_sql(
    text, text, text, text, text, text, text, bigint, bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.format_boundary_population_sql(
    text, text, text, text, text, text, text, bigint, bigint
    ) TO dml_utils_caller;

-- Ordered-key and partition introspection helpers.
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_ordered_key_index_usable(
    oid,
    text,
    text,
    text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_ordered_key_index_usable(
    oid,
    text,
    text,
    text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.ordered_key_column_attributes(
    oid,
    text,
    text,
    text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.ordered_key_column_attributes(
    oid,
    text,
    text,
    text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.resolve_partitioned_ordered_key_index(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.resolve_partitioned_ordered_key_index(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_partitioned_ordered_key_child_indexes(
    oid,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_partitioned_ordered_key_child_indexes(
    oid,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_partitioned_leaf_relations(
    oid
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_partitioned_leaf_relations(
    oid
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_partitioned_leaf_privileges(
    oid
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_partitioned_leaf_privileges(
    oid
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.manifest_leaf_identity(
    bigint,
    integer
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.manifest_leaf_identity(
    bigint,
    integer
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_live_leaf_matches_manifest(
    dml_utils_data.chunking_strategy,
    text,
    text,
    oid,
    text,
    text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_live_leaf_matches_manifest(
    dml_utils_data.chunking_strategy,
    text,
    text,
    oid,
    text,
    text
    ) TO dml_utils_caller;

-- Function-template and explain helpers.
REVOKE EXECUTE ON FUNCTION dml_utils_lib.resolve_function_chunk_target(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.resolve_function_chunk_target(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.explain_block_chunk_plans(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.explain_block_chunk_plans(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    bigint
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.explain_key_chunk_plans(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.explain_key_chunk_plans(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    bigint
    ) TO dml_utils_caller;

-- Leaf calculation and finalization helpers.
REVOKE EXECUTE ON FUNCTION dml_utils_lib.claim_next_migration_leaf(
    bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.claim_next_migration_leaf(
    bigint
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.calculate_next_leaf_in_worker(
    bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.calculate_next_leaf_in_worker(
    bigint
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.finalize_calculated_run(
    bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.finalize_calculated_run(
    bigint
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.terminal_leaf_for_run(
    bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.terminal_leaf_for_run(
    bigint
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.append_terminal_boundary(
    bigint,
    integer,
    text,
    text,
    dml_utils_data.migration_key
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.append_terminal_boundary(
    bigint,
    integer,
    text,
    text,
    dml_utils_data.migration_key
    ) TO dml_utils_caller;

-- Leaf calculation SQL builder.
REVOKE EXECUTE ON FUNCTION dml_utils_lib.leaf_boundary_population_sql(
    dml_utils_data.chunking_strategy,
    integer,
    text,
    text,
    bigint,
    integer
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.leaf_boundary_population_sql(
    dml_utils_data.chunking_strategy,
    integer,
    text,
    text,
    bigint,
    integer
    ) TO dml_utils_caller;

-- Run creation and resolution helpers.
REVOKE EXECUTE ON FUNCTION dml_utils_lib.assert_caller_can_select_relation(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.assert_caller_can_select_relation(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.resolve_migration_creation_key(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name,
    dml_utils_data.non_null_boolean
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.resolve_migration_creation_key(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name,
    dml_utils_data.non_null_boolean
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.snapshot_partition_leaf_manifest(
    bigint,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.snapshot_partition_leaf_manifest(
    bigint,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.populate_single_relation_boundaries(
    bigint,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name[],
    text[],
    dml_utils_data.positive_integer
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.populate_single_relation_boundaries(
    bigint,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name[],
    text[],
    dml_utils_data.positive_integer
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.find_active_migration_run(
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.find_active_migration_run(
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.resume_migration_run(
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
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.resume_migration_run(
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
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.create_migration_run(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    dml_utils_data.chunking_strategy,
    dml_utils_data.non_null_text,
    dml_utils_data.non_negative_integer,
    name,
    dml_utils_data.non_null_boolean
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.create_migration_run(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    dml_utils_data.chunking_strategy,
    dml_utils_data.non_null_text,
    dml_utils_data.non_negative_integer,
    name,
    dml_utils_data.non_null_boolean
    ) TO dml_utils_caller;

-- Chunk-runner coordinator helpers.
REVOKE EXECUTE ON FUNCTION dml_utils_lib.prepare_partition_wise_execution(
    bigint,
    dml_utils_data.non_null_text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.prepare_partition_wise_execution(
    bigint,
    dml_utils_data.non_null_text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.next_migration_chunk(
    bigint,
    boolean,
    text[],
    bigint[],
    text,
    text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.next_migration_chunk(
    bigint,
    boolean,
    text[],
    bigint[],
    text,
    text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.record_migration_worker_failure(
    bigint,
    bigint,
    text,
    text
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.record_migration_worker_failure(
    bigint,
    bigint,
    text,
    text
    ) TO dml_utils_caller;
REVOKE EXECUTE ON FUNCTION dml_utils_lib.sleep_between_migration_chunks(
    bigint,
    integer
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.sleep_between_migration_chunks(
    bigint,
    integer
    ) TO dml_utils_caller;

-- Worker stop/drain helper.
REVOKE EXECUTE ON FUNCTION dml_utils_lib.stop_migration_workers(
    integer[],
    bigint[],
    boolean,
    integer
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.stop_migration_workers(
    integer[],
    bigint[],
    boolean,
    integer
    ) TO dml_utils_caller;
