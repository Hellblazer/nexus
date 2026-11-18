-- Guard the table: a partial or manual rollback may already have dropped it, and
-- DROP TRIGGER resolves the relation even with IF EXISTS.
DO
$$
    BEGIN
        IF pg_catalog.to_regclass('dml_utils_data.migration_run') IS NOT NULL THEN
            DROP TRIGGER IF EXISTS migration_run_immutable ON dml_utils_data.migration_run;
        END IF;
    END;
$$;
