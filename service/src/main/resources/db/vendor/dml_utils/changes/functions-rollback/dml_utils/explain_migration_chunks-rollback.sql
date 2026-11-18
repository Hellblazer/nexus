DROP FUNCTION IF EXISTS dml_utils.explain_migration_chunks(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name
);
