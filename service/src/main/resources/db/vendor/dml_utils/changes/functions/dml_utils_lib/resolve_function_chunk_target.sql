CREATE OR REPLACE FUNCTION dml_utils_lib.resolve_function_chunk_target(
    i_table_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_function_schema_name dml_utils_data.non_null_text,
    i_function_name dml_utils_data.non_null_text,
    OUT o_primary_key_columns name[],
    OUT o_call_arguments text
)
    RETURNS record
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_argument_oids      oid[];
    l_function_not_void  boolean;
    l_argument_type_list text;
BEGIN
    -- Resolve the driving table's primary key in key order; this also validates
    -- the schema/table exist and that the key has one to three supported columns.
    o_primary_key_columns := dml_utils_lib.primary_key_columns(
            i_schema_name => i_table_schema_name,
            i_table_name => i_table_name);

    -- The function's argument types must equal the primary-key column types, in
    -- key order, and it must return void. Read the column type oids (atttypid)
    -- from the catalog, because the engine's key *kinds* collapse the integer
    -- family to bigint while a function's declared argument type must match the
    -- column's actual type.
    SELECT pg_catalog.array_agg(a.column_oid ORDER BY a.ordinality)
    INTO l_argument_oids
    FROM dml_utils_lib.primary_key_attributes(
                 i_schema_name => i_table_schema_name,
                 i_table_name => i_table_name) AS a;

    IF pg_catalog.cardinality(l_argument_oids) IS DISTINCT FROM pg_catalog.array_length(o_primary_key_columns, 1)
    THEN
        RAISE EXCEPTION 'primary key of table %.% changed while reading its columns',
            i_table_schema_name, i_table_name
            USING ERRCODE = '22023';
    END IF;

    -- Look up the function by exact name, arity and argument types. proargtypes
    -- is a 0-based oidvector of the input argument types (IN/INOUT/VARIADIC);
    -- normalise it to a 1-based array before comparing, because PostgreSQL array
    -- equality is by dimensions and l_argument_oids comes from array_agg (lower
    -- bound 1). Matching the types in the predicate, not after the fact, also
    -- resolves overloads: a same-name/same-arity sibling with different argument
    -- types is not selected, so a valid overload is still found when others
    -- exist. Duplicate signatures are impossible, so this yields at most one row.
    -- Restrict to an ordinary, non-set-returning function (prokind 'f',
    -- proretset false): a procedure or aggregate can share the name, arguments
    -- and a void return but cannot be called as SELECT schema.name(...), and a
    -- RETURNS SETOF void function would expand rows instead of returning exactly
    -- one value per driving row. (Ordinary functions used as window functions
    -- also have prokind 'f'.)
    SELECT p.prorettype <> 'void'::pg_catalog.regtype
    INTO l_function_not_void
    FROM pg_catalog.pg_proc AS p
             JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
    WHERE n.nspname = i_function_schema_name
      AND p.proname = i_function_name
      AND p.prokind = 'f'
      AND NOT p.proretset
      AND p.pronargs = pg_catalog.cardinality(l_argument_oids)
      AND (SELECT pg_catalog.array_agg(x ORDER BY o)
           FROM pg_catalog.unnest(p.proargtypes::oid[]) WITH ORDINALITY AS u(x, o)) IS NOT DISTINCT FROM l_argument_oids;

    IF NOT FOUND THEN
        SELECT pg_catalog.string_agg(t::pg_catalog.regtype::text, ', ' ORDER BY o)
        INTO l_argument_type_list
        FROM pg_catalog.unnest(l_argument_oids) WITH ORDINALITY AS a(t, o);

        RAISE EXCEPTION 'function %.%(%) does not exist',
            i_function_schema_name, i_function_name, l_argument_type_list
            USING ERRCODE = '22023';
    END IF;

    IF l_function_not_void THEN
        RAISE EXCEPTION 'function %.% must return void',
            i_function_schema_name, i_function_name
            USING ERRCODE = '22023';
    END IF;

    -- Build the call argument list. Identifiers are catalog names, quoted with
    -- %I; the table alias is fixed at the engine's default 't'.
    o_call_arguments := '';
    FOR l_position IN 1..pg_catalog.array_length(o_primary_key_columns, 1)
        LOOP
            o_call_arguments := o_call_arguments
                || pg_catalog.format('%st.%I',
                                     CASE WHEN l_position > 1 THEN ', ' ELSE '' END,
                                     o_primary_key_columns[l_position]);
        END LOOP;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.resolve_function_chunk_target(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) IS
    'Resolves the per-row function target for run_function_over_table: returns '
        'the driving table''s primary-key columns and the ''t.<column>'' call '
        'argument list, after checking the function exists, is an ordinary '
        'function, returns void and takes exactly the primary-key column types in '
        'key order (22023 otherwise). build_function_chunk_template owns the '
        'statement text.';
