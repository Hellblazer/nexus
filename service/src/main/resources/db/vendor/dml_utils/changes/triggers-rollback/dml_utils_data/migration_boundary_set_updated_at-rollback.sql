-- Guard the table: a partial or manual rollback may already have dropped it.
DO
$$
    BEGIN
        IF pg_catalog.to_regclass('dml_utils_data.migration_boundary') IS NOT NULL THEN
            DROP TRIGGER IF EXISTS migration_boundary_set_updated_at ON dml_utils_data.migration_boundary;
        END IF;
    END;
$$;
