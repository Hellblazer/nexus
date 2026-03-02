"""Fix E / nexus-0qnh: Content-hash-based skip/re-embed logic in _index_code_file.

Verifies that:
- An unchanged file (same content_hash + embedding_model) is skipped on
  the second indexing pass (Voyage AI embed is NOT called again).
- A modified file (different content_hash) triggers re-embedding.
"""
import hashlib
from pathlib import Path
from unittest.mock import MagicMock

from nexus.indexer import _index_code_file


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

    assert result is False, "Should return False (skipped) when hash unchanged"
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

    assert result is True, "Should return True (indexed) when hash changed"
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

    assert result is True, "New file should be indexed"
    voyage.embed.assert_called()
