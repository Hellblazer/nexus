CREATE OR REPLACE TRIGGER table_lock_settings_set_updated_at
    BEFORE UPDATE
    ON ddl_utils.table_lock_settings
    FOR EACH ROW
EXECUTE FUNCTION ddl_utils.set_updated_at();

COMMENT ON TRIGGER table_lock_settings_set_updated_at ON ddl_utils.table_lock_settings IS
    'Refreshes updated_at on every update of table_lock_settings, regardless of the caller.';
