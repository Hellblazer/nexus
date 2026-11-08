# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-y8xjh: a clean ``--force`` re-index clears a stale
``quality_gate_overridden`` through the REAL engine.

The engine merges chunk metadata instead of replacing it (nexus-w94eo). The
``nx index repo`` PDF path builds each row through ``make_chunk_metadata`` ->
``normalize()``, which drops ``quality_gate_overridden=False``, and then
through its own empty-value filter, so a clean rewrite's dict never carries
the key. Only ``delete_keys`` (named by
:func:`nexus.metadata_schema.rewrite_delete_keys`) can clear it. Round trip:
degraded index, clean forced re-index, read the stored row back.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.db import make_t3
from nexus.metadata_schema import make_chunk_metadata

_COLLECTION = "docs__y8xjh-pdf__bge-base-en-v15-768__v1"
_TEXT = "a chunk of a paper that was first indexed under a degraded extraction"


def _fake_pdf_chunks(overridden: bool):
    import hashlib

    def _chunks(file, content_hash, target_model, now_iso, corpus, chunk_chars=None, collection_name="",
                extraction_stats=None, extractor="auto"):
        chash = hashlib.sha256(_TEXT.encode()).hexdigest()
        meta = make_chunk_metadata(
            content_type="pdf",
            chunk_text_hash=chash,
            embedding_model=target_model,
        )
        # Two chunks, so the one-chunk batcher in _index refuses the file (oversize path).
        text2 = _TEXT + " (second page)"
        chash2 = hashlib.sha256(text2.encode()).hexdigest()
        meta2 = dict(meta, chunk_text_hash=chash2, page_number=2,
                     chunk_start_char=len(_TEXT), chunk_end_char=len(_TEXT) + len(text2))
        return [(chash, _TEXT, meta), (chash2, text2, meta2)]

    return _chunks


def _doc_id() -> str:
    """The catalog document the PDF is written under (one per test, memoized on the test's
    tenant token). nexus-z0o2p.20: a service-backed T3 never writes a file that has no document,
    so the file is registered first, as ``nx index repo`` does, and is written through the
    oversize writer, which sends ``metadata_merge`` with ``rewrite_delete_keys``."""
    import os

    from tests._catalog_fixture_ops import register_real_doc_id

    key = os.environ.get("NX_SERVICE_TOKEN", "")
    if key not in _DOC_IDS:
        _DOC_IDS[key] = register_real_doc_id(
            title="paper.pdf", physical_collection=_COLLECTION, owner_name="y8xjh-owner",
        )
    return _DOC_IDS[key]


_DOC_IDS: dict[str, str] = {}


def _index(tmp_path: Path, t3, monkeypatch, *, overridden: bool) -> None:
    from nexus.chunk_batcher import ChunkBatcher
    from nexus.indexer import _index_pdf_file

    monkeypatch.setattr("nexus.doc_indexer._pdf_chunks", _fake_pdf_chunks(overridden))
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF fake bytes for the y8xjh round trip")
    # The writer path registers nothing itself; `nx index repo` registers the
    # collection before it writes (the old upsert registered it on first write).
    from nexus.corpus import ensure_collection_registered

    ensure_collection_registered(_COLLECTION)
    doc_id = _doc_id()
    n = _index_pdf_file(
        pdf, tmp_path, _COLLECTION, "bge-base-en-v15-768",
        t3.get_or_create_collection(_COLLECTION), t3, "",
        "2026-09-26T00:00:00Z", 0.0,
        force=True,
        embed_fn=lambda texts: [[] for _ in texts],
        doc_id_resolver=lambda p: doc_id,
        batcher=ChunkBatcher(flush=lambda *a: None, max_chunks=1),  # refuses: the oversize path
    )
    assert n == 2


def _stored(t3) -> dict:
    import hashlib

    chash = hashlib.sha256(_TEXT.encode()).hexdigest()
    got = t3.get_or_create_collection(_COLLECTION).get(ids=[chash], include=["metadatas"], sensitivity="include")
    assert got["ids"] == [chash], got
    return got["metadatas"][0]






@pytest.mark.parametrize("metadatas,expected", [
    ([{"chunk_text_hash": "a", "embedding_model": "m"}], None),
    ([{"chunk_text_hash": "a", "embedding_model": "m"}, {"chunk_text_hash": "b"}], "embedding_model"),
])
def test_rewrite_delete_keys_names_keys_absent_from_any_row(metadatas, expected) -> None:
    """RDR-236 Phase 4 Step 1: the keys a full-rewrite writer owns are the CHUNK keys, so a clean batch names
    none and the retired document keys a stored row still holds are the Step 3 sweep's."""
    from nexus.metadata_schema import REWRITE_OWNED_KEYS, rewrite_delete_keys

    keys = rewrite_delete_keys(metadatas)
    assert keys == sorted(keys)
    assert set(keys) <= REWRITE_OWNED_KEYS == {"chunk_text_hash", "embedding_model"}
    if expected is None:
        assert keys == []
    else:
        assert expected in keys


def test_client_half_is_paired_with_an_engine_that_merges() -> None:
    """The client half (this file's subject) sends partial post-pass dicts
    and relies on the engine's merge + delete_keys (21030b19b). That commit
    is not in engine-service-v0.1.132, the newest tag when this was written,
    so the pin must name a later tag carrying it. Red until
    REQUIRED_ENGINE_VERSION moves in the same commit set: that is the
    mechanical pairing the wire ledger's Ack condition asks for (GH #1402,
    the 7.1.0/v0.1.62 inversion)."""
    from nexus.engine_version import REQUIRED_ENGINE_VERSION

    assert REQUIRED_ENGINE_VERSION >= (0, 1, 133), (
        f"REQUIRED_ENGINE_VERSION={REQUIRED_ENGINE_VERSION} predates the "
        "engine merge (nexus-w94eo); bump it to the tag carrying 21030b19b"
    )
