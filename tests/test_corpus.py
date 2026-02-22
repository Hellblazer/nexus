"""AC2/AC6: Embedding model selection and --corpus prefix resolution."""
import pytest

from nexus.corpus import (
    embedding_model_for_collection,
    index_model_for_collection,
    resolve_corpus,
    t3_collection_name,
    validate_collection_name,
)


# ── Embedding model selection ─────────────────────────────────────────────────

def test_embedding_model_code_collection() -> None:
    assert embedding_model_for_collection("code__myrepo") == "voyage-code-3"


def test_embedding_model_docs_collection() -> None:
    assert embedding_model_for_collection("docs__papers") == "voyage-4"


def test_embedding_model_knowledge_collection() -> None:
    assert embedding_model_for_collection("knowledge__security") == "voyage-4"


def test_embedding_model_unknown_prefix_defaults_voyage4() -> None:
    assert embedding_model_for_collection("other__collection") == "voyage-4"


# ── Collection name resolution from --collection arg ─────────────────────────

def test_t3_collection_name_no_separator() -> None:
    """--collection knowledge → knowledge__knowledge."""
    assert t3_collection_name("knowledge") == "knowledge__knowledge"


def test_t3_collection_name_with_separator() -> None:
    """--collection knowledge__security → used as-is."""
    assert t3_collection_name("knowledge__security") == "knowledge__security"


def test_t3_collection_name_code_no_separator() -> None:
    assert t3_collection_name("code") == "knowledge__code"


# ── Corpus prefix resolution ──────────────────────────────────────────────────

def test_resolve_corpus_code_prefix() -> None:
    all_cols = ["code__myrepo", "code__otherrepo", "docs__papers", "knowledge__security"]
    assert resolve_corpus("code", all_cols) == ["code__myrepo", "code__otherrepo"]


def test_resolve_corpus_knowledge_prefix() -> None:
    all_cols = ["code__myrepo", "knowledge__sec", "knowledge__arch"]
    assert resolve_corpus("knowledge", all_cols) == ["knowledge__sec", "knowledge__arch"]


def test_resolve_corpus_exact_match() -> None:
    all_cols = ["code__myrepo", "code__otherrepo", "docs__papers"]
    assert resolve_corpus("code__myrepo", all_cols) == ["code__myrepo"]


def test_resolve_corpus_no_match_returns_empty() -> None:
    assert resolve_corpus("code", ["docs__papers", "knowledge__x"]) == []


def test_resolve_corpus_docs_prefix() -> None:
    all_cols = ["docs__papers", "docs__books", "code__myrepo"]
    assert resolve_corpus("docs", all_cols) == ["docs__papers", "docs__books"]


# ── validate_collection_name ──────────────────────────────────────────────────

def test_validate_collection_name_valid() -> None:
    validate_collection_name("code__myrepo")
    validate_collection_name("knowledge__security")
    validate_collection_name("abc")


def test_validate_collection_name_too_short() -> None:
    with pytest.raises(ValueError, match="3"):
        validate_collection_name("ab")


def test_validate_collection_name_too_long() -> None:
    with pytest.raises(ValueError, match="63"):
        validate_collection_name("a" * 64)


def test_validate_collection_name_invalid_chars() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        validate_collection_name("bad:name")


def test_validate_collection_name_starts_with_hyphen() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        validate_collection_name("-badstart")


def test_validate_collection_name_ends_with_hyphen() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        validate_collection_name("badend-")


# ── index_model_for_collection ─────────────────────────────────────────────────

def test_index_model_code_collection() -> None:
    """code__ collections use voyage-code-3 at index time."""
    assert index_model_for_collection("code__myrepo") == "voyage-code-3"


def test_index_model_docs_collection() -> None:
    """docs__ collections use voyage-context-3 (CCE) at index time."""
    assert index_model_for_collection("docs__manual") == "voyage-context-3"


def test_index_model_knowledge_collection() -> None:
    """knowledge__ collections use voyage-context-3 (CCE) at index time."""
    assert index_model_for_collection("knowledge__wiki") == "voyage-context-3"


def test_index_model_scratch_collection_defaults_voyage4() -> None:
    """Unrecognized prefix defaults to voyage-4 at index time."""
    assert index_model_for_collection("scratch__anything") == "voyage-4"


def test_index_model_bare_name_defaults_voyage4() -> None:
    """Name with no __ separator defaults to voyage-4 at index time."""
    assert index_model_for_collection("bare_name") == "voyage-4"


def test_embedding_model_for_collection_regression() -> None:
    """embedding_model_for_collection still returns correct values (regression)."""
    assert embedding_model_for_collection("code__myrepo") == "voyage-code-3"
    assert embedding_model_for_collection("knowledge__security") == "voyage-4"
    assert embedding_model_for_collection("docs__papers") == "voyage-4"
    assert embedding_model_for_collection("other__collection") == "voyage-4"
