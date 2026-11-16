CREATE OR REPLACE FUNCTION ddl_utils_lib.add_foreign_key(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_constraint_name ddl_utils.non_null_text,
    i_column_names ddl_utils.non_empty_non_null_text_array,
    i_referenced_schema_name ddl_utils.non_null_text,
    i_referenced_table_name ddl_utils.non_null_text,
    i_referenced_column_names ddl_utils.non_empty_non_null_text_array,
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
    l_columns            text;
    l_referenced_columns text;
BEGIN
    PERFORM ddl_utils_lib.assert_equal_cardinality(
            i_a => i_column_names,
            i_b => i_referenced_column_names,
            i_context => 'ddl_utils_lib.add_foreign_key: column lists');

    -- The array domains allow blank elements and a non-1 lower bound;
    -- assert_non_blank_elements rejects both before the identifier lists.
    PERFORM ddl_utils_lib.assert_non_blank_elements(
            i_values => i_column_names,
            i_context => 'ddl_utils_lib.add_foreign_key',
            i_label => 'column name');
    PERFORM ddl_utils_lib.assert_non_blank_elements(
            i_values => i_referenced_column_names,
            i_context => 'ddl_utils_lib.add_foreign_key',
            i_label => 'referenced column name');

    l_columns := ddl_utils_lib.quote_identifiers(i_values => i_column_names);
    l_referenced_columns := ddl_utils_lib.quote_identifiers(
            i_values => i_referenced_column_names);

    -- NOT VALID makes the constraint metadata-only (no scan of either table);
    -- call validate_constraint separately to enforce it. Referencing a foreign
    -- key takes SHARE ROW EXCLUSIVE on both tables, not ACCESS EXCLUSIVE.
    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => pg_catalog.format(
                    'ADD CONSTRAINT %I FOREIGN KEY (%s) REFERENCES %I.%I (%s) NOT VALID',
                    i_constraint_name,
                    l_columns,
                    i_referenced_schema_name,
                    i_referenced_table_name,
                    l_referenced_columns
                                      ),
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.add_foreign_key IS
    'Adds a foreign key as NOT VALID, taking the lock settings explicitly.';
