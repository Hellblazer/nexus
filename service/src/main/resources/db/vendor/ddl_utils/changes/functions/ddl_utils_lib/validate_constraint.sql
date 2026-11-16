CREATE OR REPLACE FUNCTION ddl_utils_lib.validate_constraint(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_constraint_name ddl_utils.non_null_text,
    i_ddl_lock_timeout ddl_utils.non_negative_integer,
    i_sleep_time ddl_utils.non_negative_integer,
    i_statement_duration ddl_utils.non_negative_integer
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
BEGIN
    -- This is NOT metadata-only: it scans existing rows to enforce the
    -- constraint, under SHARE UPDATE EXCLUSIVE on the table (which does not
    -- block DML there). Validating a FOREIGN KEY also takes SHARE ROW EXCLUSIVE
    -- on the referenced table, which does block DML on it. Pair it with
    -- add_check_constraint/add_foreign_key ... NOT VALID to enforce a
    -- constraint without holding ACCESS EXCLUSIVE for the scan.
    --
    -- i_ddl_lock_timeout, i_sleep_time and i_statement_duration bound only the
    -- lock-acquisition retry loop in alter_table; they do not cap the duration of
    -- the validation scan itself.
    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => pg_catalog.format('VALIDATE CONSTRAINT %I',
                                                        i_constraint_name),
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.validate_constraint IS
    'Validates a constraint. Takes SHARE UPDATE EXCLUSIVE on the table (does '
        'not block DML there); validating a foreign key also takes SHARE ROW '
        'EXCLUSIVE on the referenced table, which does block DML on it.';
