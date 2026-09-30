# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-w94eo: ``nx collection rewrite-metadata`` strips legacy keys through
the REAL engine.

The engine merges chunk metadata instead of replacing it, so a rewrite that
sends only the canonical dict can never remove a key the stored row carries
and the canonical form lacks: the command reported "updated" on every run and
never converged. It now names those keys in ``delete_keys``. Round trip:
seed a row carrying a legacy key, rewrite, read it back, rewrite again.
"""
from __future__ import annotations

import hashlib

from nexus.db import make_t3
from nexus.db.t3 import _rewrite_collection_metadata
from nexus.metadata_schema import make_chunk_metadata
from tests._catalog_fixture_ops import give_chunks_a_live_owner
from tests._chunk_seed import seed_chunks_direct

_COLLECTION = "docs__w94eo-rewrite__bge-base-en-v15-768__v1"
_TEXT = "a chunk indexed before the canonical metadata schema existed"
_LEGACY = "store_type"


def _seed(t3) -> str:
    chash = hashlib.sha256(_TEXT.encode()).hexdigest()
    meta = make_chunk_metadata(
        content_type="markdown", chunk_text_hash=chash, content_hash="c" * 64,
        chunk_start_char=0, chunk_end_char=len(_TEXT), page_number=0,
        indexed_at="2026-09-26T00:00:00Z", embedding_model="bge-base-en-v15-768",
        title="Doc", source_author="", section_title="", section_type="",
        tags="", category="",
    )
    # Substrate SQL with the engine's real embedding: the engine refuses an
    # ownerless upsert-chunks write from RDR-223 Phase 3 on.
    seed_chunks_direct(_COLLECTION, [chash], [_TEXT], [meta], embed=True)
    # A pre-canonical key, written the way an old client wrote it.
    t3.update_chunks(_COLLECTION, [chash], [{_LEGACY: "markdown"}])
    # RDR-192 Step 5 (nexus-wbfpw.10): get()/getWhere is a live-visibility
    # gated read; a raw upsert with no catalog manifest has no live owner.
    give_chunks_a_live_owner(_COLLECTION, [chash])
    return chash


def _stored(t3, chash: str) -> dict:
    got = t3.get_or_create_collection(_COLLECTION).get(ids=[chash], include=["metadatas"])
    assert got["ids"] == [chash], got
    return got["metadatas"][0]


def test_rewrite_metadata_strips_a_legacy_key_and_converges(t2_service_env) -> None:
    t3 = make_t3()
    chash = _seed(t3)
    assert _LEGACY in _stored(t3, chash)  # non-vacuity: the engine kept it

    updated, skipped, total = _rewrite_collection_metadata(t3, _COLLECTION)
    assert (updated, total) == (1, 1)
    meta = _stored(t3, chash)
    assert _LEGACY not in meta, meta
    assert meta.get("title") == "Doc", meta

    # Converged: a second run finds nothing to change.
    assert _rewrite_collection_metadata(t3, _COLLECTION) == (0, 1, 1)


def test_rewrite_metadata_never_deletes_bib_fields(t2_service_env) -> None:
    """nx enrich bib owns bib_* and writes them after indexing; a rewrite that
    deleted them could erase a concurrent enrichment. A placeholder bib key
    that normalize() drops is left alone, and the row still converges."""
    t3 = make_t3()
    chash = _seed(t3)
    t3.update_chunks(_COLLECTION, [chash], [{"bib_year": 0}])
    assert _stored(t3, chash).get("bib_year") == 0  # non-vacuity

    assert _rewrite_collection_metadata(t3, _COLLECTION)[0] == 1
    meta = _stored(t3, chash)
    assert _LEGACY not in meta, meta
    assert meta.get("bib_year") == 0, meta

    assert _rewrite_collection_metadata(t3, _COLLECTION) == (0, 1, 1)
