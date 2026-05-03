# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for ``nx collection rewrite-metadata`` (nexus-2my fix #1).

Operationalises the nexus-40t metadata schema rationalisation on
already-indexed corpora. Without this, chunks ingested before the
4.3.1 release keep their pre-canonical metadata until they are
deleted and re-ingested — and `--force` is a silent no-op when the
pipeline-state DB still has the content_hash on file.

Covers:
  * Legacy chunks with cargo keys (``store_type``, ``indexed_at``,
    flat ``git_*``) get normalised in place via ``t3.update_chunks``
    so the metadata-schema validator runs and the canonical key set
    lands.
  * Chunks already in canonical shape are skipped (no spurious
    update). ``--dry-run`` reports counts without touching writes.
  * ``--source-path PATH`` filter narrows the rewrite scope to one
    document.
  * Pagination handles collections that exceed the 300-row Chroma
    Cloud cap.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ── _rewrite_collection_metadata helper ─────────────────────────────────────


def _legacy_meta(**overrides):
    """Mock chunk metadata in the pre-4.3.1 shape."""
    base = {
        "source_path": "p.pdf",
        "content_hash": "abc",
        "chunk_text_hash": "def",
        "chunk_index": 0,
        "chunk_count": 1,
        "store_type": "pdf",
        "indexed_at": "2026-01-01T00:00:00+00:00",  # cargo
        "format": "pdf",                             # cargo
        "extraction_method": "docling",              # cargo
        "page_count": 5,                             # cargo
        "git_project_name": "myproj",                # → git_meta
        "git_branch": "main",                        # → git_meta
        "git_commit_hash": "deadbeef",               # → git_meta
        "git_remote_url": "https://example.com",     # → git_meta
        "bib_year": 0,                               # cargo (after fix #2 — keep here for fix #1 baseline)
        "bib_authors": "",
        "bib_venue": "",
        "bib_citation_count": 0,
    }
    base.update(overrides)
    return base


def _make_t3_mock(collection_name: str, chunks: list[dict]) -> tuple:
    """Build a mock ``T3Database`` whose ``get_or_create_collection`` returns
    a collection backed by *chunks*.

    Returns ``(t3, mock_col)`` so tests can assert against the col's
    captured ``update`` calls.
    """
    from nexus.db.t3 import T3Database

    db = T3Database.__new__(T3Database)  # bypass __init__ network calls
    db._local_mode = True
    db._write_sems = {}
    db._read_sems = {}
    db._sems_lock = MagicMock()
    db._sems_lock.__enter__ = lambda self_: None
    db._sems_lock.__exit__ = lambda *args: None

    mock_col = MagicMock()
    ids = [f"id-{i}" for i in range(len(chunks))]

    def _get(limit=300, offset=0, include=None, where=None):
        # Filter by where if provided (only equality on source_path supported here).
        filt_chunks = chunks
        filt_ids = ids
        if where:
            keep = [
                (i, m) for i, m in zip(ids, chunks)
                if all(m.get(k) == v for k, v in where.items())
            ]
            filt_ids = [i for i, _ in keep]
            filt_chunks = [m for _, m in keep]
        page_ids = filt_ids[offset:offset + limit]
        page_metas = filt_chunks[offset:offset + limit]
        return {"ids": page_ids, "metadatas": page_metas}

    mock_col.get.side_effect = _get
    db.get_or_create_collection = MagicMock(return_value=mock_col)
    db._client_for = MagicMock(return_value=MagicMock(
        get_or_create_collection=MagicMock(return_value=mock_col),
    ))

    # update_chunks needs a write semaphore; stub the lock context.
    sem = MagicMock()
    sem.__enter__ = lambda self_: None
    sem.__exit__ = lambda *args: None
    db._write_sem = MagicMock(return_value=sem)
    return db, mock_col


# ── Behaviour ───────────────────────────────────────────────────────────────


def test_rewrite_drops_cargo_and_legacy_git_keys() -> None:
    """RDR-101 Phase 5c: a legacy chunk with flat git_* + cargo keys
    becomes canonical — flat git_* and git_meta are DROPPED (catalog
    Document carries git provenance now), source_path is DROPPED, and
    store_type is DROPPED."""
    from nexus.db.t3 import _rewrite_collection_metadata

    chunks = [_legacy_meta()]
    db, mock_col = _make_t3_mock("knowledge__delos", chunks)

    updated, skipped, total = _rewrite_collection_metadata(
        db, "knowledge__delos",
    )

    assert total == 1
    assert updated == 1
    assert skipped == 0

    written = mock_col.update.call_args.kwargs["metadatas"][0]
    # Phase 5c — chunks no longer carry git provenance.
    assert "git_meta" not in written
    for k in ("git_project_name", "git_branch", "git_commit_hash", "git_remote_url"):
        assert k not in written
    # Phase 5c — source_path / store_type dropped.
    assert "source_path" not in written
    assert "store_type" not in written
    # indexed_at is canonical (paired with ttl_days for derived expiry).
    assert written["indexed_at"] == "2026-01-01T00:00:00+00:00"
    assert "format" not in written
    assert "extraction_method" not in written
    assert written["content_type"]


def test_rewrite_skips_already_canonical_chunks() -> None:
    """A chunk that's already canonical produces no col.update call."""
    from nexus.db.t3 import _rewrite_collection_metadata
    from nexus.metadata_schema import normalize

    canonical = normalize({
        "source_path": "p.pdf",
        "content_hash": "abc",
        "chunk_text_hash": "def",
        "chunk_index": 0,
        "chunk_count": 1,
    }, content_type="prose")

    db, mock_col = _make_t3_mock("docs__corpus", [canonical])

    updated, skipped, total = _rewrite_collection_metadata(
        db, "docs__corpus",
    )

    assert total == 1
    assert updated == 0
    assert skipped == 1
    mock_col.update.assert_not_called()


def test_rewrite_idempotent() -> None:
    """Two passes: first writes, second is a no-op."""
    from nexus.db.t3 import _rewrite_collection_metadata
    from nexus.metadata_schema import normalize

    chunks = [_legacy_meta()]
    db, mock_col = _make_t3_mock("knowledge__delos", chunks)

    _rewrite_collection_metadata(db, "knowledge__delos")
    # Mutate the in-memory chunks list to reflect the rewrite, simulating
    # what would be true on a real second pass.
    chunks[0] = normalize(chunks[0], content_type="prose")
    mock_col.update.reset_mock()

    updated, skipped, total = _rewrite_collection_metadata(
        db, "knowledge__delos",
    )
    assert updated == 0
    assert skipped == 1
    mock_col.update.assert_not_called()


def test_rewrite_dry_run_skips_writes() -> None:
    from nexus.db.t3 import _rewrite_collection_metadata

    chunks = [_legacy_meta(), _legacy_meta(content_hash="hh2")]
    db, mock_col = _make_t3_mock("knowledge__delos", chunks)

    updated, skipped, total = _rewrite_collection_metadata(
        db, "knowledge__delos", dry_run=True,
    )
    assert total == 2
    # dry_run reports what *would* be updated.
    assert updated == 2
    assert skipped == 0
    mock_col.update.assert_not_called()


def test_rewrite_filter_by_source_path() -> None:
    from nexus.db.t3 import _rewrite_collection_metadata

    chunks = [
        _legacy_meta(source_path="paper-a.pdf"),
        _legacy_meta(source_path="paper-b.pdf"),
    ]
    db, mock_col = _make_t3_mock("knowledge__delos", chunks)

    updated, skipped, total = _rewrite_collection_metadata(
        db, "knowledge__delos", source_path="paper-a.pdf",
    )
    assert total == 1
    assert updated == 1
    written_ids = mock_col.update.call_args.kwargs["ids"]
    assert written_ids == ["id-0"]


def test_rewrite_paginates_above_300() -> None:
    """Collections >300 chunks use multiple ``coll.get(limit=300)`` calls."""
    from nexus.db.t3 import _rewrite_collection_metadata

    chunks = [_legacy_meta(content_hash=f"h{i}") for i in range(750)]
    db, mock_col = _make_t3_mock("knowledge__delos", chunks)

    updated, _, total = _rewrite_collection_metadata(
        db, "knowledge__delos",
    )
    assert total == 750
    assert updated == 750
    # 750 / 300 = 3 pages.
    assert mock_col.get.call_count == 3


def test_rewrite_respects_300_record_write_cap() -> None:
    """update_chunks splits internally — verify update calls stay ≤300."""
    from nexus.db.t3 import _rewrite_collection_metadata

    chunks = [_legacy_meta(content_hash=f"h{i}") for i in range(450)]
    db, mock_col = _make_t3_mock("knowledge__delos", chunks)

    _rewrite_collection_metadata(db, "knowledge__delos")

    for call in mock_col.update.call_args_list:
        assert len(call.kwargs["ids"]) <= 300
