# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the extracted indexer sub-modules (RDR-032).

Covers:
- IndexContext dataclass creation
- indexer_utils: check_staleness, check_credentials, build_context_prefix
- code_indexer: _extract_context, index_code_file (via mocks)
- prose_indexer: index_prose_file (via mocks)
- Backward compatibility: nexus.indexer still exports _extract_context
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.errors import CredentialsMissingError
from nexus.index_context import IndexContext
from nexus.indexer_utils import build_context_prefix, check_credentials, check_staleness


# ── IndexContext ─────────────────────────────────────────────────────────────


def test_index_context_construction(tmp_path: Path) -> None:
    """IndexContext can be created with required fields; optional fields have defaults."""
    mock_col = MagicMock()
    mock_db = MagicMock()
    mock_client = MagicMock()

    ctx = IndexContext(
        col=mock_col,
        db=mock_db,
        voyage_key="vk-test",
        voyage_client=mock_client,
        repo_path=tmp_path,
        corpus="code__myrepo",
        embedding_model="voyage-code-3",
        git_meta={"git_project_name": "myrepo"},
        now_iso="2026-01-01T00:00:00+00:00",
    )
    assert ctx.score == 0.0
    assert ctx.chunk_lines is None
    assert ctx.force is False
    assert ctx.timeout == 120.0
    assert ctx.tuning is None


def test_index_context_score_mutability(tmp_path: Path) -> None:
    """IndexContext.score can be mutated between file iterations."""
    ctx = IndexContext(
        col=MagicMock(),
        db=MagicMock(),
        voyage_key="key",
        voyage_client=MagicMock(),
        repo_path=tmp_path,
        corpus="code__test",
        embedding_model="voyage-code-3",
        git_meta={},
        now_iso="2026-01-01T00:00:00+00:00",
        score=1.5,
    )
    assert ctx.score == 1.5
    ctx.score = 2.5
    assert ctx.score == 2.5


# ── indexer_utils: check_staleness ──────────────────────────────────────────


def test_check_staleness_current_file_returns_true(tmp_path: Path) -> None:
    """check_staleness returns True when hash and model match."""
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": "abc123", "embedding_model": "voyage-code-3"}],
        "ids": ["id1"],
    }
    result = check_staleness(mock_col, tmp_path / "foo.py", "abc123", "voyage-code-3")
    assert result is True


def test_check_staleness_stale_hash_returns_false(tmp_path: Path) -> None:
    """check_staleness returns False when content_hash differs."""
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": "old_hash", "embedding_model": "voyage-code-3"}],
        "ids": ["id1"],
    }
    result = check_staleness(mock_col, tmp_path / "foo.py", "new_hash", "voyage-code-3")
    assert result is False


def test_check_staleness_model_mismatch_returns_false(tmp_path: Path) -> None:
    """check_staleness returns False when embedding_model differs."""
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": "abc123", "embedding_model": "old-model"}],
        "ids": ["id1"],
    }
    result = check_staleness(mock_col, tmp_path / "foo.py", "abc123", "voyage-code-3")
    assert result is False


def test_check_staleness_no_existing_chunks_returns_false(tmp_path: Path) -> None:
    """check_staleness returns False when no chunks exist for the file."""
    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": [], "ids": []}
    result = check_staleness(mock_col, tmp_path / "foo.py", "abc123", "voyage-code-3")
    assert result is False


# ── indexer_utils: check_credentials ────────────────────────────────────────


def test_check_credentials_both_present_no_error() -> None:
    """check_credentials does not raise when both keys are present."""
    check_credentials("voyage-key", "chroma-key")  # no exception


def test_check_credentials_missing_voyage_raises() -> None:
    """check_credentials raises CredentialsMissingError when voyage_key is missing."""
    with pytest.raises(CredentialsMissingError, match="voyage_api_key"):
        check_credentials("", "chroma-key")


def test_check_credentials_missing_chroma_raises() -> None:
    """check_credentials raises CredentialsMissingError when chroma_key is missing."""
    with pytest.raises(CredentialsMissingError, match="chroma_api_key"):
        check_credentials("voyage-key", "")


def test_check_credentials_both_missing_raises() -> None:
    """check_credentials raises CredentialsMissingError when both keys are missing."""
    with pytest.raises(CredentialsMissingError) as exc_info:
        check_credentials("", "")
    msg = str(exc_info.value)
    assert "voyage_api_key" in msg
    assert "chroma_api_key" in msg


# ── indexer_utils: build_context_prefix ─────────────────────────────────────


def test_build_context_prefix_python() -> None:
    """build_context_prefix produces a correct Python-style prefix."""
    result = build_context_prefix(
        "src/foo.py", "#", "MyClass", "my_method", 10, 25
    )
    assert result == "# File: src/foo.py  Class: MyClass  Method: my_method  Lines: 10-25"


def test_build_context_prefix_java_no_class() -> None:
    """build_context_prefix works with empty class name."""
    result = build_context_prefix(
        "Foo.java", "//", "", "doStuff", 1, 5
    )
    assert result == "// File: Foo.java  Class:   Method: doStuff  Lines: 1-5"


def test_build_context_prefix_both_empty() -> None:
    """build_context_prefix works with both class and method empty."""
    result = build_context_prefix("script.sh", "#", "", "", 1, 20)
    assert result == "# File: script.sh  Class:   Method:   Lines: 1-20"


# ── code_indexer: _extract_context backward compatibility ───────────────────


def test_extract_context_importable_from_nexus_indexer() -> None:
    """_extract_context is re-exported from nexus.indexer for backward compat."""
    from nexus.indexer import _extract_context as ec_indexer
    from nexus.code_indexer import _extract_context as ec_code
    assert ec_indexer is ec_code


# ── index_code_file via mock ─────────────────────────────────────────────────


def test_index_code_file_skips_current_file(tmp_path: Path) -> None:
    """index_code_file returns 0 when check_staleness says file is current."""
    from nexus.code_indexer import index_code_file

    py_file = tmp_path / "hello.py"
    py_file.write_text("print('hello')\n")

    import hashlib
    content = py_file.read_text()
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()

    ctx = IndexContext(
        col=MagicMock(),
        db=MagicMock(),
        voyage_key="key",
        voyage_client=MagicMock(),
        repo_path=tmp_path,
        corpus="code__test",
        embedding_model="voyage-code-3",
        git_meta={},
        now_iso="2026-01-01T00:00:00+00:00",
        force=False,
    )

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry:
        mock_retry.return_value = {
            "metadatas": [{"content_hash": h, "embedding_model": "voyage-code-3"}],
            "ids": ["id1"],
        }
        result = index_code_file(ctx, py_file)

    assert result == 0  # skipped because current


def test_index_code_file_returns_zero_for_non_text_file(tmp_path: Path) -> None:
    """index_code_file returns 0 when file cannot be read as UTF-8."""
    from nexus.code_indexer import index_code_file

    bin_file = tmp_path / "binary.py"
    bin_file.write_bytes(b"\xff\xfe binary content")

    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": [], "ids": []}

    ctx = IndexContext(
        col=mock_col,
        db=MagicMock(),
        voyage_key="key",
        voyage_client=MagicMock(),
        repo_path=tmp_path,
        corpus="code__test",
        embedding_model="voyage-code-3",
        git_meta={},
        now_iso="2026-01-01T00:00:00+00:00",
        force=False,
    )

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry:
        mock_retry.return_value = {"metadatas": [], "ids": []}
        result = index_code_file(ctx, bin_file)

    assert result == 0


# ── index_prose_file via mock ────────────────────────────────────────────────


def test_index_prose_file_skips_current_file(tmp_path: Path) -> None:
    """index_prose_file returns 0 when file is current (hash+model match)."""
    from nexus.prose_indexer import index_prose_file

    md_file = tmp_path / "README.md"
    md_file.write_text("# Hello\n\nSome content here.\n")

    import hashlib
    content = md_file.read_text()
    h = hashlib.sha256(content.encode()).hexdigest()

    ctx = IndexContext(
        col=MagicMock(),
        db=MagicMock(),
        voyage_key="key",
        voyage_client=MagicMock(),
        repo_path=tmp_path,
        corpus="docs__test",
        embedding_model="voyage-context-3",
        git_meta={},
        now_iso="2026-01-01T00:00:00+00:00",
        force=False,
    )

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry:
        mock_retry.return_value = {
            "metadatas": [{"content_hash": h, "embedding_model": "voyage-context-3"}],
            "ids": ["id1"],
        }
        result = index_prose_file(ctx, md_file)

    assert result == 0


# ── _extract_context with real Python source (032-S13) ───────────────────────


def test_extract_context_free_function() -> None:
    """_extract_context returns ('', function_name) for a free function."""
    from nexus.code_indexer import _extract_context

    source = b"def hello():\n    return 42\n"
    class_name, method_name = _extract_context(source, "python", 0, 1)
    assert class_name == ""
    assert method_name == "hello"


def test_extract_context_method_inside_class() -> None:
    """_extract_context returns (class_name, method_name) for a method."""
    from nexus.code_indexer import _extract_context

    source = b"class Foo:\n    def bar(self):\n        return 1\n"
    class_name, method_name = _extract_context(source, "python", 1, 2)
    assert class_name == "Foo"
    assert method_name == "bar"


def test_extract_context_decorated_function() -> None:
    """_extract_context finds the function name through a decorator."""
    from nexus.code_indexer import _extract_context

    source = b"@staticmethod\ndef helper():\n    pass\n"
    class_name, method_name = _extract_context(source, "python", 0, 2)
    assert class_name == ""
    assert method_name == "helper"


def test_extract_context_chunk_spanning_siblings() -> None:
    """_extract_context returns ('', '') when chunk spans two sibling functions."""
    from nexus.code_indexer import _extract_context

    source = b"def foo():\n    pass\n\ndef bar():\n    pass\n"
    # chunk_start=0 (foo) to chunk_end=4 (bar) — spans both siblings
    class_name, method_name = _extract_context(source, "python", 0, 4)
    assert class_name == ""
    assert method_name == ""


def test_extract_context_unsupported_language() -> None:
    """_extract_context returns ('', '') for an unsupported language."""
    from nexus.code_indexer import _extract_context

    class_name, method_name = _extract_context(b"some content", "unknown_lang_xyz", 0, 0)
    assert class_name == ""
    assert method_name == ""


# ── index_code_file happy path (032-S14) ─────────────────────────────────────


def test_index_code_file_happy_path_new_file(tmp_path: Path) -> None:
    """index_code_file chunks, embeds, and upserts a new Python file."""
    from nexus.code_indexer import index_code_file

    py_file = tmp_path / "example.py"
    py_file.write_text("def greet(name):\n    return f'Hello {name}'\n")

    mock_db = MagicMock()
    mock_voyage = MagicMock()
    # Simulate Voyage embed returning 2 embeddings (one per chunk)
    mock_embed_result = MagicMock()
    mock_embed_result.embeddings = [[0.1] * 128]
    mock_voyage.embed.return_value = mock_embed_result

    ctx = IndexContext(
        col=MagicMock(),
        db=mock_db,
        voyage_key="key",
        voyage_client=mock_voyage,
        repo_path=tmp_path,
        corpus="code__test",
        embedding_model="voyage-code-3",
        git_meta={"git_project_name": "test"},
        now_iso="2026-01-01T00:00:00+00:00",
        force=False,
    )

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry:
        # No existing chunks → file is new
        mock_retry.return_value = {"metadatas": [], "ids": []}
        result = index_code_file(ctx, py_file)

    assert result >= 1
    mock_voyage.embed.assert_called()
    mock_db.upsert_chunks_with_embeddings.assert_called_once()
    call_kwargs = mock_db.upsert_chunks_with_embeddings.call_args
    assert call_kwargs[1]["collection_name"] == "code__test"
    assert len(call_kwargs[1]["ids"]) == result


# ── index_prose_file non-markdown path (032-S15) ────────────────────────────


def test_index_prose_file_non_markdown_uses_line_chunk(tmp_path: Path) -> None:
    """index_prose_file uses _line_chunk for non-.md files and embed_texts defaults to documents."""
    from nexus.prose_indexer import index_prose_file

    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("Line one of notes.\nLine two of notes.\nLine three.\n")

    mock_db = MagicMock()

    ctx = IndexContext(
        col=MagicMock(),
        db=mock_db,
        voyage_key="key",
        voyage_client=None,
        repo_path=tmp_path,
        corpus="docs__test",
        embedding_model="voyage-context-3",
        git_meta={},
        now_iso="2026-01-01T00:00:00+00:00",
        force=False,
    )

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry, \
         patch("nexus.doc_indexer._embed_with_fallback") as mock_embed:
        # File is new
        mock_retry.return_value = {"metadatas": [], "ids": []}
        mock_embed.return_value = ([[0.1] * 128], "voyage-context-3")
        result = index_prose_file(ctx, txt_file)

    assert result >= 1
    mock_embed.assert_called_once()
    # Verify embed_texts (first arg) equals documents for non-markdown
    embed_call_args = mock_embed.call_args[0]
    embed_texts = embed_call_args[0]
    upsert_kwargs = mock_db.upsert_chunks_with_embeddings.call_args[1]
    assert embed_texts == upsert_kwargs["documents"]
    # Verify metadata has line_start/line_end (not char offsets like markdown)
    meta = upsert_kwargs["metadatas"][0]
    assert "line_start" in meta
    assert meta["line_start"] >= 1
