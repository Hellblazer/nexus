"""Fix E / nexus-0qnh: Content-hash-based skip/re-embed logic in _index_code_file.

Verifies that:
- An unchanged file (same content_hash + embedding_model) is skipped on
  the second indexing pass (Voyage AI embed is NOT called again).
- A modified file (different content_hash) triggers re-embedding.

Also verifies force=True bypasses the staleness check for code, prose, and PDF files.
"""
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from nexus.indexer import _index_code_file, _index_pdf_file, _index_prose_file


# ── fixtures / helpers ─────────────────────────────────────────────────────────

_TARGET_MODEL = "voyage-code-3"


def _make_voyage_client(embedding_dim: int = 8) -> MagicMock:
    """Return a mock Voyage client whose embed() returns plausible vectors."""
    mock = MagicMock()
    mock.embed.return_value = MagicMock(
        embeddings=[[0.1] * embedding_dim]
    )
    return mock


def _make_db() -> MagicMock:
    """Return a mock T3 DB that accepts upsert_chunks_with_embeddings."""
    return MagicMock()


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# ── test: unchanged file is skipped ───────────────────────────────────────────

def test_unchanged_file_skips_embed(tmp_path: Path) -> None:
    """Second index call with identical content does not call Voyage embed.

    Scenario: col.get() returns metadata with matching content_hash AND
    embedding_model → staleness check short-circuits, embed is never called.
    """
    content = "def hello():\n    return 'world'\n"
    f = tmp_path / "hello.py"
    f.write_text(content)
    h = _content_hash(content)

    # Simulate T3 already having this file at the same hash + model
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": h, "embedding_model": _TARGET_MODEL}]
    }

    voyage = _make_voyage_client()
    db = _make_db()

    result = _index_code_file(
        file=f,
        repo=tmp_path,
        collection_name="code__test",
        target_model=_TARGET_MODEL,
        col=mock_col,
        db=db,
        voyage_client=voyage,
        git_meta={},
        now_iso="2026-01-01T00:00:00Z",
        score=1.0,
    )

    assert result == 0, "Should return 0 (skipped) when hash unchanged"
    voyage.embed.assert_not_called()


# ── test: modified file triggers re-embed ─────────────────────────────────────

def test_modified_file_reembeds(tmp_path: Path) -> None:
    """File with changed content (hash mismatch) triggers a fresh embed call.

    Scenario: col.get() returns metadata with a DIFFERENT content_hash →
    staleness check fails → file is re-chunked and re-embedded.
    """
    content = "def goodbye():\n    return 'farewell'\n"
    f = tmp_path / "bye.py"
    f.write_text(content)

    # Simulate T3 having an OLD hash (different content was stored previously)
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": "old_stale_hash_abcdef", "embedding_model": _TARGET_MODEL}]
    }

    voyage = _make_voyage_client(embedding_dim=8)
    db = _make_db()

    result = _index_code_file(
        file=f,
        repo=tmp_path,
        collection_name="code__test",
        target_model=_TARGET_MODEL,
        col=mock_col,
        db=db,
        voyage_client=voyage,
        git_meta={},
        now_iso="2026-01-01T00:00:00Z",
        score=1.0,
    )

    assert result > 0, "Should return positive chunk count (indexed) when hash changed"
    voyage.embed.assert_called(), "Voyage embed must be called for modified file"


# ── test: new file (no prior record) triggers embed ───────────────────────────

def test_new_file_embeds(tmp_path: Path) -> None:
    """A file with no existing T3 record is always embedded (first-time index)."""
    content = "x = 42\ny = x + 1\n"
    f = tmp_path / "vars.py"
    f.write_text(content)

    # col.get() returns empty (file not yet in T3)
    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": []}

    voyage = _make_voyage_client(embedding_dim=8)
    db = _make_db()

    result = _index_code_file(
        file=f,
        repo=tmp_path,
        collection_name="code__test",
        target_model=_TARGET_MODEL,
        col=mock_col,
        db=db,
        voyage_client=voyage,
        git_meta={},
        now_iso="2026-01-01T00:00:00Z",
        score=1.0,
    )

    assert result > 0, "New file should be indexed (positive chunk count)"
    voyage.embed.assert_called()


# ── test: force=True bypasses staleness for code files ────────────────────────

def test_force_bypasses_staleness_code_file(tmp_path: Path) -> None:
    """force=True causes _index_code_file to re-embed even when hash matches.

    Scenario: col.get() returns metadata with matching content_hash AND
    embedding_model (would normally skip), but force=True bypasses the guard.
    Result: returns True and Voyage embed IS called.
    """
    content = "def hello():\n    return 'world'\n"
    f = tmp_path / "hello.py"
    f.write_text(content)
    h = _content_hash(content)

    # Simulate T3 having this file with a MATCHING hash — normally would skip
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": h, "embedding_model": _TARGET_MODEL}]
    }

    voyage = _make_voyage_client(embedding_dim=8)
    db = _make_db()

    result = _index_code_file(
        file=f,
        repo=tmp_path,
        collection_name="code__test",
        target_model=_TARGET_MODEL,
        col=mock_col,
        db=db,
        voyage_client=voyage,
        git_meta={},
        now_iso="2026-01-01T00:00:00Z",
        score=1.0,
        force=True,
    )

    assert result > 0, "force=True should return int > 0 (indexed) even when hash matches"
    voyage.embed.assert_called()


# ── test: force=True bypasses staleness for prose files ───────────────────────

def test_force_bypasses_staleness_prose_file(tmp_path: Path) -> None:
    """force=True causes _index_prose_file to re-embed even when hash matches.

    Scenario: col.get() returns metadata with matching content_hash AND
    embedding_model (would normally skip), but force=True bypasses the guard.
    Result: returns True and _embed_with_fallback IS called.
    """
    content = "# Hello\n\nThis is prose content for testing.\n"
    f = tmp_path / "doc.md"
    f.write_text(content)
    h = _content_hash(content)

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": h, "embedding_model": "voyage-context-3"}]
    }
    db = _make_db()

    fake_embeddings = [[0.1] * 8]
    with patch("nexus.doc_indexer._embed_with_fallback", return_value=(fake_embeddings, "voyage-context-3")) as mock_embed:
        result = _index_prose_file(
            file=f,
            repo=tmp_path,
            collection_name="docs__test",
            target_model="voyage-context-3",
            col=mock_col,
            db=db,
            voyage_key="fake-key",
            git_meta={},
            now_iso="2026-01-01T00:00:00Z",
            score=1.0,
            force=True,
        )

    assert result > 0, "force=True should return int > 0 (indexed) even when hash matches"
    mock_embed.assert_called()


# ── test: force=True bypasses staleness for PDF files ─────────────────────────

def test_force_bypasses_staleness_pdf_file(tmp_path: Path) -> None:
    """force=True causes _index_pdf_file to re-embed even when hash matches.

    Scenario: col.get() returns metadata with matching content_hash AND
    embedding_model (would normally skip), but force=True bypasses the guard.
    Result: returns True and _embed_with_fallback IS called.
    """
    # Create a dummy PDF file (just needs to exist; _pdf_chunks is mocked)
    f = tmp_path / "paper.pdf"
    f.write_bytes(b"%PDF-1.4 fake pdf content")

    # We need a plausible hash for the mock metadata
    import hashlib as _hl
    content_hash_hex = _hl.sha256(f.read_bytes()).hexdigest()

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": content_hash_hex, "embedding_model": "voyage-context-3"}]
    }
    db = _make_db()

    # Mock _pdf_chunks to return one fake chunk tuple: (id, doc, metadata)
    fake_chunk = (
        "abc123",
        "Some PDF text content",
        {
            "source_title": "Test Paper",
            "page_number": 1,
            "content_hash": content_hash_hex,
            "embedding_model": "voyage-context-3",
        },
    )
    fake_embeddings = [[0.1] * 8]

    with patch("nexus.doc_indexer._pdf_chunks", return_value=[fake_chunk]) as mock_chunks, \
         patch("nexus.doc_indexer._embed_with_fallback", return_value=(fake_embeddings, "voyage-context-3")) as mock_embed:
        result = _index_pdf_file(
            file=f,
            repo=tmp_path,
            collection_name="docs__test",
            target_model="voyage-context-3",
            col=mock_col,
            db=db,
            voyage_key="fake-key",
            git_meta={},
            now_iso="2026-01-01T00:00:00Z",
            score=1.0,
            force=True,
        )

    assert result > 0, "force=True should return int > 0 (indexed) even when hash matches"
    mock_embed.assert_called()
