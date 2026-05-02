# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the extracted indexer sub-modules (RDR-032)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.errors import CredentialsMissingError
from nexus.index_context import IndexContext
from nexus.indexer_utils import build_context_prefix, check_credentials, check_staleness


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def make_ctx(tmp_path: Path):
    """Factory for IndexContext with sensible defaults."""
    def _make(**overrides):
        defaults = dict(
            col=MagicMock(), db=MagicMock(), voyage_key="key",
            voyage_client=MagicMock(), repo_path=tmp_path,
            corpus="code__test", embedding_model="voyage-code-3",
            git_meta={}, now_iso="2026-01-01T00:00:00+00:00", force=False,
        )
        defaults.update(overrides)
        return IndexContext(**defaults)
    return _make


# ── IndexContext ─────────────────────────────────────────────────────────────

def test_index_context_defaults(make_ctx):
    ctx = make_ctx()
    assert ctx.score == 0.0
    assert ctx.chunk_lines is None
    assert ctx.force is False
    assert ctx.timeout == 120.0
    assert ctx.tuning is None


def test_index_context_score_mutability(make_ctx):
    ctx = make_ctx(score=1.5)
    assert ctx.score == 1.5
    ctx.score = 2.5
    assert ctx.score == 2.5


# ── check_staleness ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("stored_hash,stored_model,query_hash,query_model,expected", [
    ("abc123", "voyage-code-3", "abc123", "voyage-code-3", True),   # current
    ("old_hash", "voyage-code-3", "new_hash", "voyage-code-3", False),  # stale hash
    ("abc123", "old-model", "abc123", "voyage-code-3", False),       # model mismatch
])
def test_check_staleness(tmp_path, stored_hash, stored_model, query_hash, query_model, expected):
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": stored_hash, "embedding_model": stored_model}],
        "ids": ["id1"],
    }
    assert check_staleness(mock_col, tmp_path / "foo.py", query_hash, query_model) is expected


def test_check_staleness_no_existing_chunks(tmp_path):
    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": [], "ids": []}
    assert check_staleness(mock_col, tmp_path / "foo.py", "abc123", "voyage-code-3") is False


def test_check_staleness_uses_doc_id_when_provided(tmp_path):
    """nexus-dcym: when caller passes ``doc_id``, the where-filter must
    key on doc_id, not the legacy source_path. WITH TEETH: a stash-revert
    of indexer_utils.py to the source_path-keyed form makes the assertion
    on ``where`` fail.
    """
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": "abc", "embedding_model": "voyage-code-3"}],
        "ids": ["id1"],
    }
    result = check_staleness(
        mock_col, tmp_path / "shared.py", "abc", "voyage-code-3",
        doc_id="ART-deadbeef",
    )
    assert result is True
    where = mock_col.get.call_args.kwargs["where"]
    assert where == {"doc_id": "ART-deadbeef"}


def test_check_staleness_falls_back_to_source_path_when_no_doc_id(tmp_path):
    """No doc_id passed → legacy source_path lookup (back-compat)."""
    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": [], "ids": []}
    file_path = tmp_path / "foo.py"
    check_staleness(mock_col, file_path, "abc", "voyage-code-3")
    where = mock_col.get.call_args.kwargs["where"]
    assert where == {"source_path": str(file_path)}


# ── check_credentials ────────────────────────────────────────────────────────

def test_check_credentials_both_present():
    check_credentials("voyage-key", "chroma-key")  # no exception


@pytest.mark.parametrize("voyage,chroma,match", [
    ("", "chroma-key", "voyage_api_key"),
    ("voyage-key", "", "chroma_api_key"),
])
def test_check_credentials_missing(voyage, chroma, match):
    with pytest.raises(CredentialsMissingError, match=match):
        check_credentials(voyage, chroma)


def test_check_credentials_both_missing():
    with pytest.raises(CredentialsMissingError) as exc_info:
        check_credentials("", "")
    msg = str(exc_info.value)
    assert "voyage_api_key" in msg and "chroma_api_key" in msg


# ── build_context_prefix ─────────────────────────────────────────────────────

@pytest.mark.parametrize("path,comment,cls,method,ls,le,expected", [
    ("src/foo.py", "#", "MyClass", "my_method", 10, 25,
     "# File: src/foo.py  Class: MyClass  Method: my_method  Lines: 10-25"),
    ("Foo.java", "//", "", "doStuff", 1, 5,
     "// File: Foo.java  Class:   Method: doStuff  Lines: 1-5"),
    ("script.sh", "#", "", "", 1, 20,
     "# File: script.sh  Class:   Method:   Lines: 1-20"),
])
def test_build_context_prefix(path, comment, cls, method, ls, le, expected):
    assert build_context_prefix(path, comment, cls, method, ls, le) == expected


# ── _extract_context backward compatibility ───────────────────────────────────

def test_extract_context_importable_from_nexus_indexer():
    from nexus.indexer import _extract_context as ec_indexer
    from nexus.code_indexer import _extract_context as ec_code
    assert ec_indexer is ec_code


# ── _extract_context with real Python source ─────────────────────────────────

@pytest.mark.parametrize("source,lang,start,end,exp_class,exp_method", [
    (b"def hello():\n    return 42\n", "python", 0, 1, "", "hello"),
    (b"class Foo:\n    def bar(self):\n        return 1\n", "python", 1, 2, "Foo", "bar"),
    (b"@staticmethod\ndef helper():\n    pass\n", "python", 0, 2, "", "helper"),
    (b"def foo():\n    pass\n\ndef bar():\n    pass\n", "python", 0, 4, "", ""),
    (b"some content", "unknown_lang_xyz", 0, 0, "", ""),
])
def test_extract_context(source, lang, start, end, exp_class, exp_method):
    from nexus.code_indexer import _extract_context
    cls, method = _extract_context(source, lang, start, end)
    assert cls == exp_class
    assert method == exp_method


# ── index_code_file ──────────────────────────────────────────────────────────

def test_index_code_file_skips_current_file(tmp_path, make_ctx):
    from nexus.code_indexer import index_code_file
    import hashlib

    py_file = tmp_path / "hello.py"
    py_file.write_text("print('hello')\n")
    h = hashlib.sha256(py_file.read_text().encode("utf-8")).hexdigest()
    ctx = make_ctx()

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry:
        mock_retry.return_value = {
            "metadatas": [{"content_hash": h, "embedding_model": "voyage-code-3"}],
            "ids": ["id1"],
        }
        assert index_code_file(ctx, py_file) == 0


def test_index_code_file_returns_zero_for_non_text_file(tmp_path, make_ctx):
    from nexus.code_indexer import index_code_file

    bin_file = tmp_path / "binary.py"
    bin_file.write_bytes(b"\xff\xfe binary content")
    ctx = make_ctx(col=MagicMock())
    ctx.col.get.return_value = {"metadatas": [], "ids": []}

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry:
        mock_retry.return_value = {"metadatas": [], "ids": []}
        assert index_code_file(ctx, bin_file) == 0


def test_index_code_file_happy_path_new_file(tmp_path, make_ctx):
    from nexus.code_indexer import index_code_file

    py_file = tmp_path / "example.py"
    py_file.write_text("def greet(name):\n    return f'Hello {name}'\n")

    mock_voyage = MagicMock()
    mock_embed_result = MagicMock()
    mock_embed_result.embeddings = [[0.1] * 128]
    mock_voyage.embed.return_value = mock_embed_result
    mock_db = MagicMock()

    ctx = make_ctx(db=mock_db, voyage_client=mock_voyage,
                   git_meta={"git_project_name": "test"})

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry:
        mock_retry.return_value = {"metadatas": [], "ids": []}
        result = index_code_file(ctx, py_file)

    assert result >= 1
    mock_voyage.embed.assert_called()
    call_kwargs = mock_db.upsert_chunks_with_embeddings.call_args[1]
    assert call_kwargs["collection_name"] == "code__test"
    assert len(call_kwargs["ids"]) == result


# ── index_prose_file ─────────────────────────────────────────────────────────

def test_index_prose_file_skips_current_file(tmp_path, make_ctx):
    from nexus.prose_indexer import index_prose_file
    import hashlib

    md_file = tmp_path / "README.md"
    md_file.write_text("# Hello\n\nSome content here.\n")
    h = hashlib.sha256(md_file.read_text().encode()).hexdigest()
    ctx = make_ctx(corpus="docs__test", embedding_model="voyage-context-3")

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry:
        mock_retry.return_value = {
            "metadatas": [{"content_hash": h, "embedding_model": "voyage-context-3"}],
            "ids": ["id1"],
        }
        assert index_prose_file(ctx, md_file) == 0


def test_index_prose_file_non_markdown_uses_line_chunk(tmp_path, make_ctx):
    from nexus.prose_indexer import index_prose_file

    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("Line one of notes.\nLine two of notes.\nLine three.\n")
    mock_db = MagicMock()
    ctx = make_ctx(db=mock_db, voyage_client=None,
                   corpus="docs__test", embedding_model="voyage-context-3")

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry, \
         patch("nexus.doc_indexer._embed_with_fallback") as mock_embed:
        mock_retry.return_value = {"metadatas": [], "ids": []}
        mock_embed.return_value = ([[0.1] * 128], "voyage-context-3")
        result = index_prose_file(ctx, txt_file)

    assert result >= 1
    mock_embed.assert_called_once()
    embed_texts = mock_embed.call_args[0][0]
    upsert_kwargs = mock_db.upsert_chunks_with_embeddings.call_args[1]
    assert embed_texts == upsert_kwargs["documents"]
    meta = upsert_kwargs["metadatas"][0]
    assert "line_start" in meta and meta["line_start"] >= 1
