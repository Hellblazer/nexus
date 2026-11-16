CREATE OR REPLACE TRIGGER migration_run_immutable
    BEFORE UPDATE
    ON dml_utils_data.migration_run
    FOR EACH ROW
EXECUTE FUNCTION dml_utils_data.reject_migration_run_update();

COMMENT ON TRIGGER migration_run_immutable ON dml_utils_data.migration_run IS
    'Rejects updates to the migration_run columns fixed at creation (label, chunk size, driving table).';
