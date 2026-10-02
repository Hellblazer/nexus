# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.22 (RDR-192 Step 11, client half): the superseded-vector sweeps keep a
manifest-less legacy note's chunk only for a tenant whose ``rdr192-manifest-backfill`` rung
record is not verified.

A legacy note's chash CAN be in a dropped set (an unrelated document that shared its text and
then dropped it), and the T3 delete the sweeps issue has no notes guard of its own, so the
guard is gated per tenant, on the same fact the reaper and the engine sweep read, rather than
removed for every install. These tests pin:

* the provider ``mcp_infra._legacy_notes_provider`` (verified drops the guard and never
  fetches the collection's documents; no record, another rung's record, or an unreadable
  ledger keeps it; one ledger read and one document fetch however often it is called);
* both production wiring sites (``_manifest_write_loop`` and
  ``sweep_deferred_superseded_vectors``) hand the sweeps that provider, observed through the
  T3 delete each issues;
* the composition against a real engine (``test_real_engine_*``): the real
  ``orphaned_chashes`` reverse lookup, the real ledger, the real T3 delete.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nexus.mcp_infra import (
    _legacy_notes_provider,
    _manifest_write_loop,
    _PENDING_SWEEP_CANDIDATES,
    _pending_sweep_lock,
    _stash_pending_sweep,
    sweep_deferred_superseded_vectors,
)
from nexus.upgrade_ladder.registry import RUNG_RDR192_MANIFEST_BACKFILL

_RUNG = RUNG_RDR192_MANIFEST_BACKFILL
_NOTE_CHASH = "n" * 64


class _Ledger:
    """A ``CompletionLedger`` stand-in: only ``verified_rungs`` is read."""

    def __init__(self, rungs=(), *, error: Exception | None = None) -> None:
        self._rungs = frozenset(rungs)
        self._error = error
        self.reads = 0

    def verified_rungs(self):
        self.reads += 1
        if self._error is not None:
            raise self._error
        return self._rungs


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


# ── the provider ──────────────────────────────────────────────────────────────


def test_verified_ledger_drops_the_guard_and_never_fetches_documents() -> None:
    reader = _Reader([_legacy_note_entry()], before=[])
    ledger = _Ledger({_RUNG})

    notes = _legacy_notes_provider(reader, "coll", ledger=ledger)()

    assert notes == set()
    assert reader.list_by_collection_calls == [], "a verified tenant needs no note lookup at all"


def test_no_record_keeps_the_guard() -> None:
    reader = _Reader([_legacy_note_entry()], before=[])

    notes = _legacy_notes_provider(reader, "coll", ledger=_Ledger())()

    assert notes == {_NOTE_CHASH}


def test_another_rungs_record_does_not_open_the_gate() -> None:
    reader = _Reader([_legacy_note_entry()], before=[])

    notes = _legacy_notes_provider(reader, "coll", ledger=_Ledger({"some-other-rung"}))()

    assert notes == {_NOTE_CHASH}


def test_an_unreadable_ledger_keeps_the_guard() -> None:
    reader = _Reader([_legacy_note_entry()], before=[])

    notes = _legacy_notes_provider(
        reader, "coll", ledger=_Ledger(error=RuntimeError("engine down")))()

    assert notes == {_NOTE_CHASH}


def test_one_ledger_read_and_one_document_fetch_however_often_it_is_called() -> None:
    reader = _Reader([_legacy_note_entry()], before=[])
    ledger = _Ledger()
    provider = _legacy_notes_provider(reader, "coll", ledger=ledger)

    for _ in range(3):
        assert provider() == {_NOTE_CHASH}

    assert ledger.reads == 1
    assert reader.list_by_collection_calls == ["coll"]


def test_a_note_lookup_failure_still_raises_when_the_guard_is_on() -> None:
    """The sweeps' own ``note_lookup_failed`` skip depends on the provider raising."""

    class _Broken(_Reader):
        def list_by_collection(self, collection):
            raise RuntimeError("catalog down")

    provider = _legacy_notes_provider(_Broken([], before=[]), "coll", ledger=_Ledger())

    with pytest.raises(RuntimeError, match="catalog down"):
        provider()


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


def test_manifest_write_loop_unverified_tenant_keeps_the_legacy_note_chunk() -> None:
    assert _delete_ids_for_write_loop(False) == ["plain-orphan"]


def test_manifest_write_loop_verified_tenant_sweeps_the_legacy_note_chunk() -> None:
    assert _delete_ids_for_write_loop(True) == sorted([_NOTE_CHASH, "plain-orphan"])


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


def test_deferred_sweep_unverified_tenant_keeps_the_legacy_note_chunk(_no_pending_leak) -> None:
    assert _delete_ids_for_deferred_sweep(False) == ["plain-orphan"]


def test_deferred_sweep_verified_tenant_sweeps_the_legacy_note_chunk(_no_pending_leak) -> None:
    assert _delete_ids_for_deferred_sweep(True) == sorted([_NOTE_CHASH, "plain-orphan"])


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


def test_real_engine_sweep_keeps_a_legacy_note_chunk_until_the_rung_is_verified(t2_service_env):
    """The mcp_infra sweep composed with a real engine (nexus-z0o2p.32 review item 3):
    document A drops the chash a manifest-less legacy note names as its own identity.
    Before the tenant's rung record the sweep keeps it; once the engine's ledger carries the
    record, a fresh sweep (a fresh provider, as a fresh ``_manifest_write_loop`` call builds)
    deletes it, because nothing else references it."""
    import nexus.db.http_vector_client as hvc
    from nexus.mcp_infra import _sweep_superseded_vectors
    from nexus.upgrade_ladder.http_store import HttpLadderStore
    from tests._catalog_fixture_ops import ActiveCatalog, active_reader
    from tests._chunk_seed import seed_chunks_direct

    tenant = t2_service_env
    client = hvc.HttpVectorClient(tenant=tenant)
    cat = ActiveCatalog()
    reader = active_reader()

    shared_text = "wbfpw22 shared text: a legacy note's chunk, also manifested by an indexed document."
    new_text = "wbfpw22 the indexed document's replacement chunk."
    import hashlib

    shared = hashlib.sha256(shared_text.encode()).hexdigest()
    new = hashlib.sha256(new_text.encode()).hexdigest()
    for chash, text in ((shared, shared_text), (new, new_text)):
        seed_chunks_direct(_COLL, ids=[chash], documents=[text], embed=True,
                           metadatas=[{"chunk_text_hash": chash, "title": _COLL}])

    owner = cat.register_owner("wbfpw22-owner", "curator")
    doc_a = str(cat.register(owner, "wbfpw22-indexed-doc", content_type="knowledge",
                             physical_collection=_COLL))
    cat.append_manifest_chunks(doc_a, [{"chash": shared, "position": 0}], collection=_COLL)
    cat.resync_chunk_count_cache(doc_a)
    # The legacy note: note-shaped, its own meta.doc_id names the shared chunk, no manifest row.
    cat.register(owner, "wbfpw22-legacy-note", content_type="knowledge",
                 physical_collection=_COLL, meta={"doc_id": shared})
    # Document A now replaces its manifest with `new`, dropping `shared`.
    cat.write_manifest(doc_a, [{"chash": new, "position": 0}], collection=_COLL)
    assert _present(client, shared) and _present(client, new), "controls: both chunks exist"

    def _sweep() -> None:
        _sweep_superseded_vectors(
            None, doc_a, {shared}, [{"chash": new}], _COLL,
            reader=reader, notes_provider=_legacy_notes_provider(reader, _COLL))

    # No rung record: the legacy note's chunk is kept.
    _sweep()
    assert _present(client, shared), "no verified record: the legacy note's chunk must survive"

    # The ledger now carries the verified record for this tenant.
    with HttpLadderStore() as ledger:
        ledger.record_verified(_RUNG, package_version="7.99.0", detail="wbfpw22 test")

    _sweep()
    assert not _present(client, shared), (
        "verified record: nothing references the chunk, so the sweep deletes it like any other")
    assert _present(client, new), "the replacement chunk is untouched"
