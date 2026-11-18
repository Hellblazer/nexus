REVOKE EXECUTE ON FUNCTION dml_utils_lib.terminal_leaf_for_run(
    bigint
    ) FROM public;
GRANT EXECUTE ON FUNCTION dml_utils_lib.terminal_leaf_for_run(
    bigint
    ) TO dml_utils_caller;
