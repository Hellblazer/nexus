CREATE OR REPLACE TRIGGER migration_boundary_immutable
    BEFORE UPDATE
    ON dml_utils_data.migration_boundary
    FOR EACH ROW
EXECUTE FUNCTION dml_utils_data.reject_migration_boundary_update();

COMMENT ON TRIGGER migration_boundary_immutable ON dml_utils_data.migration_boundary IS
    'Rejects updates to the migration_boundary columns fixed when the boundaries are computed.';
