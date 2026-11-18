CREATE OR REPLACE TRIGGER migration_run_leaf_set_updated_at
    BEFORE UPDATE
    ON dml_utils_data.migration_run_leaf
    FOR EACH ROW
EXECUTE FUNCTION dml_utils_data.set_updated_at();

COMMENT ON TRIGGER migration_run_leaf_set_updated_at ON dml_utils_data.migration_run_leaf IS
    'Refreshes migration_run_leaf.updated_at on every update.';
