CREATE OR REPLACE FUNCTION ddl_utils.set_updated_at()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
BEGIN
    -- Requires a row-level BEFORE UPDATE trigger on a table that has an
    -- updated_at column. Stamp the row with the transaction timestamp so every
    -- row written by one transaction shares the same updated_at and an UPDATE
    -- cannot supply its own value. This does not cover INSERT (the column
    -- default applies) and a later BEFORE UPDATE trigger could still rewrite it.
    NEW.updated_at := pg_catalog.now();
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION ddl_utils.set_updated_at IS
    'Row-level BEFORE UPDATE trigger that stamps NEW.updated_at with the '
        'transaction timestamp; the table must have an updated_at column.';
