CREATE OR REPLACE TRIGGER migration_run_leaf_check
    BEFORE INSERT OR UPDATE
    ON dml_utils_data.migration_run_leaf
    FOR EACH ROW
EXECUTE FUNCTION dml_utils_data.check_migration_run_leaf();

COMMENT ON TRIGGER migration_run_leaf_check ON dml_utils_data.migration_run_leaf IS
    'Requires a partition-wise run, enforces the per-leaf blocks filepath and '
        'rejects changes to the manifest identity snapshot.';
