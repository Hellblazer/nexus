DROP FUNCTION IF EXISTS dml_utils_lib.populate_migration_boundaries(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.positive_integer,
    dml_utils_data.positive_integer,
    dml_utils_data.chunking_strategy,
    dml_utils_data.non_null_text,
    dml_utils_data.non_negative_integer,
    name,
    dml_utils_data.non_null_boolean
);
