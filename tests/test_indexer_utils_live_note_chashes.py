# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-39upx hazard 2 (RDR-145) + nexus-g6k6b (RUNFENCE precondition):
``catalog_documents_for_collection``, ``live_note_chashes``,
``non_complete_documents``, ``CollectionDocumentsCache``.

A manifest-diff sweep (the in-band ``_sweep_superseded_vectors`` and
``nx t3 gc``'s ``chashes_for_collection`` diff) can only see chashes that
have manifest rows. A ``store_put`` / ``nx store put`` note's chash never
has one — RDR-145 defers manifest-backed identity for notes, and
``catalog-003-soft-delete.xml``'s ``live_chunks`` view treats a
manifest-less chunk as live BY DESIGN. Without ``live_note_chashes``, a
note's chash is indistinguishable from a chash that fell out of a live
document's manifest via re-index: both simply read as "not referenced".

Separately, bead nexus-39upx's own comment thread (Hal, 2026-08-02) states
a BINDING requirement that ``nx t3 gc``'s corpus-wide sweep must filter on
``index_state='complete'`` — ``non_complete_documents`` implements that.

Round 2 (nexus-39upx substantive-critique, T2 21515) restructured
``live_note_chashes`` from an I/O-performing ``(reader, collection)``
function into a pure function over a pre-fetched document list, so a
single ``catalog_documents_for_collection`` fetch can serve both this and
``non_complete_documents`` — and so ``CollectionDocumentsCache`` can
memoize that one fetch across a whole batch reindex instead of paying it
once per orphan-triggering document (SIGNIFICANT 1).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.indexer_utils import (
    CollectionDocumentsCache,
    catalog_documents_for_collection,
    live_note_chashes,
    non_complete_documents,
)


def _entry(**kwargs) -> SimpleNamespace:
    defaults = {
        "file_path": "", "physical_collection": "knowledge__x", "meta": {},
        "index_state": "complete", "index_state_reported": True,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class _Reader:
    def __init__(self, docs: list) -> None:
        self._docs = docs
        self.calls: list[str] = []

    def list_by_collection(self, collection: str) -> list:
        self.calls.append(collection)
        return self._docs


# ── catalog_documents_for_collection ────────────────────────────────────


def test_fetch_is_scoped_server_side_to_the_collection() -> None:
    reader = _Reader([_entry()])
    docs = catalog_documents_for_collection(reader, "knowledge__x")
    assert len(docs) == 1
    assert reader.calls == ["knowledge__x"], "must call list_by_collection, not all_documents"


def test_fetch_reader_is_required() -> None:
    with pytest.raises(RuntimeError, match="no catalog reader"):
        catalog_documents_for_collection(None, "knowledge__x")


def test_fetch_failure_raises() -> None:
    class _Raising:
        def list_by_collection(self, collection: str) -> list:
            raise RuntimeError("engine down")

    with pytest.raises(RuntimeError, match="engine down"):
        catalog_documents_for_collection(_Raising(), "knowledge__x")


def test_fetch_none_result_is_an_empty_list() -> None:
    class _NoneReturning:
        def list_by_collection(self, collection: str) -> list | None:
            return None

    assert catalog_documents_for_collection(_NoneReturning(), "knowledge__x") == []


# ── live_note_chashes (pure) ─────────────────────────────────────────────


def test_note_chash_is_returned() -> None:
    docs = [_entry(meta={"doc_id": "notechash1"})]
    assert live_note_chashes(docs) == {"notechash1"}


def test_indexed_document_with_file_path_is_excluded() -> None:
    """A document WITH a file_path is indexed content (e.g. a PDF), not
    a note — it has (or should have) its own manifest and is covered by
    the ordinary union guard, not this exemption."""
    docs = [_entry(file_path="/papers/x.pdf", meta={"doc_id": "chash1"})]
    assert live_note_chashes(docs) == set()


def test_entry_with_no_doc_id_in_meta_contributes_nothing() -> None:
    assert live_note_chashes([_entry(meta={})]) == set()


def test_no_notes_is_an_empty_set_not_a_failure() -> None:
    assert live_note_chashes([]) == set()
    assert live_note_chashes(None) == set()


# ── non_complete_documents (pure, nexus-g6k6b) ───────────────────────────


def test_complete_document_is_not_a_blocker() -> None:
    docs = [_entry(index_state="complete")]
    assert non_complete_documents(docs) == []


def test_indexing_document_is_a_blocker() -> None:
    """The core hazard Hal named: a live in-flight run."""
    docs = [_entry(index_state="indexing", file_path="/x.pdf", meta={})]
    assert non_complete_documents(docs) == docs


def test_failed_document_is_a_blocker() -> None:
    """A fenced failure — its manifest may be a partial artifact of the
    first-batch atomic_manifest_replace, same hazard shape as indexing."""
    docs = [_entry(index_state="failed", file_path="/x.pdf", meta={})]
    assert non_complete_documents(docs) == docs


def test_none_index_state_is_a_blocker_when_reported() -> None:
    """Hal, verbatim: 'do not fix it by widening the filter to NULL —
    NULL means unknown, and sweeping unknown-state docs is exactly the
    mid-index-deletion hazard the precondition exists to prevent.' This
    is an INDEXED (non-note) document explicitly reporting index_state
    as null — never assumed safe."""
    docs = [_entry(index_state=None, index_state_reported=True,
                    file_path="/x.pdf", meta={})]
    assert non_complete_documents(docs) == docs


def test_note_shaped_document_is_EXCLUDED_even_with_none_index_state() -> None:
    """Hal, verbatim: store_put-origin documents NEVER carry index_state
    (a registered RUNFENCE exclusion) — 'their chunks are never swept:
    over-retention, not deletion... is correct'. Re-flagging notes here
    (on top of live_note_chashes's separate, unconditional protection)
    would make every knowledge collection holding even one note
    permanently refuse gc — not what Hal's ruling describes."""
    docs = [_entry(file_path="", meta={"doc_id": "notechash"}, index_state=None)]
    assert non_complete_documents(docs) == []


def test_unreported_index_state_is_floor_tolerant_not_a_blocker() -> None:
    """A pre-RUNFENCE engine that doesn't have the column at all: the
    same floor-tolerance stance the RUNFENCE arc used everywhere else —
    behaves exactly as it did before this check existed, never a
    refusal the operator cannot act on."""
    docs = [_entry(index_state=None, index_state_reported=False,
                    file_path="/x.pdf", meta={})]
    assert non_complete_documents(docs) == []


def test_mixed_collection_only_the_non_complete_ones_block() -> None:
    complete = _entry(index_state="complete", file_path="/a.pdf", meta={})
    indexing = _entry(index_state="indexing", file_path="/b.pdf", meta={})
    note = _entry(file_path="", meta={"doc_id": "notechash"}, index_state=None)
    assert non_complete_documents([complete, indexing, note]) == [indexing]


def test_empty_or_none_documents_is_no_blockers() -> None:
    assert non_complete_documents([]) == []
    assert non_complete_documents(None) == []


# ── CollectionDocumentsCache ──────────────────────────────────────────────


def test_cache_fetches_at_most_once() -> None:
    reader = _Reader([_entry()])
    cache = CollectionDocumentsCache(reader, "knowledge__x")
    cache.get()
    cache.get()
    cache.get()
    assert reader.calls == ["knowledge__x"], "must fetch exactly once across repeated .get() calls"


def test_cache_derives_notes_and_blockers_from_one_fetch() -> None:
    note = _entry(file_path="", meta={"doc_id": "notechash"}, index_state=None)
    indexing = _entry(index_state="indexing", file_path="/b.pdf", meta={})
    reader = _Reader([note, indexing])
    cache = CollectionDocumentsCache(reader, "knowledge__x")

    assert live_note_chashes(cache.get()) == {"notechash"}
    assert non_complete_documents(cache.get()) == [indexing]
    assert reader.calls == ["knowledge__x"], "one fetch must serve both derived views"


def test_cache_caches_and_reraises_the_same_failure() -> None:
    class _Raising:
        def __init__(self) -> None:
            self.calls = 0

        def list_by_collection(self, collection: str) -> list:
            self.calls += 1
            raise RuntimeError("engine down")

    reader = _Raising()
    cache = CollectionDocumentsCache(reader, "knowledge__x")
    with pytest.raises(RuntimeError, match="engine down"):
        cache.get()
    with pytest.raises(RuntimeError, match="engine down"):
        cache.get()
    assert reader.calls == 1, "a failed fetch must not be retried once per subsequent .get()"
