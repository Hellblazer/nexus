DROP FUNCTION IF EXISTS dml_utils_lib.populate_single_relation_boundaries(
    bigint,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.chunking_strategy,
    name[],
    text[],
    dml_utils_data.positive_integer
);
