"""Fix E / nexus-0qnh: Content-hash-based skip/re-embed logic in _index_code_file.

Verifies that:
- An unchanged file (same content_hash + embedding_model) is skipped on
  the second indexing pass (Voyage AI embed is NOT called again).
- A modified file (different content_hash) triggers re-embedding.

Also verifies force=True bypasses the staleness check for code, prose, and PDF files.
"""
import pytest
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


def _make_embed_fn(embedding_dim: int = 8):
    """nexus-sghyo (2026-08-06): the SUPPORTED embed-injection point.

    Client-side Voyage embedding (``ctx.voyage_client.embed``) is retired
    outright (Hal determination 2026-07-28: "we do no embedding on the
    client") — code_indexer.py's ``else`` branch that used to dispatch to
    it now raises unconditionally. ``embed_fn`` (checked FIRST, before any
    mode/credential branching) is the way these tests exercise the
    staleness/re-embed decision without depending on client-side Voyage.
    Returns ``(embed_fn, call_log)`` — call_log records each batch of
    texts passed, standing in for the deleted ``voyage.embed.assert_
    called()`` proof.
    """
    calls: list[list[str]] = []

    def embed_fn(texts: list[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [[0.1] * embedding_dim for _ in texts]

    return embed_fn, calls


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

    embed_fn, embed_calls = _make_embed_fn(embedding_dim=8)
    db = _make_db()

    result = _index_code_file(
        file=f,
        repo=tmp_path,
        collection_name="code__test",
        target_model=_TARGET_MODEL,
        col=mock_col,
        db=db,
        voyage_client=None,
        git_meta={},
        now_iso="2026-01-01T00:00:00Z",
        score=1.0,
        embed_fn=embed_fn,
    )

    assert result > 0, "Should return positive chunk count (indexed) when hash changed"
    assert embed_calls, "embed_fn must be called for modified file"


# ── test: new file (no prior record) triggers embed ───────────────────────────

def test_new_file_embeds(tmp_path: Path) -> None:
    """A file with no existing T3 record is always embedded (first-time index)."""
    content = "x = 42\ny = x + 1\n"
    f = tmp_path / "vars.py"
    f.write_text(content)

    # col.get() returns empty (file not yet in T3)
    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": []}

    embed_fn, embed_calls = _make_embed_fn(embedding_dim=8)
    db = _make_db()

    result = _index_code_file(
        file=f,
        repo=tmp_path,
        collection_name="code__test",
        target_model=_TARGET_MODEL,
        col=mock_col,
        db=db,
        voyage_client=None,
        git_meta={},
        now_iso="2026-01-01T00:00:00Z",
        score=1.0,
        embed_fn=embed_fn,
    )

    assert result > 0, "New file should be indexed (positive chunk count)"
    assert embed_calls


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

    embed_fn, embed_calls = _make_embed_fn(embedding_dim=8)
    db = _make_db()

    result = _index_code_file(
        file=f,
        repo=tmp_path,
        collection_name="code__test",
        target_model=_TARGET_MODEL,
        col=mock_col,
        db=db,
        voyage_client=None,
        git_meta={},
        now_iso="2026-01-01T00:00:00Z",
        score=1.0,
        force=True,
        embed_fn=embed_fn,
    )

    assert result > 0, "force=True should return int > 0 (indexed) even when hash matches"
    assert embed_calls


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

    # nexus-sghyo (2026-08-06): embed_fn is the supported injection point
    # now — the deleted doc_indexer._embed_with_fallback used to back the
    # non-service path (client-side Voyage embedding is retired, Hal
    # determination 2026-07-28).
    embed_fn, embed_calls = _make_embed_fn(embedding_dim=8)
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
        embed_fn=embed_fn,
    )

    assert result > 0, "force=True should return int > 0 (indexed) even when hash matches"
    assert embed_calls


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
    # nexus-sghyo (2026-08-06): embed_fn is the supported injection point
    # now — the deleted doc_indexer._embed_with_fallback used to back the
    # non-service path (client-side Voyage embedding is retired, Hal
    # determination 2026-07-28).
    embed_fn, embed_calls = _make_embed_fn(embedding_dim=8)

    with patch("nexus.doc_indexer._pdf_chunks", return_value=[fake_chunk]) as mock_chunks:
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
            embed_fn=embed_fn,
        )

    assert result > 0, "force=True should return int > 0 (indexed) even when hash matches"
    assert embed_calls


# nexus-sghyo (2026-08-06): the ``_legacy_vector_backend`` autouse fixture
# that force-pinned this whole module to NX_STORAGE_BACKEND_VECTORS=chroma
# (the legacy chroma/local embed pipeline opt-out) is RETIRED — that
# pipeline is deleted outright: the client no longer embeds via Voyage
# (Hal determination 2026-07-28: "we do no embedding on the client"). Every
# test above now injects ``embed_fn`` explicitly (checked FIRST, before
# any mode dispatch), so the module runs fine under the ambient
# service-mode default.
