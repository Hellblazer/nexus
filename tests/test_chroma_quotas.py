# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import pytest

from nexus.db.chroma_quotas import (
    QUOTAS,
    SAFE_CHUNK_BYTES,
    NameTooLong,
    QueryStringTooLong,
    QuotaValidator,
    QuotaViolation,
    RecordTooLarge,
    ResultsExceedLimit,
    TooManyPredicates,
)


# ── QUOTAS constants ────────────────────────────────────────────────────────

@pytest.mark.parametrize("field,expected", [
    ("MAX_RECORDS_PER_WRITE", 300),
    ("MAX_DOCUMENT_BYTES", 16_384),
    ("MAX_EMBEDDING_DIMENSIONS", 4_096),
    ("MAX_ID_BYTES", 128),
    ("MAX_METADATA_KEY_BYTES", 36),
    ("MAX_RECORD_METADATA_VALUE_BYTES", 4_096),
    ("MAX_QUERY_RESULTS", 300),
    ("MAX_WHERE_PREDICATES", 8),
    ("MAX_CONCURRENT_READS", 10),
    ("MAX_CONCURRENT_WRITES", 10),
    ("MAX_COLLECTION_NAME_BYTES", 128),
    ("SAFE_CHUNK_BYTES", 12_288),
])
def test_quotas_constant(field: str, expected: int) -> None:
    assert getattr(QUOTAS, field) == expected


def test_quotas_is_frozen() -> None:
    with pytest.raises((AttributeError, TypeError)):
        QUOTAS.MAX_RECORDS_PER_WRITE = 999  # type: ignore[misc]


# ── Error hierarchy ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls", [
    RecordTooLarge, NameTooLong, TooManyPredicates,
    ResultsExceedLimit, QueryStringTooLong,
])
def test_subclass_of_quota_violation(cls: type) -> None:
    assert issubclass(cls, QuotaViolation)


def test_quota_violation_is_value_error() -> None:
    assert issubclass(QuotaViolation, ValueError)


def test_quota_violation_carries_field_and_limit() -> None:
    exc = RecordTooLarge(field="document", actual=20_000, limit=16_384)
    assert (exc.field, exc.actual, exc.limit) == ("document", 20_000, 16_384)


# ── QuotaValidator.validate_record ──────────────────────────────────────────

@pytest.fixture
def validator() -> QuotaValidator:
    return QuotaValidator()


class TestValidateRecord:
    @pytest.mark.parametrize("kwargs", [
        dict(id="ok", document="x" * QUOTAS.MAX_DOCUMENT_BYTES, embedding=None, metadata={}),
        dict(id="a" * QUOTAS.MAX_ID_BYTES, document="ok", embedding=None, metadata={}),
        dict(id="ok", document="ok", embedding=None,
             metadata={f"key{i}": "val" for i in range(QUOTAS.MAX_RECORD_METADATA_KEYS)}),
        dict(id="ok", document="ok", embedding=None, metadata={}),
        dict(id="ok", document="ok", embedding=None, metadata={}, uri=None),
    ])
    def test_accepts_valid(self, validator: QuotaValidator, kwargs: dict) -> None:
        validator.validate_record(**kwargs)

    @pytest.mark.parametrize("kwargs,exc_type,field_contains", [
        (dict(id="ok", document="x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1),
              embedding=None, metadata={}), RecordTooLarge, "document"),
        (dict(id="a" * (QUOTAS.MAX_ID_BYTES + 1), document="ok",
              embedding=None, metadata={}), NameTooLong, "id"),
        (dict(id="ok", document="ok", embedding=None,
              metadata={f"key{i}": "val" for i in range(QUOTAS.MAX_RECORD_METADATA_KEYS + 1)}),
         RecordTooLarge, "metadata_keys"),
        (dict(id="ok", document="ok", embedding=None,
              metadata={"mykey": "x" * (QUOTAS.MAX_RECORD_METADATA_VALUE_BYTES + 1)}),
         RecordTooLarge, "metadata_value"),
        (dict(id="ok", document="ok", embedding=None,
              metadata={"k" * (QUOTAS.MAX_METADATA_KEY_BYTES + 1): "val"}),
         NameTooLong, "metadata_key"),
        (dict(id="ok", document="ok",
              embedding=[0.1] * (QUOTAS.MAX_EMBEDDING_DIMENSIONS + 1), metadata={}),
         RecordTooLarge, "embedding"),
        (dict(id="ok", document="ok", embedding=None, metadata={},
              uri="u" * (QUOTAS.MAX_URI_BYTES + 1)), NameTooLong, "uri"),
    ])
    def test_rejects_invalid(self, validator: QuotaValidator, kwargs: dict,
                             exc_type: type, field_contains: str) -> None:
        with pytest.raises(exc_type) as exc_info:
            validator.validate_record(**kwargs)
        assert field_contains in exc_info.value.field


# ── QuotaValidator.validate_query ───────────────────────────────────────────

class TestValidateQuery:
    @pytest.mark.parametrize("kwargs", [
        dict(query_text="hello", where=None, n_results=QUOTAS.MAX_QUERY_RESULTS),
        dict(query_text="hello",
             where={f"key{i}": f"val{i}" for i in range(QUOTAS.MAX_WHERE_PREDICATES)},
             n_results=10),
        dict(query_text="hello", where=None, n_results=10),
        dict(query_text="x" * QUOTAS.MAX_QUERY_STRING_CHARS, where=None, n_results=10),
    ])
    def test_accepts_valid(self, validator: QuotaValidator, kwargs: dict) -> None:
        validator.validate_query(**kwargs)

    @pytest.mark.parametrize("kwargs,exc_type,field_contains", [
        (dict(query_text="hello", where=None,
              n_results=QUOTAS.MAX_QUERY_RESULTS + 1), ResultsExceedLimit, "n_results"),
        (dict(query_text="hello",
              where={f"key{i}": f"val{i}" for i in range(QUOTAS.MAX_WHERE_PREDICATES + 1)},
              n_results=10), TooManyPredicates, "where_predicates"),
        (dict(query_text="x" * (QUOTAS.MAX_QUERY_STRING_CHARS + 1),
              where=None, n_results=10), QueryStringTooLong, "query_text"),
    ])
    def test_rejects_invalid(self, validator: QuotaValidator, kwargs: dict,
                             exc_type: type, field_contains: str) -> None:
        with pytest.raises(exc_type) as exc_info:
            validator.validate_query(**kwargs)
        assert field_contains in exc_info.value.field


# ── validate_collection_name / validate_db_name ─────────────────────────────

def test_validate_collection_name_accepts_128(validator: QuotaValidator) -> None:
    validator.validate_collection_name("a" * 128)


@pytest.mark.parametrize("method,name,field", [
    ("validate_collection_name", "a" * 129, "collection_name"),
    ("validate_db_name", "b" * 129, "db_name"),
])
def test_validate_name_rejects_oversized(validator: QuotaValidator,
                                         method: str, name: str, field: str) -> None:
    with pytest.raises(NameTooLong) as exc_info:
        getattr(validator, method)(name)
    assert field in exc_info.value.field


# ── corpus.py 128-byte Cloud limit ──────────────────────────────────────────

def test_corpus_rejects_129_byte_name() -> None:
    from nexus.corpus import validate_collection_name
    with pytest.raises(ValueError):
        validate_collection_name("a" * 129)


# ── SAFE_CHUNK_BYTES ────────────────────────────────────────────────────────

def test_safe_chunk_bytes_module_alias() -> None:
    assert SAFE_CHUNK_BYTES == QUOTAS.SAFE_CHUNK_BYTES == 12_288


def test_safe_chunk_bytes_less_than_max_document_bytes() -> None:
    assert QUOTAS.SAFE_CHUNK_BYTES < QUOTAS.MAX_DOCUMENT_BYTES
