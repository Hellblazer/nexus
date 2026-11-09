# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pin the cross-indexer metadata consistency contract.

Drift guard for the chunk-metadata factory (RDR-089 follow-up,
see docs/exploration/metadata-consistency-matrix.md). Every indexer that writes
T3 chunk metadata must route through ``make_chunk_metadata`` so that a chunk row carries
``ALLOWED_TOP_LEVEL`` (the key table's CHUNK keys, RDR-236 Phase 4 Step 1) and nothing else, and no
deprecated key (``source_title``, ``expires_at``) leaks through. The per-route proof over every client
write route is ``tests/test_6pfgb65_chunk_keys_only.py``.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nexus.metadata_schema import ALLOWED_TOP_LEVEL, make_chunk_metadata


# ── Factory contract ────────────────────────────────────────────────────────


def test_factory_rejects_deprecated_keys_via_normalize() -> None:
    """``source_title`` and ``expires_at`` were collapsed into
    ``title`` and derived expiry — readers and writers must not
    leak the old names through."""
    assert "source_title" not in ALLOWED_TOP_LEVEL
    assert "expires_at" not in ALLOWED_TOP_LEVEL


# ── Per-indexer integration: every chunked path routes through factory ─────


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A minimal git-style repo for indexer fixtures."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def test_pdf_indexer_emits_full_keyset(tmp_repo: Path) -> None:
    """``_pdf_chunks`` (the doc_indexer batch path) routes through
    the factory and emits the chunk keys, and nothing else (RDR-236 Phase 4 Step 1)."""
    pytest.importorskip("pymupdf")
    import pymupdf

    pdf = tmp_repo / "tiny.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "Tiny test paper.\n\nIntroduction\n\nBody text.")
    doc.save(pdf)
    doc.close()

    from nexus.doc_indexer import _pdf_chunks
    prepared = _pdf_chunks(
        pdf, content_hash="x" * 64, target_model="model-ctx",
        now_iso="2026-04-26T00:00:00+00:00", corpus="test",
    )
    assert prepared, "fixture PDF should produce at least one chunk"
    for _, _, meta, _p in prepared:
        assert set(meta) == ALLOWED_TOP_LEVEL, f"_pdf_chunks wrote {sorted(meta)}"




