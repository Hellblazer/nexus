-- Restores the previous check. It is left NOT VALID: a rollback cannot run
-- ddl_utils' validating procedure, which commits and so cannot run inside the
-- rollback transaction. NOT VALID still checks every inserted or updated row, so
-- an existing run whose ordered_key_kinds contains bytea fails every UPDATE with
-- 23514 (check_violation), including the ones that record its progress. Archive
-- and delete such runs before rolling this back.
SELECT ddl_utils.drop_constraint(
    i_schema_name => 'dml_utils_data',
    i_table_name => 'migration_run',
    i_constraint_name => 'migration_run_ordered_key_shape_check'
);

SELECT ddl_utils.add_check_constraint(
    i_schema_name => 'dml_utils_data',
    i_table_name => 'migration_run',
    i_constraint_name => 'migration_run_ordered_key_shape_check',
    i_check_expression => '${ordered_key_shape_check_expression_before_bytea}'
);

COMMENT ON COLUMN dml_utils_data.migration_run.ordered_key_kinds IS
    'For chunk_by = ordered_key, the boundary kind of each key column in '
        'ordered_key_columns order (bigint, text, uuid or timestamptz); NULL for '
        'the other strategies. Immutable.';
