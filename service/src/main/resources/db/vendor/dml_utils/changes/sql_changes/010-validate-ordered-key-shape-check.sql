-- Validates the ordered-key shape check that the previous changeset added NOT
-- VALID. The procedure adds a constraint only when it is missing, so here it only
-- validates, scanning migration_run under a lock that does not block writes. It
-- commits as it goes, so it must be called outside a transaction block.
CALL ddl_utils.ensure_check_constraint(
    i_schema_name => 'dml_utils_data',
    i_table_name => 'migration_run',
    i_constraint_name => 'migration_run_ordered_key_shape_check',
    i_check_expression => '${ordered_key_shape_check_expression}'
);
