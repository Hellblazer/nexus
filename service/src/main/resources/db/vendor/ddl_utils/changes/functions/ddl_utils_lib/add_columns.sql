CREATE OR REPLACE FUNCTION ddl_utils_lib.add_columns(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_column_names ddl_utils.non_empty_non_null_text_array,
    i_column_types ddl_utils.non_empty_non_null_text_array,
    i_default_values ddl_utils.non_empty_text_array,
    i_nullable ddl_utils.non_empty_non_null_boolean_array,
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
    l_fragment text    := '';
    l_count    integer := pg_catalog.cardinality(i_column_names);
    l_regtype  regtype;
BEGIN
    IF pg_catalog.cardinality(i_column_types) <> l_count
        OR pg_catalog.cardinality(i_default_values) <> l_count
        OR pg_catalog.cardinality(i_nullable) <> l_count THEN
        RAISE EXCEPTION
            'ddl_utils_lib.add_columns: column arrays must have the same length (columns=%, types=%, defaults=%, nullable=%)',
            l_count,
            pg_catalog.cardinality(i_column_types),
            pg_catalog.cardinality(i_default_values),
            pg_catalog.cardinality(i_nullable)
            USING ERRCODE = '22023';
    END IF;

    -- The array domains constrain cardinality but not the lower bound, so a
    -- caller could pass '[0:1]={a,b}'; the nullable flag is not passed to
    -- assert_non_blank_elements, so reject it here. That helper enforces the
    -- 1-based precondition for the other three arrays.
    PERFORM ddl_utils_lib.assert_one_based(
            i_values => i_nullable,
            i_context => 'ddl_utils_lib.add_columns');

    -- A blank name, type or default cannot produce a valid clause.
    PERFORM ddl_utils_lib.assert_non_blank_elements(
            i_values => i_column_names,
            i_context => 'ddl_utils_lib.add_columns',
            i_label => 'column name');
    PERFORM ddl_utils_lib.assert_non_blank_elements(
            i_values => i_column_types,
            i_context => 'ddl_utils_lib.add_columns',
            i_label => 'type');
    PERFORM ddl_utils_lib.assert_non_blank_elements(
            i_values => i_default_values,
            i_context => 'ddl_utils_lib.add_columns',
            i_label => 'default value');

    FOR l_index IN 1..l_count
        LOOP
        -- The type is spliced in as raw SQL, so require it to resolve to a
        -- single SQL type. This rejects extra clauses such as 'int, DROP COLUMN
        -- x' or 'int DEFAULT 0'.
            l_regtype := NULL;
            BEGIN
                l_regtype := pg_catalog.to_regtype(i_column_types[l_index]);
            EXCEPTION
                WHEN syntax_error THEN
                    l_regtype := NULL;
            END;
            IF l_regtype IS NULL THEN
                RAISE EXCEPTION
                    'ddl_utils_lib.add_columns: the type for column % is not a valid SQL type',
                    i_column_names[l_index]
                    USING ERRCODE = '22023';
            END IF;

            -- A default is an arbitrary expression (now(), coalesce(a, b), ...), so
            -- reject a top-level comma that could append a second ALTER TABLE
            -- action. This is best-effort: a comma-free suffix such as
            -- "0 CHECK (false)" still appends a column constraint and is not
            -- rejected here.
            PERFORM ddl_utils_lib.assert_no_top_level_comma(
                    i_value => i_default_values[l_index],
                    i_context => pg_catalog.format(
                            'ddl_utils_lib.add_columns: the default for column %I',
                            i_column_names[l_index]));

            IF l_index > 1 THEN
                l_fragment := l_fragment || ', ';
            END IF;

            l_fragment := l_fragment || pg_catalog.format(
                    'ADD COLUMN %I %s',
                    i_column_names[l_index],
                    i_column_types[l_index]
                                        );

            -- A NULL default means no DEFAULT clause; a non-null default is raw SQL
            -- (for example now()), so quoted literals must include their quotes.
            IF i_default_values[l_index] IS NOT NULL THEN
                l_fragment :=
                        l_fragment || pg_catalog.format(' DEFAULT %s', i_default_values[l_index]);
            END IF;

            IF NOT i_nullable[l_index] THEN
                l_fragment := l_fragment || ' NOT NULL';
            END IF;
        END LOOP;

    -- All clauses are applied in one ALTER TABLE, so the call is all-or-nothing:
    -- a single invalid or already-existing column fails the whole statement.
    PERFORM ddl_utils_lib.alter_table(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name,
            i_alter_table_fragment => l_fragment,
            i_ddl_lock_timeout => i_ddl_lock_timeout,
            i_sleep_time => i_sleep_time,
            i_statement_duration => i_statement_duration
            );
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.add_columns IS
    'Adds several columns, taking the lock settings explicitly.';
