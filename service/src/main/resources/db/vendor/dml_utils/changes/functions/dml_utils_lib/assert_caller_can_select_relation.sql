CREATE OR REPLACE FUNCTION dml_utils_lib.assert_caller_can_select_relation(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text
)
    RETURNS void
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_caller name;
BEGIN
    -- A SECURITY DEFINER routine must still authorize the driving-table scan with
    -- the effective caller. Recover it from SET ROLE (or the session user) and
    -- check SELECT before the table is read.
    l_caller := COALESCE(NULLIF(pg_catalog.current_setting('role', true), 'none'),
                         session_user)::name;
    IF NOT pg_catalog.has_table_privilege(
            l_caller,
            pg_catalog.format('%I.%I', i_schema_name, i_table_name),
            'SELECT')
    THEN
        RAISE EXCEPTION 'caller lacks SELECT privilege on table %.%',
            i_schema_name, i_table_name
            USING ERRCODE = '42501';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_caller_can_select_relation(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text
    ) IS
    'Raises insufficient_privilege (42501) when the effective caller cannot '
        'SELECT the named relation. Used before a definer-backed scan.';
