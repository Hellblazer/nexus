CREATE OR REPLACE TRIGGER migration_boundary_leaf
    BEFORE INSERT OR UPDATE
    ON dml_utils_data.migration_boundary
    FOR EACH ROW
EXECUTE FUNCTION dml_utils_data.check_migration_boundary_leaf();

COMMENT ON TRIGGER migration_boundary_leaf ON dml_utils_data.migration_boundary IS
    'Requires leaf_ordinal for a partition-wise run boundary and rejects it for '
        'a single-relation run.';
