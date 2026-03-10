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

    mock_col = MagicMock()
    # Simulate a current file (staleness check returns True = current)
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": "any", "embedding_model": "voyage-code-3"}],
        "ids": ["id1"],
    }

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
        # Simulate: stored hash matches content hash → current
        content = py_file.read_text()
        import hashlib
        h = hashlib.sha256(content.encode()).hexdigest()
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
