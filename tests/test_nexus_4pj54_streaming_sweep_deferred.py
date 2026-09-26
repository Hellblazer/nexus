# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4pj54: a streaming/incremental multi-batch PDF re-index must not
sweep T3 rows a LATER batch in the same run is about to re-append.

Diagnosis (T2 nexus/swept-count-streaming-reindex-2026-09-25, read-only,
develop @ 1e45a8727): a 66-chunk PDF re-indexed with an existing manifest
(``nx dt index --force --re-embed``) reported "swept 64" though only 2
chashes actually changed. Mechanism: ``pipeline_stages.uploader_loop`` (and
``doc_indexer._index_pdf_incremental``, for docs over 128 chunks) fires
``hooks.fire_batch`` once per upload batch, never setting
``manifest_complete`` (neither producer knows completeness at batch-fire
time). The FIRST batch legitimately carries chunk position 0 —
``_manifest_write_loop``'s position-0 gate reads that as "whole document"
and takes the REPLACE path, computing every chash the batch dropped and
sweeping them from T3 immediately. Most of those chashes are UNCHANGED
chunks a later batch in the same run is about to re-append — the sweep
deleted 62 live rows and the uploader re-created them.

The fix: a REPLACE whose batch is not proven complete (no producer
``manifest_complete`` claim) holds its dropped set instead of sweeping it
(``mcp_infra._stash_pending_sweep``); the document's completion fence
(``doc_indexer._fence_complete``, on a SUCCESSFUL stamp only) sweeps the
held candidates against the FINAL manifest
(``mcp_infra.sweep_deferred_superseded_vectors``). A file-atomic single-
batch producer (``manifest_complete`` present) is unaffected — it still
sweeps on the spot, exactly as before nexus-4pj54.

These are direct-function tests against ``_manifest_write_loop`` /
``sweep_deferred_superseded_vectors`` — the same level
``tests/test_superseded_vector_sweep.py``'s "production wiring" tests
already exercise the sibling code in this module at — driven with the
EXACT multi-call, no-``manifest_complete`` shape the two real multi-batch
producers use. ``_manifest_write_loop`` is the function
``manifest_write_batch_hook`` (registered as the default manifest hook)
calls on every ``hooks.fire_batch``; nothing about the fix lives above
that boundary, so a call-for-call replay of what a streaming run does IS
the production wiring, not a stand-in for it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.errors import IndexRunVerifyRefused
from nexus.mcp_infra import (
    _PENDING_SWEEP_CANDIDATES,
    _manifest_write_loop,
    _pending_sweep_lock,
    _stash_pending_sweep,
    discard_deferred_superseded_vectors,
    get_superseded_sweep_stats,
    reset_superseded_sweep_stats,
    sweep_deferred_superseded_vectors,
)


@pytest.fixture(autouse=True)
def _no_pending_sweeps_leak_between_tests():
    """Pending entries are process-lifetime state that
    reset_superseded_sweep_stats deliberately leaves alone; clear them so
    one test's held candidates never merge into the next test's doc-A."""
    with _pending_sweep_lock:
        _PENDING_SWEEP_CANDIDATES.clear()
    yield
    with _pending_sweep_lock:
        _PENDING_SWEEP_CANDIDATES.clear()


def _metas(*chashes: str, start: int = 0):
    """Build ``_manifest_write_loop``'s ``indexed_metas`` shape: a list of
    ``(position, meta)`` tuples, positions starting at *start* (a later
    batch in a streaming/incremental upload continues the GLOBAL chunk
    index, never resetting to 0 — RDR-108 Phase 3's ``chunk_index``
    injection, mirrored here rather than always starting at 0 like the
    sibling file's ``_metas`` helper, which only ever drives single-batch
    calls)."""
    return [
        (start + i, {"chunk_text_hash": h, "chunk_index": start + i})
        for i, h in enumerate(chashes)
    ]


class _StreamingFakeCatalog:
    """Tracks each doc's manifest as an ordered chash-per-position list,
    evolving across MULTIPLE ``_manifest_write_loop`` calls within one
    test — simulating a streaming/incremental producer's several
    ``hooks.fire_batch`` firings for the SAME document in one process
    (nexus-4pj54). Deliberately has NO ``write_manifest_many`` — this
    forces ``_manifest_write_loop`` down its per-doc
    ``atomic_manifest_replace``/``append_manifest_chunks`` fallback branch
    (mirrors ``tests/test_superseded_vector_sweep.py``'s ``_FakeCatalogHTTP``,
    which the SAME module's write-only-proxy tests rely on to reach that
    exact branch by omission).
    """

    def __init__(self, initial: dict[str, list[str]] | None = None,
                 refs: dict[str, list[str]] | None = None,
                 notes: list | None = None) -> None:
        self._manifests: dict[str, list[str]] = {
            k: list(v) for k, v in (initial or {}).items()
        }
        self._refs = refs or {}
        self._notes = notes or []
        self.list_by_collection_calls: list[str] = []

    # -- reads --
    def get_chunk_chashes(self, doc_id: str) -> list[str]:
        return [h for h in self._manifests.get(doc_id, []) if h]

    def get_manifests(self, doc_ids: list[str]):
        """``write_many`` branch's batch "before" read — row objects
        carrying a ``.chash`` attribute, one dict entry per doc_id."""
        from types import SimpleNamespace
        return {
            doc_id: [SimpleNamespace(chash=h) for h in self._manifests.get(doc_id, []) if h]
            for doc_id in doc_ids
        }

    def docs_for_chashes(self, chashes):
        return {h: self._refs.get(h, []) for h in chashes}

    def list_by_collection(self, collection: str) -> list:
        self.list_by_collection_calls.append(collection)
        return list(self._notes)

    # -- writes --
    def _apply(self, doc_id: str, chunks: list[dict]) -> None:
        existing = self._manifests.setdefault(doc_id, [])
        for c in chunks:
            pos = c["position"]
            while len(existing) <= pos:
                existing.append(None)
            existing[pos] = c["chash"]

    def atomic_manifest_replace(self, doc_id, chunks, *, collection):
        assert collection, "atomic_manifest_replace called with a blank collection"
        self._manifests[doc_id] = []
        self._apply(doc_id, chunks)

    def append_manifest_chunks(self, doc_id, chunks, *, collection):
        assert collection, "append_manifest_chunks called with a blank collection"
        self._apply(doc_id, chunks)

    def resync_chunk_count_cache(self, doc_id):
        return None


class _StreamingFakeCatalogWriteMany(_StreamingFakeCatalog):
    """Same tracking, but exposes ``write_manifest_many`` — the fast
    branch every real streaming/incremental producer's FIRST (position-0-
    bearing) batch actually reaches in production (nexus-67qsd/jk88j:
    whitelisted and reachable through the production writer since
    2026-08-08). Continuation slices (no position 0) still fall through
    to the per-doc ``append_manifest_chunks`` loop regardless — write_many
    only ever handles the position-0-bearing subset — exactly like
    production."""

    def write_manifest_many(self, docs, *, collection, complete=None, **_kw):
        assert collection, "write_manifest_many called with a blank collection"
        for doc_id, chunks in docs:
            self.atomic_manifest_replace(doc_id, chunks, collection=collection)
        return {"failed_doc_ids": [], "complete_refused": [], "complete_refused_count": 0}


def _t3_spy():
    """A ``nexus.db.make_t3`` double whose ``get_collection(...).delete``
    is a bare ``MagicMock`` — every test below asserts on its call
    history."""
    col = MagicMock()
    t3 = MagicMock(get_collection=MagicMock(return_value=col))
    return t3, col


# ── The bug's exact shape, per-doc fallback branch (atomic_manifest_replace) ──


def test_streaming_first_batch_replace_holds_candidates_instead_of_sweeping() -> None:
    """THE regression, per-doc branch. batch 1 carries position 0 (chunks
    0-1) and REPLACES a 6-chunk manifest down to 2 rows with no
    ``manifest_complete`` claim — exactly ``pipeline_stages.uploader_loop``'s
    and ``doc_indexer._index_pdf_incremental``'s shape. Before the fix this
    swept all 4 dropped chashes on the spot; none of them may be live-or-
    dead yet, since later batches haven't landed."""
    reset_superseded_sweep_stats()
    fake = _StreamingFakeCatalog(
        initial={"doc-A": ["old0", "old1", "old2", "old3", "old4", "old5"]})
    t3, col = _t3_spy()
    with patch("nexus.db.make_t3", return_value=t3), \
            patch("nexus.mcp_infra.get_catalog", return_value=fake):
        _manifest_write_loop(
            fake, {"doc-A": _metas("new0", "new1")}, "coll",
            reader=fake, manifest_complete=None,
        )
    col.delete.assert_not_called()
    assert get_superseded_sweep_stats()["swept"] == 0
    reset_superseded_sweep_stats()


def test_streaming_completion_sweeps_only_the_truly_superseded_chashes() -> None:
    """The full journey: batch 1 replaces (holds candidates), batch 2
    appends the UNCHANGED tail chunks back (same chashes as before — only
    the FIRST two chunks were genuinely re-extracted under new text this
    run), then the document's completion fence sweeps. Only the 2 chashes
    that never came back (old0/old1) are deleted; the 4 old2..old5 rows
    that batch 2 re-appended survive untouched at every step."""
    reset_superseded_sweep_stats()
    fake = _StreamingFakeCatalog(
        initial={"doc-A": ["old0", "old1", "old2", "old3", "old4", "old5"]})
    t3, col = _t3_spy()
    with patch("nexus.db.make_t3", return_value=t3), \
            patch("nexus.mcp_infra.get_catalog", return_value=fake):
        # Batch 1: position 0-1, REPLACE, no manifest_complete.
        _manifest_write_loop(
            fake, {"doc-A": _metas("new0", "new1")}, "coll",
            reader=fake, manifest_complete=None,
        )
        col.delete.assert_not_called()

        # Batch 2: continuation (positions 2-5), re-appends the UNCHANGED
        # chunks under their ORIGINAL chashes.
        _manifest_write_loop(
            fake,
            {"doc-A": _metas("old2", "old3", "old4", "old5", start=2)},
            "coll", reader=fake, manifest_complete=None,
        )
        col.delete.assert_not_called()

        # Document completion: sweep held candidates against the FINAL
        # manifest (mirrors doc_indexer._fence_complete's post-stamp call).
        sweep_deferred_superseded_vectors("doc-A")

    col.delete.assert_called_once()
    assert sorted(col.delete.call_args.kwargs["ids"]) == ["old0", "old1"]
    assert get_superseded_sweep_stats()["swept"] == 2
    # The final manifest still holds every chunk exactly once, in order.
    assert fake.get_chunk_chashes("doc-A") == [
        "new0", "new1", "old2", "old3", "old4", "old5",
    ]
    reset_superseded_sweep_stats()


def test_streaming_completion_respects_the_union_guard() -> None:
    """A chash the deferred candidate set still contains but that ANOTHER
    live document references must survive — the same union guard
    ``_sweep_superseded_vectors`` enforces for the immediate-sweep path,
    now proven for the deferred path too."""
    reset_superseded_sweep_stats()
    fake = _StreamingFakeCatalog(
        initial={"doc-A": ["shared", "gone"]},
        refs={"shared": ["doc-A", "doc-B"]},
    )
    t3, col = _t3_spy()
    with patch("nexus.db.make_t3", return_value=t3), \
            patch("nexus.mcp_infra.get_catalog", return_value=fake):
        _manifest_write_loop(
            fake, {"doc-A": _metas("new0")}, "coll",
            reader=fake, manifest_complete=None,
        )
        col.delete.assert_not_called()
        sweep_deferred_superseded_vectors("doc-A")

    col.delete.assert_called_once()
    assert col.delete.call_args.kwargs["ids"] == ["gone"], (
        "'shared' is still referenced by doc-B and must survive"
    )
    reset_superseded_sweep_stats()


def test_interrupted_run_leaves_candidates_unswept_never_deletes_live_rows() -> None:
    """An interruption after batch 1's REPLACE (no completion fence ever
    fires) must never sweep — leaving superseded rows unswept is
    acceptable (RDR-192's reaper is the backstop); deleting a row a
    resumed run still needs is not."""
    reset_superseded_sweep_stats()
    fake = _StreamingFakeCatalog(
        initial={"doc-A": ["old0", "old1", "old2"]})
    t3, col = _t3_spy()
    with patch("nexus.db.make_t3", return_value=t3), \
            patch("nexus.mcp_infra.get_catalog", return_value=fake):
        _manifest_write_loop(
            fake, {"doc-A": _metas("new0")}, "coll",
            reader=fake, manifest_complete=None,
        )
        # No completion fence call -- the run "crashed" here.
    col.delete.assert_not_called()
    reset_superseded_sweep_stats()


# ── The bug's exact shape, write_many fast branch ────────────────────────────


def test_write_many_branch_streaming_first_batch_holds_candidates() -> None:
    """Same scenario, but through the write_many fast branch every real
    streaming/incremental producer actually reaches in production."""
    reset_superseded_sweep_stats()
    fake = _StreamingFakeCatalogWriteMany(
        initial={"doc-A": ["old0", "old1", "old2", "old3", "old4", "old5"]})
    t3, col = _t3_spy()
    with patch("nexus.db.make_t3", return_value=t3), \
            patch("nexus.mcp_infra.get_catalog", return_value=fake):
        _manifest_write_loop(
            fake, {"doc-A": _metas("new0", "new1")}, "coll",
            reader=fake, manifest_complete=None,
        )
    col.delete.assert_not_called()
    reset_superseded_sweep_stats()


def test_write_many_branch_completion_sweeps_only_the_truly_superseded_chashes() -> None:
    reset_superseded_sweep_stats()
    fake = _StreamingFakeCatalogWriteMany(
        initial={"doc-A": ["old0", "old1", "old2", "old3", "old4", "old5"]})
    t3, col = _t3_spy()
    with patch("nexus.db.make_t3", return_value=t3), \
            patch("nexus.mcp_infra.get_catalog", return_value=fake):
        _manifest_write_loop(
            fake, {"doc-A": _metas("new0", "new1")}, "coll",
            reader=fake, manifest_complete=None,
        )
        col.delete.assert_not_called()
        _manifest_write_loop(
            fake,
            {"doc-A": _metas("old2", "old3", "old4", "old5", start=2)},
            "coll", reader=fake, manifest_complete=None,
        )
        col.delete.assert_not_called()
        sweep_deferred_superseded_vectors("doc-A")

    col.delete.assert_called_once()
    assert sorted(col.delete.call_args.kwargs["ids"]) == ["old0", "old1"]
    assert get_superseded_sweep_stats()["swept"] == 2
    reset_superseded_sweep_stats()


def test_write_many_branch_multiple_documents_in_one_batch_each_hold_independently() -> None:
    """A write_many batch can carry SEVERAL documents at once (RDR-108
    flush-grain aggregation). Each doc's dropped set must be held/swept
    independently of the others' completeness."""
    reset_superseded_sweep_stats()
    fake = _StreamingFakeCatalogWriteMany(
        initial={
            "doc-A": ["a-old0", "a-old1"],
            "doc-B": ["b-old0", "b-old1"],
        })
    t3, col = _t3_spy()
    with patch("nexus.db.make_t3", return_value=t3), \
            patch("nexus.mcp_infra.get_catalog", return_value=fake):
        # Both replace in the SAME write_many call; only doc-B claims
        # completeness (e.g. a small file-atomic doc riding alongside a
        # streaming one in the same flush-grain batch).
        _manifest_write_loop(
            fake,
            {
                "doc-A": _metas("a-new0"),
                "doc-B": _metas("b-new0"),
            },
            "coll", reader=fake,
            manifest_complete={"doc-B": "b" * 64},
        )
        # doc-B (complete) swept immediately; doc-A (not complete) held.
        col.delete.assert_called_once()
        assert sorted(col.delete.call_args.kwargs["ids"]) == ["b-old0", "b-old1"]

        sweep_deferred_superseded_vectors("doc-A")

    assert col.delete.call_count == 2
    assert sorted(col.delete.call_args.kwargs["ids"]) == ["a-old0", "a-old1"]
    reset_superseded_sweep_stats()


# ── Regression: a file-atomic single-batch producer is unaffected ──────────


def test_file_atomic_manifest_complete_still_sweeps_immediately_per_doc_branch() -> None:
    """``manifest_complete`` present (the file-atomic contract every
    non-streaming producer — code_indexer, prose_indexer, indexer.py's
    ChunkBatcher, single-flush doc_indexer paths — relies on) must sweep
    on the spot exactly as before nexus-4pj54: nothing to hold, nothing
    deferred."""
    reset_superseded_sweep_stats()
    fake = _StreamingFakeCatalog(initial={"doc-A": ["old0", "old1", "keep"]})
    t3, col = _t3_spy()
    with patch("nexus.db.make_t3", return_value=t3), \
            patch("nexus.mcp_infra.get_catalog", return_value=fake):
        _manifest_write_loop(
            fake, {"doc-A": _metas("keep", "new1")}, "coll",
            reader=fake, manifest_complete={"doc-A": "a" * 64},
        )
    col.delete.assert_called_once()
    assert sorted(col.delete.call_args.kwargs["ids"]) == ["old0", "old1"]
    # Nothing left pending for a completed, already-swept doc.
    sweep_deferred_superseded_vectors("doc-A")
    assert col.delete.call_count == 1, "completion sweep must be a no-op here"
    reset_superseded_sweep_stats()


def test_file_atomic_manifest_complete_still_sweeps_immediately_write_many_branch() -> None:
    reset_superseded_sweep_stats()
    fake = _StreamingFakeCatalogWriteMany(initial={"doc-A": ["old0", "old1", "keep"]})
    t3, col = _t3_spy()
    with patch("nexus.db.make_t3", return_value=t3), \
            patch("nexus.mcp_infra.get_catalog", return_value=fake):
        _manifest_write_loop(
            fake, {"doc-A": _metas("keep", "new1")}, "coll",
            reader=fake, manifest_complete={"doc-A": "a" * 64},
        )
    col.delete.assert_called_once()
    assert sorted(col.delete.call_args.kwargs["ids"]) == ["old0", "old1"]
    reset_superseded_sweep_stats()


# ── sweep_deferred_superseded_vectors on its own ─────────────────────────────


def test_deferred_sweep_is_a_true_noop_when_nothing_is_pending() -> None:
    """The common case (every non-streaming producer): no candidates were
    ever stashed for this doc_id, so the completion-fence call this test
    mirrors must cost nothing and touch neither the catalog nor T3."""
    with patch("nexus.mcp_infra.get_catalog") as get_cat, \
            patch("nexus.db.make_t3") as make_t3:
        sweep_deferred_superseded_vectors("doc-never-stashed")
    get_cat.assert_not_called()
    make_t3.assert_not_called()


# ── doc_indexer._fence_complete wiring ───────────────────────────────────────


class _StubFenceWriter:
    def __init__(self, *, result=None, raises: BaseException | None = None) -> None:
        self._result = result
        self._raises = raises
        self.closed = False

    def complete_index_run(self, doc_id, content_hash, chunk_count):
        if self._raises is not None:
            raise self._raises
        return self._result

    def close(self):
        self.closed = True


def test_fence_complete_triggers_the_deferred_sweep_on_success() -> None:
    from nexus.doc_indexer import _fence_complete

    writer = _StubFenceWriter(result={"referenced": 2, "present": 2, "missing": 0})
    with patch("nexus.catalog.factory.make_catalog_writer", return_value=writer), \
            patch("nexus.mcp_infra.sweep_deferred_superseded_vectors") as swept:
        _fence_complete("1.2.3", "c" * 64, 2)
    swept.assert_called_once_with("1.2.3")


def test_fence_complete_does_not_sweep_on_a_refused_stamp() -> None:
    """A refused completion means the manifest is NOT confirmed complete —
    sweeping here could still delete a row a genuinely finished batch
    would have re-appended. The refusal propagates; a later successful
    stamp (retry) is what triggers the sweep."""
    from nexus.doc_indexer import _fence_complete

    writer = _StubFenceWriter(raises=IndexRunVerifyRefused(
        doc_id="1.2.3", referenced=2, present=1, missing=1, chunk_count=2,
        server_detail="verify failed",
    ))
    with patch("nexus.catalog.factory.make_catalog_writer", return_value=writer), \
            patch("nexus.mcp_infra.sweep_deferred_superseded_vectors") as swept, \
            pytest.raises(IndexRunVerifyRefused):
        _fence_complete("1.2.3", "c" * 64, 2)
    swept.assert_not_called()


def test_fence_complete_does_not_sweep_on_a_transport_failure() -> None:
    from nexus.doc_indexer import _fence_complete

    writer = _StubFenceWriter(raises=RuntimeError("engine down"))
    with patch("nexus.catalog.factory.make_catalog_writer", return_value=writer), \
            patch("nexus.mcp_infra.sweep_deferred_superseded_vectors") as swept:
        _fence_complete("1.2.3", "c" * 64, 2)  # advisory-only: must not raise
    swept.assert_not_called()


def test_fence_complete_sweeps_even_on_the_pre_fence_engine_none_sentinel() -> None:
    """``None`` is the client's own "engine predates the fence" sentinel —
    not a failure, and not a refusal. The upload itself genuinely
    finished, so the deferred sweep must still run."""
    from nexus.doc_indexer import _fence_complete

    writer = _StubFenceWriter(result=None)
    with patch("nexus.catalog.factory.make_catalog_writer", return_value=writer), \
            patch("nexus.mcp_infra.sweep_deferred_superseded_vectors") as swept:
        _fence_complete("1.2.3", "c" * 64, 2)
    swept.assert_called_once_with("1.2.3")


# ── Failed / fenced runs discard, never sweep (review round 1) ───────────────


def test_discard_drops_the_held_candidates_without_sweeping_and_counts_it() -> None:
    reset_superseded_sweep_stats()
    fake = _StreamingFakeCatalog(initial={"doc-A": ["old0", "old1", "old2"]})
    t3, col = _t3_spy()
    with patch("nexus.db.make_t3", return_value=t3), \
            patch("nexus.mcp_infra.get_catalog", return_value=fake):
        _manifest_write_loop(
            fake, {"doc-A": _metas("new0")}, "coll",
            reader=fake, manifest_complete=None,
        )
        assert get_superseded_sweep_stats()["deferred_pending"] == 1

        assert discard_deferred_superseded_vectors("doc-A") == 3

        # A later completion call for the same doc finds nothing to sweep.
        sweep_deferred_superseded_vectors("doc-A")
    col.delete.assert_not_called()
    stats = get_superseded_sweep_stats()
    assert stats["deferred_discarded"] == 1
    assert stats["deferred_pending"] == 0
    assert stats["swept"] == 0
    reset_superseded_sweep_stats()
    assert get_superseded_sweep_stats()["deferred_discarded"] == 0


def test_discard_with_nothing_pending_is_a_counted_noop() -> None:
    reset_superseded_sweep_stats()
    assert discard_deferred_superseded_vectors("doc-never-stashed") == 0
    assert get_superseded_sweep_stats()["deferred_discarded"] == 0


def test_fence_fail_discards_the_held_sweep() -> None:
    """The real _fence_fail (only its catalog writer stubbed) must drop the
    doc's deferred sweep: a failed run's manifest is not complete."""
    from nexus.doc_indexer import _fence_fail

    class _FailWriter:
        def __init__(self) -> None:
            self.failed: list[tuple[str, str]] = []

        def fail_index_run(self, doc_id, error):
            self.failed.append((doc_id, error))

        def close(self):
            pass

    reset_superseded_sweep_stats()
    _stash_pending_sweep("1.2.3", "coll", {"old0", "old1"})
    writer = _FailWriter()
    with patch("nexus.catalog.factory.make_catalog_writer", return_value=writer), \
            patch("nexus.db.make_t3") as make_t3:
        _fence_fail("1.2.3", "boom")
    assert writer.failed == [("1.2.3", "boom")]
    assert "1.2.3" not in _PENDING_SWEEP_CANDIDATES
    make_t3.assert_not_called()
    assert get_superseded_sweep_stats()["deferred_discarded"] == 1
    reset_superseded_sweep_stats()


def test_fence_fail_still_discards_when_the_fail_write_raises() -> None:
    from nexus.doc_indexer import _fence_fail

    reset_superseded_sweep_stats()
    _stash_pending_sweep("1.2.3", "coll", {"old0"})
    with patch("nexus.catalog.factory.make_catalog_writer",
               side_effect=RuntimeError("engine down")):
        _fence_fail("1.2.3", "boom")  # advisory: never raises
    assert "1.2.3" not in _PENDING_SWEEP_CANDIDATES
    assert get_superseded_sweep_stats()["deferred_discarded"] == 1
    reset_superseded_sweep_stats()


def test_refused_stamp_discards_the_held_sweep_and_reraises_unchanged() -> None:
    """A refused completion stamp propagates past every _fence_fail call
    site, so _fence_complete itself must drop the doc's deferred sweep:
    never swept, not left pending, and the refusal re-raised as the same
    object."""
    from nexus.doc_indexer import _fence_complete

    refusal = IndexRunVerifyRefused(
        doc_id="1.2.3", referenced=2, present=1, missing=1, chunk_count=2,
        server_detail="verify failed",
    )
    reset_superseded_sweep_stats()
    _stash_pending_sweep("1.2.3", "coll", {"old0", "old1"})
    writer = _StubFenceWriter(raises=refusal)
    with patch("nexus.catalog.factory.make_catalog_writer", return_value=writer), \
            patch("nexus.db.make_t3") as make_t3, \
            pytest.raises(IndexRunVerifyRefused) as excinfo:
        _fence_complete("1.2.3", "c" * 64, 2)
    assert excinfo.value is refusal, "the refusal must propagate unchanged"
    assert "1.2.3" not in _PENDING_SWEEP_CANDIDATES, "a refused stamp leaked its deferred sweep"
    make_t3.assert_not_called()
    stats = get_superseded_sweep_stats()
    assert stats["deferred_discarded"] == 1
    assert stats["deferred_pending"] == 0
    assert stats["swept"] == 0
    reset_superseded_sweep_stats()


def test_run_summary_names_discarded_and_pending_sweeps(capsys) -> None:
    from nexus.commands._helpers import _emit_superseded_swept_info

    reset_superseded_sweep_stats()
    _stash_pending_sweep("doc-held", "coll", {"x"})
    _stash_pending_sweep("doc-failed", "coll", {"y"})
    discard_deferred_superseded_vectors("doc-failed")
    assert _emit_superseded_swept_info() is False
    out = capsys.readouterr().out
    assert "not run for 1 failed/fenced and 1 unfinished document(s)" in out
    assert "t3 gc -c COLLECTION" in out
    reset_superseded_sweep_stats()


# ── Pending attribution is per run/file (review round 2) ─────────────────────


def test_pending_held_from_before_a_reset_is_not_reported_against_the_next_file(capsys) -> None:
    """``reset_identity_drop_collectors`` resets once PER FILE in a --dir
    batch. An entry an earlier file left held (its completion stamp failed
    in transport) must not show up as "unfinished" in every later file's
    summary; a document the current file stashed and left held must, by
    name."""
    from nexus.commands._helpers import _emit_superseded_swept_info

    reset_superseded_sweep_stats()
    _stash_pending_sweep("doc-file-3", "coll", {"old0"})
    assert get_superseded_sweep_stats()["deferred_pending_doc_ids"] == ["doc-file-3"]

    reset_superseded_sweep_stats()  # the per-file reset before file 4
    stats = get_superseded_sweep_stats()
    assert stats["deferred_pending"] == 0
    assert stats["deferred_pending_doc_ids"] == []
    assert "doc-file-3" in _PENDING_SWEEP_CANDIDATES, "reset must not drop live state"
    _emit_superseded_swept_info()
    assert "unfinished" not in capsys.readouterr().out

    _stash_pending_sweep("doc-file-4", "coll", {"old1"})
    _emit_superseded_swept_info()
    out = capsys.readouterr().out
    assert "0 failed/fenced and 1 unfinished document(s)" in out
    assert "unfinished: doc-file-4" in out
    assert "doc-file-3" not in out
    reset_superseded_sweep_stats()


def test_restashing_an_earlier_files_held_doc_reports_it_again() -> None:
    """Re-indexing the stuck document in a later file makes it this file's
    work again: it counts if it is still held at that file's end."""
    reset_superseded_sweep_stats()
    _stash_pending_sweep("doc-A", "coll", {"old0"})
    reset_superseded_sweep_stats()
    _stash_pending_sweep("doc-A", "coll", {"old1"})
    assert get_superseded_sweep_stats()["deferred_pending_doc_ids"] == ["doc-A"]
    reset_superseded_sweep_stats()
