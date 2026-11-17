-- Widens the ordered-key shape check so a run may record an ordered key of up to
-- four columns (it allowed three). The check is replaced through the lock-aware
-- ddl_utils helpers, as in 009. The new constraint is added NOT VALID (metadata
-- only, and enforced for new rows at once); the next changeset validates it.
SELECT ddl_utils.drop_constraint(
    i_schema_name => 'dml_utils_data',
    i_table_name => 'migration_run',
    i_constraint_name => 'migration_run_ordered_key_shape_check'
);

SELECT ddl_utils.add_check_constraint(
    i_schema_name => 'dml_utils_data',
    i_table_name => 'migration_run',
    i_constraint_name => 'migration_run_ordered_key_shape_check',
    i_check_expression => '${ordered_key_shape_check_expression_four_columns}'
);
