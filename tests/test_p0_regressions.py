# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for P0 bug fixes (nexus-wv7).

Each test is named after the bug it guards against.  All tests must pass
with `uv run pytest tests/test_p0_regressions.py -x -q`.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.db.t1 import T1Database
from nexus.db.t3 import T3Database
from nexus.md_chunker import SemanticMarkdownChunker
from nexus.registry import RepoRegistry


# ── Test 1: T1 session isolation ──────────────────────────────────────────────

def test_t1_session_isolation_list_entries_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """list_entries() returns only entries belonging to the calling session.

    Regression guard: the session_id metadata filter must be applied so that
    one session's list_entries() does not surface another session's documents,
    even when both share the same underlying PersistentClient directory.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    db_a = T1Database(session_id="session-A")
    db_b = T1Database(session_id="session-B")
    # Clean state in case of leftover data.
    db_a.clear()
    db_b.clear()

    try:
        db_a.put("secret content from session A")

        entries_b = db_b.list_entries()
        assert entries_b == [], (
            "Session B must not see Session A's entries via list_entries(). "
            f"Got: {entries_b}"
        )
        entries_a = db_a.list_entries()
        assert len(entries_a) == 1
        assert entries_a[0]["session_id"] == "session-A"
    finally:
        db_a.clear()
        db_b.clear()


# ── Test 2: T3 collection naming uniqueness ────────────────────────────────────

def test_registry_collection_name_unique_for_same_basename(tmp_path: Path) -> None:
    """Two repos sharing the same leaf name must receive different collection names.

    Regression guard: a naive ``code__{name}`` scheme (name = repo.name) assigns
    the same collection to ``/tmp/a/repo`` and ``/tmp/b/repo``, causing one repo's
    index to silently overwrite the other.  The collection name must include a
    path-derived discriminator (e.g. a short hash of the full path).
    """
    reg_path = tmp_path / "repos.json"
    reg = RepoRegistry(reg_path)

    repo_a = tmp_path / "a" / "repo"
    repo_b = tmp_path / "b" / "repo"
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)

    reg.add(repo_a)
    reg.add(repo_b)

    info_a = reg.get(repo_a)
    info_b = reg.get(repo_b)

    assert info_a is not None, "repo_a not registered"
    assert info_b is not None, "repo_b not registered"

    col_a = info_a["collection"]
    col_b = info_b["collection"]

    assert col_a != col_b, (
        f"Two repos with the same basename 'repo' but different paths were "
        f"assigned the same collection name {col_a!r}.  Each repo must have a "
        "unique collection name (e.g. include a hash of the full path)."
    )


# ── Test 3: T3 put() with ttl_days > 0 produces non-empty expires_at ──────────

def test_t3_put_with_ttl_sets_non_empty_expires_at() -> None:
    """put() with ttl_days=30 must store a non-empty ISO 8601 expires_at value.

    Regression guard: a bug caused expires_at to remain "" even when ttl_days>0,
    making TTL-based expiry silently impossible — entries would never be pruned.
    """
    client = chromadb.EphemeralClient()
    ef = DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)

    doc_id = db.put(
        collection="knowledge__ttltest",
        content="This entry should expire in 30 days.",
        title="ttl-test-entry",
        ttl_days=30,
    )

    col = client.get_collection("knowledge__ttltest", embedding_function=ef)
    result = col.get(ids=[doc_id], include=["metadatas"])
    assert result["metadatas"], "Document not found after put()"
    meta = result["metadatas"][0]

    expires_at = meta.get("expires_at", "")
    assert expires_at != "", (
        f"expires_at must be a non-empty ISO 8601 string when ttl_days=30, "
        f"but got {expires_at!r}"
    )
    # Basic sanity: looks like an ISO timestamp
    assert "T" in expires_at or expires_at.count("-") >= 2, (
        f"expires_at does not look like an ISO 8601 timestamp: {expires_at!r}"
    )


# ── Test 4: RepoRegistry recovers from corrupt JSON ───────────────────────────

def test_registry_recovers_from_corrupt_json(tmp_path: Path) -> None:
    """RepoRegistry must not raise when the on-disk JSON is malformed.

    Regression guard: if the registry JSON is truncated or corrupted (e.g. by a
    crash during write), constructing RepoRegistry must succeed and start with an
    empty registry rather than propagating json.JSONDecodeError to the caller.
    """
    reg_path = tmp_path / "repos.json"
    reg_path.write_text('{"bad json"')  # deliberately invalid JSON

    # Must not raise — registry should start empty.
    try:
        reg = RepoRegistry(reg_path)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"RepoRegistry raised {type(exc).__name__} on corrupt JSON: {exc}"
        )

    assert reg.all() == [], (
        "Registry constructed from corrupt JSON must start empty, "
        f"but got: {reg.all()}"
    )


# ── Test 5: SemanticMarkdownChunker sets chunk_start_char and chunk_end_char ──

def test_semantic_chunker_sets_char_offsets() -> None:
    """Every chunk produced by SemanticMarkdownChunker must carry char offset metadata.

    Regression guard: the semantic path (_make_chunk) omitted chunk_start_char
    and chunk_end_char from the metadata dict, while the naive fallback set them
    correctly.  Downstream indexers (doc_indexer) rely on these fields being
    present and non-None.
    """
    chunker = SemanticMarkdownChunker(chunk_size=512)
    text = "# Introduction\n\nThis is the introduction section.\n\n## Details\n\nHere are some details."
    chunks = chunker.chunk(text, {"source_path": "/test/doc.md"})

    assert chunks, "SemanticMarkdownChunker produced no chunks for non-empty input"

    for i, chunk in enumerate(chunks):
        start = chunk.metadata.get("chunk_start_char")
        end = chunk.metadata.get("chunk_end_char")
        assert start is not None, (
            f"chunk[{i}] missing 'chunk_start_char' in metadata: {chunk.metadata}"
        )
        assert end is not None, (
            f"chunk[{i}] missing 'chunk_end_char' in metadata: {chunk.metadata}"
        )


# ── Test 7: doc_indexer atomicity — originals survive a failed add() ──────────

def test_index_markdown_atomicity_originals_survive_add_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-existing documents must not be lost if col.upsert() raises during reindex.

    Regression guard: index_markdown() calls col.upsert() *before* pruning stale
    chunks.  If col.upsert() raises an exception, no delete has occurred and the
    original documents must still be intact — no data-loss window.
    """
    monkeypatch.setenv("VOYAGE_API_KEY", "vk_test")
    monkeypatch.setenv("CHROMA_API_KEY", "ck_test")
    monkeypatch.setenv("CHROMA_TENANT", "tenant")
    monkeypatch.setenv("CHROMA_DATABASE", "db")

    # Build a mock collection pre-populated with 3 documents.
    original_ids = ["orig-1", "orig-2", "orig-3"]
    original_docs = ["doc one content", "doc two content", "doc three content"]
    original_metas = [
        {"source_path": "/other/doc.md", "content_hash": "aaa"},
        {"source_path": "/other/doc.md", "content_hash": "aaa"},
        {"source_path": "/other/doc.md", "content_hash": "aaa"},
    ]

    stored_ids = list(original_ids)
    stored_docs = list(original_docs)
    stored_metas = list(original_metas)

    def mock_get(where=None, include=None, limit=None):
        """Simulate col.get() — returns entries matching source_path filter."""
        include = include or []
        source = (where or {}).get("source_path")
        if source is None:
            docs = list(stored_docs) if "documents" in include else []
            return {"ids": list(stored_ids), "metadatas": list(stored_metas), "documents": docs}
        matched = [
            (i, d, m)
            for i, d, m in zip(stored_ids, stored_docs, stored_metas)
            if m.get("source_path") == source
        ]
        if not matched:
            return {"ids": [], "metadatas": [], "documents": []}
        ids_, docs_, metas_ = zip(*matched)
        return {
            "ids": list(ids_),
            "metadatas": list(metas_),
            "documents": list(docs_) if "documents" in include else [],
        }

    def mock_delete(ids):
        for doc_id in ids:
            idx = stored_ids.index(doc_id)
            stored_ids.pop(idx)
            stored_docs.pop(idx)
            stored_metas.pop(idx)

    def mock_count():
        return len(stored_ids)

    mock_col = MagicMock()
    mock_col.get.side_effect = mock_get
    mock_col.delete.side_effect = mock_delete
    mock_col.upsert.side_effect = RuntimeError("Simulated embedding API failure during upsert()")
    mock_col.count.side_effect = mock_count

    # Create a markdown file whose source_path does NOT match "/other/doc.md"
    # so that it is treated as a new file (no stale entries to delete initially).
    # Then patch stale-entry lookup to pretend there ARE stale entries from
    # the target file, forcing a delete+add cycle.
    md_path = tmp_path / "new_doc.md"
    md_path.write_text("# New Document\n\nSome new content here.\n")

    # Make the stale-entry check return the 3 originals so they get deleted,
    # then the add() fails — originals must be restored.
    # We simulate this by making source_path of originals match md_path.
    for m in stored_metas:
        m["source_path"] = str(md_path)
        m["content_hash"] = "old-hash-that-differs"

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        from nexus.doc_indexer import index_markdown
        with pytest.raises(Exception):  # noqa: B017
            index_markdown(md_path, corpus="testcorpus")

    # After the failed upsert(), no delete occurred — 3 originals must be intact.
    remaining = len(stored_ids)
    assert remaining == 3, (
        f"After a failed col.upsert(), the collection must still contain the 3 "
        f"original documents, but only {remaining} remain: {stored_ids}.  "
        "This indicates a data-loss atomicity bug in index_markdown()."
    )
