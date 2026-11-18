DROP FUNCTION IF EXISTS dml_utils_lib.explain_chunk_plans(
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    dml_utils_data.non_null_text,
    name[],
    text[],
    bigint,
    dml_utils_data.chunking_strategy
);
