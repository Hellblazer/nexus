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
    assert embedding_model_for_collection("code__myrepo") == "voyage-4"


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


def test_index_model_rdr_collection() -> None:
    """rdr__ collections use voyage-context-3 (CCE) at index time."""
    assert index_model_for_collection("rdr__myrepo-abcdef12") == "voyage-context-3"


def test_index_model_scratch_collection_defaults_voyage4() -> None:
    """Unrecognized prefix defaults to voyage-4 at index time."""
    assert index_model_for_collection("scratch__anything") == "voyage-4"


def test_index_model_bare_name_defaults_voyage4() -> None:
    """Name with no __ separator defaults to voyage-4 at index time."""
    assert index_model_for_collection("bare_name") == "voyage-4"


def test_embedding_model_for_collection_regression() -> None:
    """voyage-4 is the universal query model for all collection types."""
    assert embedding_model_for_collection("code__myrepo") == "voyage-4"
    assert embedding_model_for_collection("knowledge__security") == "voyage-4"
    assert embedding_model_for_collection("docs__papers") == "voyage-4"
    assert embedding_model_for_collection("rdr__myrepo-abcdef12") == "voyage-4"
    assert embedding_model_for_collection("other__collection") == "voyage-4"


# ── corpus resolution for RDR ────────────────────────────────────────────────

def test_resolve_corpus_rdr_prefix() -> None:
    """--corpus rdr matches rdr__* collections."""
    all_cols = ["code__myrepo", "docs__papers", "rdr__myrepo-abcdef12"]
    assert resolve_corpus("rdr", all_cols) == ["rdr__myrepo-abcdef12"]


def test_resolve_corpus_rdr_does_not_match_docs_rdr() -> None:
    """--corpus rdr must NOT match docs__rdr__* (the old buggy naming)."""
    all_cols = ["docs__rdr__myrepo", "rdr__myrepo-abcdef12"]
    assert resolve_corpus("rdr", all_cols) == ["rdr__myrepo-abcdef12"]


def test_resolve_corpus_docs_does_not_match_rdr() -> None:
    """--corpus docs must NOT match rdr__* collections."""
    all_cols = ["docs__papers", "rdr__myrepo-abcdef12"]
    assert resolve_corpus("docs", all_cols) == ["docs__papers"]


# ── validate_collection_name boundary & edge cases ──────────────────────────

def test_validate_collection_name_exactly_63_chars() -> None:
    """Maximum valid length is exactly 63 characters."""
    name = "a" * 63
    validate_collection_name(name)  # should not raise


def test_validate_collection_name_exactly_3_chars() -> None:
    """Minimum valid length is exactly 3 characters."""
    validate_collection_name("a1b")


def test_validate_collection_name_empty_string() -> None:
    with pytest.raises(ValueError, match="3"):
        validate_collection_name("")


def test_validate_collection_name_single_char() -> None:
    with pytest.raises(ValueError, match="3"):
        validate_collection_name("a")


def test_validate_collection_name_double_underscore_valid() -> None:
    """Double underscores in the middle are valid — used by all collection prefixes."""
    validate_collection_name("code__myrepo")
    validate_collection_name("a__b")


@pytest.mark.parametrize("char", [".", " ", "/", "@", "+", "%", "=", "!", "~"])
def test_validate_collection_name_rejects_special_chars(char: str) -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        validate_collection_name(f"bad{char}name")


def test_validate_collection_name_starts_with_underscore() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        validate_collection_name("_badstart")


def test_validate_collection_name_ends_with_underscore() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        validate_collection_name("badend_")


def test_validate_collection_name_digits_at_boundaries() -> None:
    """Names starting and ending with digits are valid."""
    validate_collection_name("1abc9")
    validate_collection_name("123")


# ── resolve_corpus edge cases ────────────────────────────────────────────────

def test_resolve_corpus_prefix_requires_double_underscore() -> None:
    """--corpus code must NOT match 'codebase__x' (only 'code__*')."""
    all_cols = ["codebase__myrepo", "code__myrepo"]
    assert resolve_corpus("code", all_cols) == ["code__myrepo"]


def test_resolve_corpus_multiple_separators_exact_match() -> None:
    """Corpus arg with __ uses exact match, even with multiple __ separators."""
    all_cols = ["code__repo__extra", "code__repo"]
    assert resolve_corpus("code__repo__extra", all_cols) == ["code__repo__extra"]


# ── t3_collection_name edge cases ────────────────────────────────────────────

def test_t3_collection_name_already_prefixed_knowledge() -> None:
    """Arg already containing __ is used as-is, even if knowledge__ prefix."""
    assert t3_collection_name("knowledge__existing") == "knowledge__existing"


def test_t3_collection_name_other_prefix_passthrough() -> None:
    """Arg with code__ prefix is preserved as-is."""
    assert t3_collection_name("code__myrepo") == "code__myrepo"
