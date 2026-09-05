# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-39upx: sweep T3 rows a re-index leaves unreferenced.

A re-index that changes the extracted text writes chunks under NEW chashes —
content addressing working correctly — and atomic_manifest_replace repoints the
manifest at them. Nothing removed the OLD vector rows, so they persisted,
referenced by no manifest and still returned by vector search, which reads T3
directly rather than the manifest. Measured on tumbler 1.14.19 after the
nexus-gtltb normalizer fix: manifest 182 rows / 163 unique chashes, T3 180 rows,
17 of them referenced by nothing.

Round 2 (nexus-39upx substantive-critique, T2 21515, SIGNIFICANT 1):
``_sweep_superseded_vectors`` no longer computes its own manifest-less-note
set — it takes a ``notes_provider`` callable, so a batch reindex sharing one
collection can share ONE memoized ``CollectionDocumentsCache`` across every
document instead of re-fetching the whole collection's catalog documents
once per orphan-triggering document. Direct-call tests below supply
``notes_provider`` explicitly (a plain lambda); the ``_manifest_write_loop``
production-wiring tests further down prove the REAL provider
(``CollectionDocumentsCache`` + ``live_note_chashes``, fed by
``list_by_collection``) is wired in correctly, including memoization.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from nexus.mcp_infra import _sweep_superseded_vectors


def _chunks(*chashes: str) -> list[dict]:
    return [{"chash": h, "position": i} for i, h in enumerate(chashes)]


def _cat(refs: dict[str, list[str]]) -> MagicMock:
    cat = MagicMock()
    cat.docs_for_chashes.return_value = refs
    return cat


def _notes(*chashes: str):
    """A ``notes_provider`` returning a fixed set — the common case for
    direct ``_sweep_superseded_vectors`` calls below."""
    s = set(chashes)
    return lambda: s


def _raising_notes(exc: Exception):
    def _provider():
        raise exc
    return _provider


def test_superseded_chunks_are_deleted() -> None:
    cat = _cat({"old1": ["doc-A"], "old2": ["doc-A"]})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"old1", "old2", "keep"},
                                  _chunks("keep", "new1"), "coll", reader=cat,
                                  notes_provider=_notes())
    col.delete.assert_called_once()
    assert sorted(col.delete.call_args.kwargs["ids"]) == ["old1", "old2"]


def test_chunk_shared_with_another_document_is_NOT_deleted() -> None:
    """THE guard. Identical chunk text collapses to ONE T3 row shared by every
    document containing it, so "not in THIS manifest" is not "unreferenced".
    Deleting on that basis removes chunks other documents still depend on."""
    cat = _cat({"shared": ["doc-A", "doc-B"], "mine": ["doc-A"]})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"shared", "mine"},
                                  _chunks("new"), "coll", reader=cat,
                                  notes_provider=_notes())
    assert col.delete.call_args.kwargs["ids"] == ["mine"], "shared row must survive"


def test_chunk_shared_with_a_document_in_a_DIFFERENT_physical_collection_IS_deleted() -> None:
    """nexus-flkdc: docs_for_chashes is a catalog-WIDE reverse lookup, not
    scoped to a physical_collection. A document living in a DIFFERENT
    physical_collection (e.g. the same paper re-registered elsewhere —
    chash is a hash of the raw text, collection-independent) must not
    permanently pin a T3 row in the collection actually being swept: the
    delete() call below only ever touches THIS collection's rows, so a
    reference from another collection can never be the thing keeping
    this row alive."""
    from types import SimpleNamespace

    cat = _cat({"cross-collection": ["doc-A", "doc-other"]})
    cat.resolve_many.return_value = {
        "doc-other": SimpleNamespace(physical_collection="other-coll"),
    }
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"cross-collection"},
                                  _chunks("new"), "coll", reader=cat,
                                  notes_provider=_notes())
    col.delete.assert_called_once()
    assert col.delete.call_args.kwargs["ids"] == ["cross-collection"], (
        "a reference from a document in a DIFFERENT physical collection "
        "must not pin this row"
    )


# ── nexus-39upx hazard 2 (RDR-145): the manifest-less-note guard ───────────
#
# docs_for_chashes (hazard 1's guard, above) only sees MANIFESTED
# references. A store_put / nx store put note never has a manifest row —
# RDR-145 defers manifest-backed identity for notes, and catalog-003-
# soft-delete.xml's live_chunks view treats a manifest-less chunk as live
# BY DESIGN. Without a second guard, a note's chash is indistinguishable
# from a chash that fell out of the re-indexed document's OWN prior
# manifest: both simply read "not referenced" to docs_for_chashes.
#
# Round 2: the note SET is now caller-supplied via notes_provider — these
# tests prove _sweep_superseded_vectors respects whatever it returns.
# Scoping the note set to the right collection is CollectionDocumentsCache /
# catalog_documents_for_collection's job now (tests/test_indexer_utils_
# live_note_chashes.py), and production wiring is proven further down.


def test_note_chash_survives_even_when_the_union_guard_clears_it() -> None:
    """The load-bearing case: a note's chash has NO other document
    referencing it either (docs_for_chashes doesn't even know it exists),
    so hazard 1's guard alone would treat it as safe to delete. The note
    guard must still save it."""
    cat = _cat({})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"note-chash"}, _chunks("new"),
                                  "coll", reader=cat, notes_provider=_notes("note-chash"))
    col.delete.assert_not_called()


def test_note_chash_protected_while_a_genuine_orphan_in_the_SAME_batch_is_still_deleted() -> None:
    """Non-vacuous in both directions: the note guard must not become a
    blanket refusal that also hides genuine orphans it shares a sweep
    call with."""
    cat = _cat({})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"note-chash", "genuine-orphan"},
                                  _chunks("new"), "coll", reader=cat,
                                  notes_provider=_notes("note-chash"))
    col.delete.assert_called_once()
    assert col.delete.call_args.kwargs["ids"] == ["genuine-orphan"]


def test_note_lookup_failure_deletes_NOTHING_fail_open() -> None:
    """Same fail-open direction as the union guard: an unprovable
    note-set must never license a delete."""
    cat = _cat({})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"old"}, _chunks("new"), "coll",
                                  reader=cat, notes_provider=_raising_notes(RuntimeError("engine down")))
    col.delete.assert_not_called()


# ── nexus-39upx hazard 4 (honest output): the CLI-visible collectors ──────


def test_successful_sweep_records_the_swept_count() -> None:
    from nexus.mcp_infra import get_superseded_sweep_stats, reset_superseded_sweep_stats

    reset_superseded_sweep_stats()
    cat = _cat({"old1": ["doc-A"], "old2": ["doc-A"]})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"old1", "old2", "keep"},
                                  _chunks("keep", "new1"), "coll", reader=cat,
                                  notes_provider=_notes())
    stats = get_superseded_sweep_stats()
    assert stats["swept"] == 2
    assert stats["skipped"] == []
    reset_superseded_sweep_stats()


def test_note_lookup_failure_records_a_named_skip_not_silence() -> None:
    from nexus.mcp_infra import get_superseded_sweep_stats, reset_superseded_sweep_stats

    reset_superseded_sweep_stats()
    cat = _cat({})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"old"}, _chunks("new"), "coll",
                                  reader=cat, notes_provider=_raising_notes(RuntimeError("engine down")))
    stats = get_superseded_sweep_stats()
    assert stats["swept"] == 0
    assert stats["skipped"] == [
        {"doc_id": "doc-A", "collection": "coll", "reason": "note_lookup_failed"}
    ]
    reset_superseded_sweep_stats()


def test_delete_failure_records_a_named_skip() -> None:
    from nexus.mcp_infra import get_superseded_sweep_stats, reset_superseded_sweep_stats

    reset_superseded_sweep_stats()
    cat = _cat({"old": ["doc-A"]})
    col = MagicMock()
    col.delete.side_effect = RuntimeError("t3 unreachable")
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"old"}, _chunks("new"), "coll",
                                  reader=cat, notes_provider=_notes())
    stats = get_superseded_sweep_stats()
    assert stats["swept"] == 0
    assert stats["skipped"] == [
        {"doc_id": "doc-A", "collection": "coll", "reason": "delete_failed"}
    ]
    reset_superseded_sweep_stats()


def test_nothing_dropped_means_no_delete_call() -> None:
    cat = _cat({})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"a", "b"}, _chunks("a", "b"), "coll",
                                  reader=cat, notes_provider=_notes())
    col.delete.assert_not_called()


def test_reverse_lookup_failure_deletes_NOTHING() -> None:
    """Fail-open. A sweep that cannot prove orphanhood must not guess —
    over-retention is recoverable, over-deletion is not."""
    cat = MagicMock()
    cat.docs_for_chashes.side_effect = RuntimeError("engine down")
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"old"}, _chunks("new"), "coll",
                                  reader=cat, notes_provider=_notes())
    col.delete.assert_not_called()


def test_missing_collection_is_a_noop() -> None:
    cat = _cat({"old": ["doc-A"]})
    with patch("nexus.db.make_t3") as mk:
        _sweep_superseded_vectors(cat, "doc-A", {"old"}, _chunks("new"), None,
                                  reader=cat, notes_provider=_notes())
    mk.assert_not_called()


def test_delete_failure_does_not_raise_into_the_index() -> None:
    """Cleanup must never fail the indexing operation that triggered it."""
    cat = _cat({"old": ["doc-A"]})
    col = MagicMock()
    col.delete.side_effect = RuntimeError("t3 unreachable")
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"old"}, _chunks("new"), "coll",
                                  reader=cat, notes_provider=_notes())


def test_unreferenced_chash_with_empty_ref_list_is_deleted() -> None:
    """docs_for_chashes may omit a chash entirely, or map it to []."""
    cat = _cat({"gone": []})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"gone", "absent"},
                                  _chunks("new"), "coll", reader=cat,
                                  notes_provider=_notes())
    assert sorted(col.delete.call_args.kwargs["ids"]) == ["absent", "gone"]


# ── nexus-tl5qh: report the server's ACTUAL delete count, not requested ────
#
# o8dil.45 fixed four call sites that discarded _ServiceCollectionStub.delete()'s
# return and reported len(ids) unconditionally. These two sweep sites were
# left unfixed there (outside that bead's declared file ownership) and carry
# the identical defect: the engine's anti-join can legitimately delete fewer
# chunks than requested (one still referenced by another live document's
# manifest), and reporting the requested count over-states what actually
# happened.


def test_partial_delete_records_the_actual_swept_count_not_requested() -> None:
    from nexus.mcp_infra import get_superseded_sweep_stats, reset_superseded_sweep_stats

    reset_superseded_sweep_stats()
    cat = _cat({"old1": ["doc-A"], "old2": ["doc-A"]})
    col = MagicMock()
    # Server's anti-join refuses one of the two candidates.
    col.delete.return_value = 1
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"old1", "old2", "keep"},
                                  _chunks("keep", "new1"), "coll", reader=cat,
                                  notes_provider=_notes())
    stats = get_superseded_sweep_stats()
    assert stats["swept"] == 1, (
        f"expected the server's ACTUAL deleted count (1 of 2 requested), "
        f"got {stats['swept']!r} — the requested candidate size must never "
        f"stand in for what the server actually deleted"
    )
    reset_superseded_sweep_stats()


def test_partial_delete_logs_a_warning_not_the_success_event() -> None:
    import structlog

    cat = _cat({"old1": ["doc-A"], "old2": ["doc-A"]})
    col = MagicMock()
    col.delete.return_value = 1
    with structlog.testing.capture_logs() as logs, \
            patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"old1", "old2", "keep"},
                                  _chunks("keep", "new1"), "coll", reader=cat,
                                  notes_provider=_notes())
    warnings = [l for l in logs if l.get("log_level") == "warning"]
    assert any(l["event"] == "superseded_sweep_partial_delete" for l in warnings), (
        f"expected a partial-delete warning, got events: {[l['event'] for l in logs]}"
    )
    assert not any(l["event"] == "superseded_vectors_swept" for l in logs), (
        "a partial delete must not also emit the full-success event"
    )


def test_full_delete_records_requested_count_and_logs_no_partial_warning() -> None:
    """Control: when the server deletes everything requested (the common
    case), the actual count equals the requested count and no partial
    warning fires."""
    from nexus.mcp_infra import get_superseded_sweep_stats, reset_superseded_sweep_stats
    import structlog

    reset_superseded_sweep_stats()
    cat = _cat({"old1": ["doc-A"], "old2": ["doc-A"]})
    col = MagicMock()
    col.delete.return_value = 2
    with structlog.testing.capture_logs() as logs, \
            patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors(cat, "doc-A", {"old1", "old2", "keep"},
                                  _chunks("keep", "new1"), "coll", reader=cat,
                                  notes_provider=_notes())
    stats = get_superseded_sweep_stats()
    assert stats["swept"] == 2
    assert not any(l.get("event") == "superseded_sweep_partial_delete" for l in logs)
    reset_superseded_sweep_stats()


# ── nexus-tl5qh, batch sibling: _sweep_superseded_vectors_many ─────────────


def test_batch_partial_delete_records_the_actual_swept_count_not_requested() -> None:
    from nexus.mcp_infra import _sweep_superseded_vectors_many, get_superseded_sweep_stats, reset_superseded_sweep_stats

    reset_superseded_sweep_stats()
    # Empty ref lists: genuinely unreferenced by any live document (the
    # batch caller has already subtracted the batch's OWN live set before
    # this function runs, so a residual reference here would mean a
    # DIFFERENT document still needs it).
    cat = _cat({"old1": [], "old2": []})
    col = MagicMock()
    col.delete.return_value = 1  # server refuses one of the two candidates
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors_many(
            cat, {"doc-A": {"old1"}, "doc-B": {"old2"}}, "coll",
            reader=cat, notes_provider=_notes(),
        )
    stats = get_superseded_sweep_stats()
    assert stats["swept"] == 1, (
        f"expected the server's ACTUAL deleted count (1 of 2 requested), "
        f"got {stats['swept']!r}"
    )
    reset_superseded_sweep_stats()


def test_batch_partial_delete_logs_a_warning_not_the_success_event() -> None:
    from nexus.mcp_infra import _sweep_superseded_vectors_many
    import structlog

    cat = _cat({"old1": [], "old2": []})
    col = MagicMock()
    col.delete.return_value = 1
    with structlog.testing.capture_logs() as logs, \
            patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors_many(
            cat, {"doc-A": {"old1"}, "doc-B": {"old2"}}, "coll",
            reader=cat, notes_provider=_notes(),
        )
    warnings = [l for l in logs if l.get("log_level") == "warning"]
    assert any(l["event"] == "superseded_sweep_batch_partial_delete" for l in warnings), (
        f"expected a partial-delete warning, got events: {[l['event'] for l in logs]}"
    )
    assert not any(l["event"] == "superseded_vectors_swept_batch" for l in logs), (
        "a partial delete must not also emit the full-success event"
    )


def test_batch_full_delete_records_requested_count_and_logs_no_partial_warning() -> None:
    from nexus.mcp_infra import _sweep_superseded_vectors_many, get_superseded_sweep_stats, reset_superseded_sweep_stats
    import structlog

    reset_superseded_sweep_stats()
    cat = _cat({"old1": [], "old2": []})
    col = MagicMock()
    col.delete.return_value = 2
    with structlog.testing.capture_logs() as logs, \
            patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors_many(
            cat, {"doc-A": {"old1"}, "doc-B": {"old2"}}, "coll",
            reader=cat, notes_provider=_notes(),
        )
    stats = get_superseded_sweep_stats()
    assert stats["swept"] == 2
    assert not any(l.get("event") == "superseded_sweep_batch_partial_delete" for l in logs)
    reset_superseded_sweep_stats()


def test_batch_chunk_shared_with_a_document_in_a_DIFFERENT_physical_collection_IS_deleted() -> None:
    """nexus-flkdc, batch sibling of the per-doc test above:
    ``_sweep_superseded_vectors_many`` threads ``collection`` through to
    ``orphaned_chashes`` too. A document in a DIFFERENT physical_collection
    must not pin a row in the collection this batch delete() targets."""
    from types import SimpleNamespace

    from nexus.mcp_infra import _sweep_superseded_vectors_many

    cat = _cat({"cross-collection": ["doc-other"]})
    cat.resolve_many.return_value = {
        "doc-other": SimpleNamespace(physical_collection="other-coll"),
    }
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors_many(
            cat, {"doc-A": {"cross-collection"}}, "coll",
            reader=cat, notes_provider=_notes(),
        )
    col.delete.assert_called_once()
    assert col.delete.call_args.kwargs["ids"] == ["cross-collection"], (
        "a reference from a document in a DIFFERENT physical collection "
        "must not pin this row"
    )


def test_batch_chunk_shared_with_a_document_in_the_SAME_physical_collection_is_NOT_deleted() -> None:
    """Companion negative case: a live reference from a document actually
    IN the collection being swept must still protect the row — collection
    scoping narrows the guard, it must not disable it."""
    from types import SimpleNamespace

    from nexus.mcp_infra import _sweep_superseded_vectors_many

    cat = _cat({"shared": ["doc-other-live"]})
    cat.resolve_many.return_value = {
        "doc-other-live": SimpleNamespace(physical_collection="coll"),
    }
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _sweep_superseded_vectors_many(
            cat, {"doc-A": {"shared"}}, "coll",
            reader=cat, notes_provider=_notes(),
        )
    col.delete.assert_not_called()


# ── nexus-kgos1: the CALL SITE, not the function ────────────────────────────
#
# Every test above drives _sweep_superseded_vectors DIRECTLY with a MagicMock
# catalog, which answers any attribute. Production hands the sweep a
# _ServiceCatalogWriter — a closed-whitelist WRITE-ONLY proxy that raises
# AttributeError for get_chunk_chashes and docs_for_chashes. The AttributeError
# was swallowed by a bare `except` that logged nothing, so `before` came back
# empty, `dropped` was empty, and the sweep returned having deleted nothing.
#
# It had never deleted a row in production, in any mode. The tests above all
# pass against that broken wiring because they sit BELOW the layer that fails.
# These drive _manifest_write_loop with the real proxy.


class _FakeCatalogHTTP:
    """Stands in for HttpCatalogClient: has the reads, and records the writes."""

    def __init__(self, before: list[str], refs: dict[str, list[str]],
                 notes: list | None = None) -> None:
        self._before = before
        self._refs = refs
        self._notes = notes or []
        self.replaced: list[tuple[str, list[dict]]] = []
        # nexus-39upx round 2: records every list_by_collection call so
        # tests can assert the memoization contract (fetched at most once
        # per _manifest_write_loop call, not once per doc_id).
        self.list_by_collection_calls: list[str] = []

    # -- reads (present here; BLOCKED by the write-only proxy) --
    def get_chunk_chashes(self, doc_id: str) -> list[str]:
        return list(self._before)

    def docs_for_chashes(self, chashes):
        return {h: self._refs.get(h, []) for h in chashes}

    def list_by_collection(self, collection: str) -> list:
        self.list_by_collection_calls.append(collection)
        return list(self._notes)

    # -- writes (allowed through the whitelist) --
    def atomic_manifest_replace(
        self, doc_id: str, chunks: list[dict], *, collection: str,
    ) -> None:
        # RDR-191: a real caller must always supply a non-blank collection —
        # asserting here (rather than silently accepting/dropping it) is
        # exactly the double-side guard the bug's own postmortem calls for.
        assert collection, "atomic_manifest_replace called with a blank collection"
        self.replaced.append((doc_id, chunks))

    def resync_chunk_count_cache(self, doc_id: str) -> None:
        return None


def _metas(*chashes: str):
    return [(i, {"chunk_text_hash": h, "chunk_index": i}) for i, h in enumerate(chashes)]


def test_sweep_fires_through_the_real_write_only_proxy() -> None:
    """The regression: reads must not be routed through the write-only proxy."""
    from nexus.catalog.factory import _ServiceCatalogWriter
    from nexus.mcp_infra import _manifest_write_loop

    fake = _FakeCatalogHTTP(before=["old1", "old2", "keep"],
                            refs={"old1": ["doc-A"], "old2": ["doc-A"]})
    writer = _ServiceCatalogWriter(fake)
    col = MagicMock()

    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _manifest_write_loop(writer, {"doc-A": _metas("keep", "new1")}, "coll",
                             reader=fake)

    assert fake.replaced, "the manifest replace must still happen"
    col.delete.assert_called_once()
    assert sorted(col.delete.call_args.kwargs["ids"]) == ["old1", "old2"]


def test_write_only_proxy_really_does_block_the_reads() -> None:
    """Pins the mechanism, so this cannot regress into a passing no-op again.

    If the whitelist ever grows these ops, the fix above becomes untestable by
    construction and this test says so loudly rather than silently going green.
    """
    import pytest

    from nexus.catalog.factory import _ServiceCatalogWriter

    writer = _ServiceCatalogWriter(_FakeCatalogHTTP([], {}))
    for op in ("get_chunk_chashes", "docs_for_chashes"):
        with pytest.raises(AttributeError, match="not a catalog write op"):
            getattr(writer, op)


def test_sweep_is_skipped_loudly_when_docs_for_chashes_raises() -> None:
    """nexus-ocf52/b9puj real-shape follow-on: docs_for_chashes now raises
    RuntimeError on a count-reconciliation failure (a field-stripping hop
    on the reverse-lookup round-trip), not just on a transport-level
    exception. The sweep's fail-open contract must cover that raise
    identically to any other reverse-lookup failure — delete nothing, log
    the same skip event, never guess."""
    from nexus.mcp_infra import _manifest_write_loop

    class _NoReverseLookup(_FakeCatalogHTTP):
        def docs_for_chashes(self, chashes):
            raise RuntimeError(
                "manifest/docs_for_chashes truncated: page carries 0 "
                "tumblers but count says 1 — refusing"
            )

    fake = _NoReverseLookup(before=["old1", "old2", "keep"], refs={})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))), \
            patch("structlog.get_logger") as log:
        _manifest_write_loop(fake, {"doc-A": _metas("keep", "new1")}, "coll",
                             reader=fake)

    col.delete.assert_not_called()          # fail-open: nothing deleted
    events = [c.args[0] for c in log.return_value.warning.call_args_list if c.args]
    assert "superseded_sweep_skipped_no_reverse_lookup" in events, events


def test_sweep_is_skipped_loudly_when_the_before_read_fails() -> None:
    """Fail-open is right; failing SILENTLY is what hid this for the whole
    life of the feature."""
    from nexus.mcp_infra import _manifest_write_loop

    class _NoReads(_FakeCatalogHTTP):
        def get_chunk_chashes(self, doc_id):
            raise RuntimeError("engine down")

    fake = _NoReads(before=[], refs={})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))), \
            patch("structlog.get_logger") as log:
        _manifest_write_loop(fake, {"doc-A": _metas("new1")}, "coll", reader=fake)

    col.delete.assert_not_called()          # fail-open: nothing deleted
    events = [c.args[0] for c in log.return_value.warning.call_args_list if c.args]
    assert "superseded_sweep_before_read_failed" in events, events


def test_before_read_failure_records_a_named_skip_nexus_39upx_hazard_4() -> None:
    """Honest output: a failure that hid the sweep for its whole life
    (nexus-kgos1) must be CLI-visible, not just a structlog line no one
    is tailing."""
    from nexus.mcp_infra import (
        _manifest_write_loop,
        get_superseded_sweep_stats,
        reset_superseded_sweep_stats,
    )

    class _NoReads(_FakeCatalogHTTP):
        def get_chunk_chashes(self, doc_id):
            raise RuntimeError("engine down")

    reset_superseded_sweep_stats()
    fake = _NoReads(before=[], refs={})
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _manifest_write_loop(fake, {"doc-A": _metas("new1")}, "coll", reader=fake)

    stats = get_superseded_sweep_stats()
    assert stats["skipped"] == [
        {"doc_id": "doc-A", "collection": "coll", "reason": "before_read_failed"}
    ]
    reset_superseded_sweep_stats()


def test_reader_is_required_not_defaulted() -> None:
    """Omitting `reader` must be a TypeError, never a silent no-op sweep.

    This was briefly `reader=None` falling back to `cat`, to avoid updating the
    older tests. That default immediately re-created the very defect above one
    call site over — tests/catalog/test_manifest_write_many.py drove the loop
    with a double carrying no read methods, so the sweep walked into the caught
    AttributeError and no-opped on every run, unasserted. A missing wire-up has
    to fail at the call.
    """
    import pytest

    from nexus.mcp_infra import _manifest_write_loop

    with pytest.raises(TypeError, match="reader"):
        _manifest_write_loop(MagicMock(), {}, "coll")          # type: ignore[call-arg]
    with pytest.raises(TypeError, match="reader"):
        _sweep_superseded_vectors(MagicMock(), "doc-A", set(), [], "coll",
                                  notes_provider=lambda: set())  # type: ignore[call-arg]


# ── nexus-39upx round 2 SIGNIFICANT 1: memoized note-lookup at production
#    wiring (_manifest_write_loop shares ONE CollectionDocumentsCache
#    across every doc_id in the batch, not one fetch per document) ────────


def test_production_wiring_uses_list_by_collection_not_all_documents() -> None:
    """SIGNIFICANT 1's headline fix: the collection-wide catalog read must
    be the server-scoped list_by_collection, not the unscoped
    all_documents(content_type=...) cross-collection fetch."""
    fake = _FakeCatalogHTTP(before=["old1"], refs={},
                            notes=[])
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        from nexus.mcp_infra import _manifest_write_loop
        _manifest_write_loop(fake, {"doc-A": _metas("new1")}, "coll", reader=fake)

    assert fake.list_by_collection_calls == ["coll"]
    assert not hasattr(fake, "all_documents")


def test_production_wiring_protects_a_real_note_entry() -> None:
    from types import SimpleNamespace

    from nexus.mcp_infra import _manifest_write_loop

    note = SimpleNamespace(file_path="", meta={"doc_id": "note-chash"})
    fake = _FakeCatalogHTTP(before=["note-chash", "genuine-orphan"], refs={}, notes=[note])
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _manifest_write_loop(fake, {"doc-A": _metas("new1")}, "coll", reader=fake)

    col.delete.assert_called_once()
    assert col.delete.call_args.kwargs["ids"] == ["genuine-orphan"]


def test_collection_documents_fetched_at_most_once_per_batch_of_many_documents() -> None:
    """The actual round-2 ask: a batch reindex over MULTIPLE documents
    sharing one collection must fetch the collection's catalog documents
    ONCE, not once per orphan-triggering document."""
    from nexus.mcp_infra import _manifest_write_loop

    # Three documents, each dropping a distinct chash with no other
    # references — every one reaches the note-lookup call.
    fake = _FakeCatalogHTTP(
        before=["old1", "old2", "old3"],
        refs={},
        notes=[],
    )
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _manifest_write_loop(
            fake,
            {
                "doc-A": _metas("new-a"),
                "doc-B": _metas("new-b"),
                "doc-C": _metas("new-c"),
            },
            "coll", reader=fake,
        )

    # get_chunk_chashes returns the SAME `before` list for every doc_id in
    # this fake (it doesn't key by doc_id), so all three docs drop
    # old1/old2/old3 and all three reach the note-lookup — the interesting
    # assertion is that list_by_collection is still called exactly once.
    assert fake.list_by_collection_calls == ["coll"]
    assert col.delete.call_count == 3


def test_note_lookup_failure_is_cached_not_retried_per_document_in_the_batch() -> None:
    from nexus.mcp_infra import _manifest_write_loop

    class _FailingList(_FakeCatalogHTTP):
        def __init__(self) -> None:
            super().__init__(before=["old1", "old2", "old3"], refs={})
            self.list_by_collection_call_count = 0

        def list_by_collection(self, collection: str) -> list:
            self.list_by_collection_call_count += 1
            raise RuntimeError("engine down")

    fake = _FailingList()
    col = MagicMock()
    with patch("nexus.db.make_t3", return_value=MagicMock(
            get_collection=MagicMock(return_value=col))):
        _manifest_write_loop(
            fake,
            {"doc-A": _metas("new-a"), "doc-B": _metas("new-b"), "doc-C": _metas("new-c")},
            "coll", reader=fake,
        )

    col.delete.assert_not_called()  # fail-open across every document
    assert fake.list_by_collection_call_count == 1, (
        "a failed collection-documents fetch must not be retried once per "
        "document in the same batch"
    )
