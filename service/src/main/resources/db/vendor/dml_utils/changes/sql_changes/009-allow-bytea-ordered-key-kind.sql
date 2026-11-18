-- Widens the ordered-key shape check so a run may record bytea as a key kind.
-- The check is replaced through the lock-aware ddl_utils helpers. The new
-- constraint is added NOT VALID (metadata only, and enforced for new rows at
-- once); the next changeset validates it.
SELECT ddl_utils.drop_constraint(
    i_schema_name => 'dml_utils_data',
    i_table_name => 'migration_run',
    i_constraint_name => 'migration_run_ordered_key_shape_check'
);

SELECT ddl_utils.add_check_constraint(
    i_schema_name => 'dml_utils_data',
    i_table_name => 'migration_run',
    i_constraint_name => 'migration_run_ordered_key_shape_check',
    i_check_expression => '${ordered_key_shape_check_expression}'
);

COMMENT ON COLUMN dml_utils_data.migration_run.ordered_key_kinds IS
    'For chunk_by = ordered_key, the boundary kind of each key column in '
        'ordered_key_columns order (bigint, text, uuid, timestamptz or bytea); NULL '
        'for the other strategies. Immutable.';
