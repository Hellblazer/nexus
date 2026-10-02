# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.22 (RDR-192 Step 11, client half): the superseded-vector sweeps keep a
manifest-less legacy note's chunk PERMANENTLY, whether or not the tenant's
``rdr192-manifest-backfill`` rung record is verified (Sam 2026-10-02).

A legacy note's chash CAN be in a dropped set (an unrelated document that shared its text and
then dropped it, ``CatalogManifestSweepRepositoryTest`` Order 12), the T3 delete the sweeps
issue has no notes guard of its own, and the rung record is an attestation, not a census, so
the guard is neither removed nor gated on the record. These tests pin that:

* both production wiring sites (``_manifest_write_loop`` and
  ``sweep_deferred_superseded_vectors``) keep the note's chunk and delete the plain orphan,
  observed through the T3 delete each issues, with the rung record reported verified or not;
* the composition against a real engine (``test_real_engine_*``): the real
  ``orphaned_chashes`` reverse lookup, the real ledger carrying the verified record, the real
  T3 delete.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nexus.mcp_infra import (
    _manifest_write_loop,
    _PENDING_SWEEP_CANDIDATES,
    _pending_sweep_lock,
    _stash_pending_sweep,
    sweep_deferred_superseded_vectors,
)
from nexus.upgrade_ladder.registry import RUNG_RDR192_MANIFEST_BACKFILL

_RUNG = RUNG_RDR192_MANIFEST_BACKFILL
_NOTE_CHASH = "n" * 64


def _legacy_note_entry(chash: str = _NOTE_CHASH):
    """A live, note-shaped catalog entry: no file_path, meta.doc_id = its own chunk."""
    return SimpleNamespace(file_path="", meta={"doc_id": chash})


class _Reader:
    def __init__(self, notes, *, before, refs=None) -> None:
        self._notes = notes
        self._before = before
        self._refs = refs or {}
        self.list_by_collection_calls: list[str] = []

    def list_by_collection(self, collection):
        self.list_by_collection_calls.append(collection)
        return list(self._notes)

    def get_chunk_chashes(self, doc_id):
        return list(self._before)

    def docs_for_chashes(self, chashes):
        return {h: self._refs.get(h, []) for h in chashes}


# ── production wiring: both sites, observed through the T3 delete ─────────────


def _metas(*chashes: str):
    return [(i, {"chunk_text_hash": h, "chunk_index": i}) for i, h in enumerate(chashes)]


class _Writer:
    def atomic_manifest_replace(self, doc_id, chunks, *, collection):
        assert collection

    def resync_chunk_count_cache(self, doc_id):
        return None


def _delete_ids_for_write_loop(rung_verified: bool) -> list[str] | None:
    reader = _Reader([_legacy_note_entry()], before=[_NOTE_CHASH, "plain-orphan"])
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))), \
            patch("nexus.upgrade_ladder.rungs.rdr192_manifest_backfill.rdr192_backfill_complete",
                  return_value=rung_verified):
        _manifest_write_loop(_Writer(), {"doc-A": _metas("new1")}, "coll", reader=reader,
                             manifest_complete={"doc-A": "a" * 64})
    if not col.delete.called:
        return None
    return sorted(col.delete.call_args.kwargs["ids"])


@pytest.mark.parametrize("rung_verified", [False, True])
def test_manifest_write_loop_keeps_the_legacy_note_chunk(rung_verified: bool) -> None:
    assert _delete_ids_for_write_loop(rung_verified) == ["plain-orphan"]


@pytest.fixture
def _no_pending_leak():
    with _pending_sweep_lock:
        _PENDING_SWEEP_CANDIDATES.clear()
    yield
    with _pending_sweep_lock:
        _PENDING_SWEEP_CANDIDATES.clear()


def _delete_ids_for_deferred_sweep(rung_verified: bool) -> list[str] | None:
    reader = _Reader([_legacy_note_entry()], before=[], refs={})
    reader.get_chunk_chashes = lambda doc_id: ["final"]  # type: ignore[method-assign]
    col = MagicMock()
    _stash_pending_sweep("doc-D", "coll", {_NOTE_CHASH, "plain-orphan"})
    with patch("nexus.mcp_infra.get_catalog", return_value=reader), \
            patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))), \
            patch("nexus.upgrade_ladder.rungs.rdr192_manifest_backfill.rdr192_backfill_complete",
                  return_value=rung_verified):
        sweep_deferred_superseded_vectors("doc-D")
    if not col.delete.called:
        return None
    return sorted(col.delete.call_args.kwargs["ids"])


@pytest.mark.parametrize("rung_verified", [False, True])
def test_deferred_sweep_keeps_the_legacy_note_chunk(rung_verified: bool, _no_pending_leak) -> None:
    assert _delete_ids_for_deferred_sweep(rung_verified) == ["plain-orphan"]


# ── a real engine: the real reverse lookup, ledger and T3 delete ──────────────

_COLL = "knowledge__wbfpw22-note-guard__bge-base-en-v15-768__v1"


def _present(client, chash: str) -> bool:
    from nexus.errors import CollectionNotFoundError

    try:
        # include_non_live: the chunk under test has no live own-collection owner (the
        # legacy note carries no manifest row), so live(c) hides it from a plain get while
        # it is still stored. Whether it is STORED is the question.
        result = client.get_collection(_COLL).get(ids=[chash], include=[], include_non_live=True)
    except CollectionNotFoundError:
        return False
    return chash in (result.get("ids") or [])


def test_real_engine_sweep_keeps_a_legacy_note_chunk_even_with_the_rung_verified(t2_service_env):
    """The mcp_infra sweep composed with a real engine (nexus-z0o2p.32 review item 3):
    document A drops the chash a manifest-less legacy note names as its own identity, along
    with a plain orphan. The sweep keeps the note's chunk and deletes the orphan, both before
    the tenant's rung record and after the engine's ledger carries it."""
    import hashlib

    import nexus.db.http_vector_client as hvc
    from nexus.indexer_utils import CollectionDocumentsCache, live_note_chashes
    from nexus.mcp_infra import _sweep_superseded_vectors
    from nexus.upgrade_ladder.http_store import HttpLadderStore
    from tests._catalog_fixture_ops import ActiveCatalog, active_reader
    from tests._chunk_seed import seed_chunks_direct

    tenant = t2_service_env
    client = hvc.HttpVectorClient(tenant=tenant)
    cat = ActiveCatalog()
    reader = active_reader()

    texts = {
        "shared": "wbfpw22 shared text: a legacy note's chunk, also manifested by an indexed document.",
        "new": "wbfpw22 the indexed document's replacement chunk.",
        "orphan1": "wbfpw22 a plain orphan, dropped before the rung record.",
        "orphan2": "wbfpw22 a plain orphan, dropped after the rung record.",
    }
    h = {k: hashlib.sha256(t.encode()).hexdigest() for k, t in texts.items()}
    for k, text in texts.items():
        seed_chunks_direct(_COLL, ids=[h[k]], documents=[text], embed=True,
                           metadatas=[{"chunk_text_hash": h[k], "title": _COLL}])

    owner = cat.register_owner("wbfpw22-owner", "curator")
    doc_a = str(cat.register(owner, "wbfpw22-indexed-doc", content_type="knowledge",
                             physical_collection=_COLL))
    cat.append_manifest_chunks(doc_a, [{"chash": h["shared"], "position": 0}], collection=_COLL)
    cat.resync_chunk_count_cache(doc_a)
    # The legacy note: note-shaped, its own meta.doc_id names the shared chunk, no manifest row.
    cat.register(owner, "wbfpw22-legacy-note", content_type="knowledge",
                 physical_collection=_COLL, meta={"doc_id": h["shared"]})
    # Document A now replaces its manifest with `new`, dropping `shared`.
    cat.write_manifest(doc_a, [{"chash": h["new"], "position": 0}], collection=_COLL)
    assert all(_present(client, c) for c in h.values()), "controls: every chunk exists"

    def _sweep(orphan: str) -> None:
        _sweep_superseded_vectors(
            None, doc_a, {h["shared"], h[orphan]}, [{"chash": h["new"]}], _COLL,
            reader=reader,
            notes_provider=lambda: live_note_chashes(CollectionDocumentsCache(reader, _COLL).get()))

    # No rung record.
    _sweep("orphan1")
    assert _present(client, h["shared"]), "the legacy note's chunk must survive"
    assert not _present(client, h["orphan1"]), "control: the sweep ran and deleted a plain orphan"

    # The ledger now carries the verified record for this tenant: the guard does not move.
    with HttpLadderStore() as ledger:
        ledger.record_verified(_RUNG, package_version="7.99.0", detail="wbfpw22 test")

    _sweep("orphan2")
    assert _present(client, h["shared"]), "a verified record is an attestation, not a census: the guard stays"
    assert not _present(client, h["orphan2"]), "control: the sweep ran again and deleted a plain orphan"
    assert _present(client, h["new"]), "the replacement chunk is untouched"
