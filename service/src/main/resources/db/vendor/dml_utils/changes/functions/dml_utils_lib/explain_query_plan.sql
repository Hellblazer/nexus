CREATE OR REPLACE FUNCTION dml_utils_lib.explain_query_plan(
    i_sql_text text
)
    RETURNS json
    LANGUAGE plpgsql
    VOLATILE
    SECURITY INVOKER
AS
$$
DECLARE
    l_plan json;
BEGIN
    -- A NULL would concatenate to the literal "EXPLAIN (FORMAT JSON) " with no
    -- statement, and a blank statement would fail with a syntax error; reject
    -- both up front with a message that names the argument.
    IF i_sql_text IS NULL OR pg_catalog.btrim(i_sql_text) = '' THEN
        RAISE EXCEPTION 'i_sql_text must not be NULL or empty'
            USING ERRCODE = '22023';
    END IF;

    -- EXPLAIN (FORMAT JSON) returns one row with a single json column. Without
    -- ANALYZE it only plans, so the statement is never executed: an INSERT or
    -- UPDATE yields a ModifyTable plan and changes nothing, and an insert whose
    -- foreign key is unsatisfied still plans. EXECUTE accepts a single
    -- statement, so a multi-statement string fails rather than running.
    --
    -- The function is VOLATILE, not STABLE: PostgreSQL rejects EXPLAIN inside a
    -- non-volatile function ("EXPLAIN is not allowed in a non-volatile
    -- function"). It still only plans, so it changes nothing.
    --
    -- The statement is caller-supplied, but the routine is SECURITY INVOKER and
    -- only plans (it cannot execute), so it is no more privileged than running
    -- EXPLAIN on the same SQL directly: it is not an authorization boundary and
    -- needs no provenance check.
    EXECUTE 'EXPLAIN (FORMAT JSON) ' || i_sql_text INTO l_plan;

    RETURN l_plan;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.explain_query_plan IS
    'Returns EXPLAIN (FORMAT JSON) of i_sql_text as a json value without '
        'executing it (no ANALYZE), so an INSERT/UPDATE only plans. Rejects a '
        'NULL or empty statement. The statement is caller-supplied but the '
        'routine is SECURITY INVOKER and only plans, so it is no more privileged '
        'than running EXPLAIN on the same SQL directly.';
