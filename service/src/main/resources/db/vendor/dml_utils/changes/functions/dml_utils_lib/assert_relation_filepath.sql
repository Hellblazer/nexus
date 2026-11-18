CREATE OR REPLACE FUNCTION dml_utils_lib.assert_relation_filepath(
    i_schema_name dml_utils_data.non_null_text,
    i_table_name dml_utils_data.non_null_text,
    i_expected_filepath text
)
    RETURNS void
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_current_filepath text;
BEGIN
    -- A NULL expectation means the run did not record a fingerprint (a
    -- primary-key run), so there is nothing to check.
    IF i_expected_filepath IS NULL THEN
        RETURN;
    END IF;

    l_current_filepath := dml_utils_lib.relation_filepath(
            i_schema_name => i_schema_name,
            i_table_name => i_table_name);

    -- A block run stores the filepath its boundaries were computed against. If
    -- the heap was rewritten since, the stored block boundaries no longer
    -- describe the same rows, so a resume must fail closed rather than process
    -- unrelated data. This detects rewrites; it cannot detect concurrent DML,
    -- so a block run still requires a quiescent source.
    IF l_current_filepath IS DISTINCT FROM i_expected_filepath THEN
        RAISE EXCEPTION 'relation %.% was rewritten (physical file % is now %); block boundaries are stale',
            i_schema_name, i_table_name, i_expected_filepath, l_current_filepath
            USING ERRCODE = '22023';
    END IF;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.assert_relation_filepath IS
    'Raises invalid_parameter_value (22023) when the current pg_relation_filepath '
        'of the table differs from the recorded one, so a block-chunked run fails '
        'closed after a heap rewrite instead of resuming stale block boundaries. '
        'A NULL recorded filepath (a primary-key run) is a no-op.';
