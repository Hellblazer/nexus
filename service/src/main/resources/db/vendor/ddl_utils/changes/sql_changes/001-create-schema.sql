-- The schema is owned by the role that runs the migration (ddl_utils_owner in
-- the documented setup), so Liquibase must connect as that role.
CREATE SCHEMA IF NOT EXISTS ddl_utils;

COMMENT ON SCHEMA ddl_utils IS
    'Lock-aware DDL surface: lock-settings tables, accessors, domains and wrappers.';
