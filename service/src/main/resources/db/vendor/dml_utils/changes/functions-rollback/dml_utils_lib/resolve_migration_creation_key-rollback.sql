DROP FUNCTION IF EXISTS dml_utils_lib.resolve_migration_creation_key(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name,
    dml_utils_data.non_null_boolean
);
