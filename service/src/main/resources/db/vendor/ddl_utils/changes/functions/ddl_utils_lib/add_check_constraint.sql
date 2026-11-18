CREATE OR REPLACE FUNCTION ddl_utils_lib.add_check_constraint(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_constraint_name ddl_utils.non_null_text,
    i_check_expression ddl_utils.non_null_text,
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
    -- The expression is arbitrary SQL, so reject only a top-level comma that
    -- could append another ALTER TABLE action. NOT VALID makes the constraint
    -- metadata-only (no scan); call validate_constraint separately to enforce
    -- it against existing rows.
    PERFORM ddl_utils_lib.assert_no_top_level_comma(
            i_value => i_check_expression,
            i_context => 'ddl_utils_lib.add_check_constraint: the check expression');

    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => pg_catalog.format(
                    'ADD CONSTRAINT %I CHECK (%s) NOT VALID',
                    i_constraint_name,
                    i_check_expression
                                      ),
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.add_check_constraint IS
    'Adds a CHECK constraint as NOT VALID, taking the lock settings explicitly.';
