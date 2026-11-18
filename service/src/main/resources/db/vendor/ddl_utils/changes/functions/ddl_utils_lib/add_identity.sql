CREATE OR REPLACE FUNCTION ddl_utils_lib.add_identity(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_column_name ddl_utils.non_null_text,
    i_generated ddl_utils.non_null_text,
    i_ddl_lock_timeout ddl_utils.non_negative_integer,
    i_sleep_time ddl_utils.non_negative_integer,
    i_statement_duration ddl_utils.non_negative_integer
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
DECLARE
    l_sequence regclass;
    l_max      bigint;
BEGIN
    -- The generated mode is a two-value keyword set; validate it so a caller
    -- cannot splice arbitrary SQL. It is emitted verbatim (not as an
    -- identifier) because these are unquoted PostgreSQL keywords.
    PERFORM ddl_utils_lib.assert_allowed_keyword(
            i_value => i_generated,
            i_allowed => ARRAY ['always', 'by default'],
            i_context => 'ddl_utils_lib.add_identity: generated');

    -- Adding an identity is metadata-only (like SET DEFAULT) and affects future
    -- rows only. PostgreSQL requires the column to be NOT NULL (it raises 42P16
    -- otherwise).
    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => pg_catalog.format(
                    'ALTER COLUMN %I ADD GENERATED %s AS IDENTITY',
                    i_column_name,
                    pg_catalog.upper(i_generated)
                                      ),
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );

    -- A new identity sequence starts at 1, so a populated column would hand out
    -- duplicate values to later inserts. Move the sequence past the current
    -- maximum. The scan runs in the same transaction as the ALTER (which just
    -- held ACCESS EXCLUSIVE), so no concurrent insert can race it; the scan is
    -- not bounded by the lock settings, so a large table extends the exclusive
    -- lock window.
    l_sequence := pg_catalog.pg_get_serial_sequence(
            pg_catalog.format('%I.%I', i_schema_name, i_table_name), i_column_name)::regclass;
    IF l_sequence IS NULL THEN
        RAISE EXCEPTION 'ddl_utils_lib.add_identity: no identity sequence found for %.%.%',
            i_schema_name, i_table_name, i_column_name
            USING ERRCODE = '55000';
    END IF;

    EXECUTE pg_catalog.format('SELECT pg_catalog.max(%I)::bigint FROM %I.%I',
                              i_column_name, i_schema_name, i_table_name)
        INTO l_max;
    -- The identity sequence starts at 1 with MINVALUE 1. A zero or negative
    -- maximum cannot be passed to setval (it raises "out of bounds"), so treat
    -- it like the empty-table case and start at 1.
    IF l_max IS NULL OR l_max < 1 THEN
        PERFORM pg_catalog.setval(l_sequence, 1, false);
    ELSE
        PERFORM pg_catalog.setval(l_sequence, l_max, true);
    END IF;
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.add_identity IS
    'Adds an identity to a column, taking the lock settings explicitly. The '
        'column must already be NOT NULL; the sequence is advanced past the '
        'column''s current maximum (or started at 1 when the maximum is below 1) '
        'so generated values cannot collide. The max() scan runs under ACCESS '
        'EXCLUSIVE and is not bounded by the lock settings.';
