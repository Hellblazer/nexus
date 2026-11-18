CREATE OR REPLACE FUNCTION dml_utils_data.set_updated_at()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
BEGIN
    -- Stamp the row with the transaction timestamp so every row written by one
    -- transaction shares the same updated_at, and a caller cannot override it.
    NEW.updated_at := pg_catalog.now();
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION dml_utils_data.set_updated_at IS
    'BEFORE UPDATE trigger that stamps NEW.updated_at with the transaction timestamp.';
