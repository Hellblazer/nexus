# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.0 (nexus-z0o2p.10): the request protocol of the multi-batch combined writer.

Drives :class:`nexus.catalog.multi_batch_write.MultiBatchDocumentWriter` against ``FakeCat``, a
recording catalog writer that also models the parts of the engine the protocol depends on: the
manifest (rows keyed by position, replaced by ``write_many``, upserted by ``append``), the
pre-run snapshot ``begin_index_run`` returns, the drop list ``write_many`` reports from the
manifest it replaced, and the completion check (``complete_index_run`` and ``write_many``'s
``complete`` compare the claimed count with the number of manifest ROWS). Modelling the engine is
what lets these tests catch a wrong completion count and a lost-and-resent first batch, which an
echoing fake cannot. The behaviour against the real engine is in
``tests/integration/test_rdr223_multi_batch_writer_journey.py``.

The protocol (RDR-223 Technical Design 1, bead nexus-z0o2p.10):

* one request: ONE ``write_manifest_many`` with chunks, sweep on, and the completion stamp riding
  it;
* several requests (a second batch, or a batch over the chunk cap; ``content_hash`` required):
  ``begin_index_run(snapshot_manifest=True)``, batch 1 by ``write_manifest_many`` with sweep OFF
  and no ``complete``, batches 2..N by ``append_manifest_chunks`` with chunks, the LAST append
  carrying the snapshot minus what the run wrote as ``sweep_chashes`` (at most 300, the rest in
  trailing sweep-only appends), and only after all of that ``complete_index_run`` with the manifest
  row count.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from nexus.catalog.multi_batch_write import (
    BatchWriteFailedError,
    MultiBatchDocumentWriter,
    RepeatedPositionError,
    write_document,
)
from nexus.errors import CombinedWriteEmbedTimeoutError, IndexRunVerifyRefused
from tests._time_seam import module_time

_COLLECTION = "docs__mbw-unit__bge-base-en-v15-768__v1"
_DOC = "1.1.1"


def _h(i: int) -> str:
    return f"{i:064x}"


def _rows(start: int, n: int, *, offset: int = 0) -> list[dict]:
    return [{"chash": _h(offset + start + i), "position": start + i} for i in range(n)]


def _chunks(start: int, n: int, *, offset: int = 0) -> list[dict]:
    return [
        {"chash": _h(offset + start + i), "text": f"text {offset + start + i}", "metadata": {}}
        for i in range(n)
    ]


def _batch(start: int, n: int, *, offset: int = 0) -> tuple[list[dict], list[dict]]:
    return _rows(start, n, offset=offset), _chunks(start, n, offset=offset)


class FakeCat:
    """Records every writer call in order and models the engine state the protocol relies on.

    *prior* is the manifest the previous run left (chash per position). *resend_first_write_many*
    executes the first ``write_manifest_many`` twice and returns the second response: a lost
    response that the transport layer resent.
    """

    def __init__(self, *, prior: list[str] | None = None, dropped_unknown: bool = False,
                 omit_dropped: bool = False, complete_refused: bool = False,
                 failed: bool = False, resend_first_write_many: bool = False,
                 snapshot: Any = "model", begin_returns_none: bool = False,
                 complete_returns_none: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.manifest: dict[int, str] = dict(enumerate(prior or []))
        self._dropped_unknown = dropped_unknown
        self._omit_dropped = omit_dropped
        self._complete_refused = complete_refused
        self._failed = failed
        self._resend = resend_first_write_many
        self._snapshot = snapshot
        self._begin_none = begin_returns_none
        self._complete_none = complete_returns_none
        self._write_many_calls = 0

    def _rec(self, name: str, **kw: Any) -> None:
        self.calls.append((name, kw))

    def _distinct(self) -> list[str]:
        return list(dict.fromkeys(self.manifest[p] for p in sorted(self.manifest)))

    def begin_index_run(self, doc_id, content_hash, run_id, collection, *, snapshot_manifest=False):
        self._rec("begin_index_run", doc_id=doc_id, content_hash=content_hash,
                  run_id=run_id, collection=collection, snapshot_manifest=snapshot_manifest)
        if self._begin_none:
            return None
        out: dict = {"ok": True}
        if snapshot_manifest:
            if self._snapshot == "model":
                out.update(prior_chashes=self._distinct(), prior_count=len(self.manifest))
            elif self._snapshot is not None:
                out.update(self._snapshot)
        return out

    def _do_write_many(self, doc_id, rows, chunks, sweep):
        before = self._distinct()
        self.manifest = {r["position"]: r["chash"] for r in rows}
        after = set(self.manifest.values())
        dropped = [c for c in before if c not in after]
        return dropped

    def write_manifest_many(self, docs, complete=None, *, sweep=False, chunks=None,
                            collection, force_re_embed=False, embedding_model=None):
        self._rec("write_manifest_many", docs=docs, complete=complete, sweep=sweep,
                  chunks=chunks, collection=collection, force_re_embed=force_re_embed,
                  embedding_model=embedding_model)
        (doc_id, rows), = docs
        self._write_many_calls += 1
        dropped = self._do_write_many(doc_id, rows, chunks, sweep)
        if self._resend and self._write_many_calls == 1:
            dropped = self._do_write_many(doc_id, rows, chunks, sweep)   # the resend
        out: dict = {
            "failed_doc_ids": [doc_id] if self._failed else [],
            "complete_refused": [], "complete_refused_count": 0,
            "swept": len(dropped) if sweep else 0, "sweep_skipped": 0, "sweep_detail": [],
        }
        if complete and (self._complete_refused or len(rows) != len(self.manifest)):
            out["complete_refused"] = [{"doc_id": doc_id, "referenced": len(self.manifest),
                                        "missing": 0 if not self._complete_refused else 1,
                                        "chunk_count": len(rows)}]
            out["complete_refused_count"] = 1
        if chunks is not None:
            out.update(chunks_written=len(chunks), embed_embedded=len(chunks),
                       embed_skipped=0, chunks_deduped=0)
        if self._dropped_unknown:
            out["dropped_unknown"] = [doc_id]
        elif not self._omit_dropped and not self._failed:
            out["dropped_chashes"] = {doc_id: list(dropped)}
            out["dropped_count"] = {doc_id: len(dropped)}
        return out

    def append_manifest_chunks(self, doc_id, chunks, *, collection, chunk_payload=None,
                               sweep_chashes=None, force_re_embed=False, embedding_model=None):
        self._rec("append_manifest_chunks", doc_id=doc_id, rows=chunks, collection=collection,
                  chunk_payload=chunk_payload, sweep_chashes=sweep_chashes,
                  force_re_embed=force_re_embed, embedding_model=embedding_model)
        for r in chunks:
            self.manifest[r["position"]] = r["chash"]
        out: dict = {"ok": True, "count": len(chunks)}
        if chunk_payload is not None:
            out.update(chunks_written=len(chunk_payload), embed_embedded=len(chunk_payload),
                       embed_skipped=0, chunks_deduped=0, chunks_unreferenced=0)
        if sweep_chashes:
            out.update(swept=len(sweep_chashes), sweep_skipped=0, sweep_detail={})
        return out

    def complete_index_run(self, doc_id, content_hash, chunk_count):
        self._rec("complete_index_run", doc_id=doc_id, content_hash=content_hash,
                  chunk_count=chunk_count)
        if self._complete_none:
            return None
        referenced = len(self.manifest)
        if chunk_count != referenced:
            # CatalogRepository.completeIndexRun: referenced = count(*) over manifest rows.
            raise IndexRunVerifyRefused(
                doc_id=doc_id, referenced=referenced, present=referenced, missing=0,
                chunk_count=chunk_count)
        return {"referenced": referenced, "present": referenced, "missing": 0}

    def fail_index_run(self, doc_id, error):
        self._rec("fail_index_run", doc_id=doc_id, error=error)

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def of(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]


def _writer(cat: FakeCat, **kw: Any) -> MultiBatchDocumentWriter:
    return MultiBatchDocumentWriter(cat, doc_id=_DOC, collection=_COLLECTION, **kw)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.retry as retry

    module_time(monkeypatch, retry).sleep = lambda s: None


# ── one request ───────────────────────────────────────────────────────────────


def test_one_batch_is_one_write_many_with_sweep_on_and_complete_riding_it() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1", run_id="run1")
    w.add_batch(*_batch(0, 3))
    res = w.finish()
    assert cat.names() == ["begin_index_run", "write_manifest_many"]
    wm = cat.of("write_manifest_many")[0]
    assert wm["sweep"] is True
    assert wm["complete"] == {_DOC: "hash1"}
    assert len(wm["chunks"]) == 3 and wm["collection"] == _COLLECTION
    begin = cat.of("begin_index_run")[0]
    # A fenced single request also snapshots, so a resent write_many still reports correctly.
    assert begin["run_id"] == "run1" and begin["snapshot_manifest"] is True
    assert res.completed and res.requests == 1 and res.batches == 1


def test_one_batch_without_a_content_hash_has_no_fence_and_no_complete() -> None:
    cat = FakeCat()
    w = _writer(cat)
    w.add_batch(*_batch(0, 2))
    res = w.finish()
    assert cat.names() == ["write_manifest_many"]
    assert cat.of("write_manifest_many")[0]["complete"] in (None, {})
    assert not res.completed


def test_one_batch_result_reports_the_drop_list_of_the_write() -> None:
    cat = FakeCat(prior=[_h(0), _h(50), _h(51)])
    w = _writer(cat)
    w.add_batch(*_batch(0, 2))
    res = w.finish()
    assert res.dropped == [_h(50), _h(51)] and res.dropped_count == 2
    assert res.dropped_unknown is False


def test_a_fenced_one_batch_write_reports_its_drop_list_from_the_snapshot_after_a_resend() -> None:
    """The resend's own dropped_chashes is empty; the fenced single request reports from the
    snapshot taken before any write."""
    cat = FakeCat(prior=[_h(0), _h(50), _h(51)], resend_first_write_many=True)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 2))
    res = w.finish()
    assert res.dropped == [_h(50), _h(51)] and res.dropped_count == 2


def test_an_unfenced_one_batch_write_can_under_report_after_a_resend() -> None:
    """Documented limit: with no fence there is no snapshot, so the write_many response is all
    there is, and a resend makes it empty. (The sweep itself ran server-side on the first
    attempt.)"""
    cat = FakeCat(prior=[_h(0), _h(50), _h(51)], resend_first_write_many=True)
    w = _writer(cat)
    w.add_batch(*_batch(0, 2))
    res = w.finish()
    assert res.dropped == [] and res.dropped_count == 0


def test_one_batch_dropped_unknown_is_flagged_and_has_no_list() -> None:
    cat = FakeCat(dropped_unknown=True)
    w = _writer(cat)
    w.add_batch(*_batch(0, 2))
    res = w.finish()
    assert res.dropped_unknown is True and res.dropped is None and res.dropped_count is None


def test_one_batch_response_with_no_drop_entry_is_an_old_engine_that_names_the_remedy() -> None:
    from nexus.errors import EngineOlderThanClientError

    cat = FakeCat(omit_dropped=True)
    w = _writer(cat)
    w.add_batch(*_batch(0, 2))
    with pytest.raises(EngineOlderThanClientError, match="dropped") as exc:
        w.finish()
    assert "older than this client" in str(exc.value) and "Upgrade the local engine" in str(exc.value)


def test_one_batch_response_with_disagreeing_drop_counts_is_a_corrupt_answer() -> None:
    cat = FakeCat()
    real = cat.write_manifest_many

    def lying(*a, **kw):
        out = real(*a, **kw)
        out["dropped_chashes"] = {_DOC: [_h(9)]}
        out["dropped_count"] = {_DOC: 2}
        return out

    cat.write_manifest_many = lying  # type: ignore[method-assign]
    w = _writer(cat)
    w.add_batch(*_batch(0, 2))
    with pytest.raises(BatchWriteFailedError, match="truncated or corrupt"):
        w.finish()


def test_one_batch_complete_refused_raises() -> None:
    cat = FakeCat(complete_refused=True)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 2))
    with pytest.raises(IndexRunVerifyRefused):
        w.finish()


def test_write_many_failure_of_the_document_raises() -> None:
    cat = FakeCat(failed=True)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 2))
    with pytest.raises(BatchWriteFailedError) as ei:
        w.finish()
    assert ei.value.doc_id == _DOC


def test_an_empty_content_hash_is_refused() -> None:
    with pytest.raises(ValueError, match="content_hash"):
        _writer(FakeCat(), content_hash="")


def test_a_repeated_chash_at_two_positions_stamps_with_the_row_count() -> None:
    """The same text at two positions is one chunk and two manifest rows; the engine's completion
    check counts rows. Batch 2 repeats chash 1 and batch 1 repeats chash 0 within itself."""
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1")
    rows1 = [{"chash": _h(0), "position": 0}, {"chash": _h(1), "position": 1},
             {"chash": _h(0), "position": 2}]
    rows2 = [{"chash": _h(1), "position": 3}, {"chash": _h(2), "position": 4}]
    w.add_batch(rows1, _chunks(0, 2))
    w.add_batch(rows2, _chunks(2, 1))
    res = w.finish()
    done = cat.of("complete_index_run")[0]
    assert done["chunk_count"] == 5 == len(cat.manifest)          # rows, not the 3 distinct chashes
    assert res.completed and res.distinct_chashes == 3 and res.manifest_rows == 5


def test_a_repeated_chash_within_a_single_request_completes() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1")
    rows = [{"chash": _h(0), "position": 0}, {"chash": _h(0), "position": 1}]
    w.add_batch(rows, _chunks(0, 1))
    assert w.finish().completed


# ── several requests ──────────────────────────────────────────────────────────


def test_three_batches_request_sequence() -> None:
    cat = FakeCat(prior=[_h(900), _h(901)])
    w = _writer(cat, content_hash="hash1", run_id="run1")
    for b in (_batch(0, 2), _batch(2, 2), _batch(4, 2)):
        w.add_batch(*b)
    res = w.finish()
    assert cat.names() == [
        "begin_index_run", "write_manifest_many", "append_manifest_chunks",
        "append_manifest_chunks", "complete_index_run",
    ]
    assert cat.of("begin_index_run")[0]["snapshot_manifest"] is True
    wm = cat.of("write_manifest_many")[0]
    # Batch 1 must neither sweep (it would delete the previous run's chunks before the later
    # batches land) nor carry the completion stamp (write_many stamps against ITS OWN row count).
    assert wm["sweep"] is False
    assert wm["complete"] in (None, {})
    assert [r["position"] for r in wm["docs"][0][1]] == [0, 1]
    a1, a2 = cat.of("append_manifest_chunks")
    assert a1["sweep_chashes"] in (None, [])
    assert a2["sweep_chashes"] == [_h(900), _h(901)]          # the LAST append carries the sweep
    assert [r["position"] for r in a2["rows"]] == [4, 5]
    assert len(a2["chunk_payload"]) == 2
    done = cat.of("complete_index_run")[0]
    assert done == {"doc_id": _DOC, "content_hash": "hash1", "chunk_count": 6}
    assert res.completed and res.requests == 3 and res.batches == 3 and res.distinct_chashes == 6
    assert res.dropped == [_h(900), _h(901)] and res.dropped_count == 2


def test_completion_stamp_is_the_very_last_call() -> None:
    cat = FakeCat(prior=[_h(i) for i in range(900, 1000)])
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 2), _batch(2, 2)):
        w.add_batch(*b)
    w.finish()
    assert cat.names()[-1] == "complete_index_run"
    assert "complete_index_run" not in cat.names()[:-1]


def test_snapshot_chashes_this_run_re_wrote_are_not_swept() -> None:
    """The snapshot holds the whole previous manifest, so for an unchanged document it is every
    chash. Anything the run wrote is subtracted client-side first: no sweep request for an
    unchanged re-index."""
    cat = FakeCat(prior=[_h(i) for i in range(6)])
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 2), _batch(2, 2), _batch(4, 2)):
        w.add_batch(*b)
    res = w.finish()
    for a in cat.of("append_manifest_chunks"):
        assert not a["sweep_chashes"]
    assert res.swept == 0 and res.dropped == [] and res.dropped_count == 0
    assert len(cat.of("append_manifest_chunks")) == 2          # no sweep-only append either


def test_a_lost_and_resent_first_batch_still_sweeps_the_previous_tail() -> None:
    """The transport resends a write_many whose response was lost. The resend reads the manifest
    its first attempt already replaced, so its own dropped_chashes is empty: the sweep must come
    from the begin snapshot, which was taken before any write."""
    old_tail = [_h(900), _h(901)]
    cat = FakeCat(prior=[_h(0), _h(1), *old_tail], resend_first_write_many=True)
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 2), _batch(2, 2)):
        w.add_batch(*b)
    res = w.finish()
    # Non-vacuity: the fake really did report nothing dropped on the response the writer saw.
    assert cat.of("write_manifest_many")[0]["sweep"] is False
    last = cat.of("append_manifest_chunks")[-1]
    assert last["sweep_chashes"] == old_tail
    assert res.dropped == old_tail


def test_the_sweep_ignores_a_lying_first_batch_response() -> None:
    """Even a first-batch response claiming dropped_unknown does not change the sweep: the
    snapshot is the source."""
    cat = FakeCat(prior=[_h(900)], dropped_unknown=True)
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 1), _batch(1, 1)):
        w.add_batch(*b)
    w.finish()
    assert cat.of("append_manifest_chunks")[-1]["sweep_chashes"] == [_h(900)]


def test_a_concurrent_writers_drop_is_hedged_in_from_batch_ones_response() -> None:
    """The snapshot is the source, but a chash a concurrent writer put in the manifest AFTER the
    snapshot shows up in batch 1's own dropped_chashes; the sweep is the union, minus the run's."""
    cat = FakeCat(prior=[_h(900), _h(901)], snapshot={"prior_chashes": [_h(900)], "prior_count": 1})
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 1), _batch(1, 1)):
        w.add_batch(*b)
    w.finish()
    assert cat.of("append_manifest_chunks")[-1]["sweep_chashes"] == [_h(900), _h(901)]


def test_long_dropped_list_is_split_into_300s_with_trailing_sweep_only_appends() -> None:
    dropped = [_h(10_000 + i) for i in range(650)]
    cat = FakeCat(prior=dropped)
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 2), _batch(2, 2)):
        w.add_batch(*b)
    res = w.finish()
    appends = cat.of("append_manifest_chunks")
    assert len(appends) == 1 + 2
    data, *sweeps = appends
    assert len(data["chunk_payload"]) == 2 and data["sweep_chashes"] == dropped[:300]
    # Trailing sweep-only appends: no rows, no chunks, the rest of the list in order.
    assert [(s["rows"], s["chunk_payload"]) for s in sweeps] == [([], None)] * 2
    assert [s["sweep_chashes"] for s in sweeps] == [dropped[300:600], dropped[600:]]
    assert res.swept == 650 and res.dropped_count == 650


def test_sweep_only_appends_follow_the_last_data_append_and_precede_complete() -> None:
    cat = FakeCat(prior=[_h(10_000 + i) for i in range(301)])
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 2), _batch(2, 2)):
        w.add_batch(*b)
    w.finish()
    kinds = [
        "data" if kw.get("chunk_payload") is not None else "sweep-only"
        for n, kw in cat.calls if n == "append_manifest_chunks"
    ]
    assert kinds == ["data", "sweep-only"]
    assert cat.names().index("complete_index_run") > max(
        i for i, n in enumerate(cat.names()) if n == "append_manifest_chunks")


# ── the engine's answers cannot be trusted blindly ────────────────────────────


def test_begin_answering_none_is_an_error_not_a_completed_write() -> None:
    cat = FakeCat(begin_returns_none=True)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    with pytest.raises(BatchWriteFailedError, match="fence"):
        w.add_batch(*_batch(1, 1))
    assert "write_manifest_many" not in cat.names()


def test_a_single_request_begin_answering_none_is_also_an_error() -> None:
    cat = FakeCat(begin_returns_none=True)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    with pytest.raises(BatchWriteFailedError, match="fence"):
        w.finish()


def test_complete_answering_none_is_an_error_not_completed() -> None:
    cat = FakeCat(complete_returns_none=True)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    w.add_batch(*_batch(1, 1))
    with pytest.raises(BatchWriteFailedError, match="NOT stamped"):
        w.finish()


@pytest.mark.parametrize("snapshot", [None, {}, {"prior_chashes": []}])
def test_a_begin_answer_with_no_snapshot_is_an_old_engine_that_names_the_remedy(snapshot) -> None:
    """``begin_index_run(snapshot_manifest=True)`` answered without the snapshot fields: the engine
    predates the writer's protocol. EngineOlderThanClientError, with the remedy, not a bare
    BatchWriteFailedError that a batch run would repeat for every record (nexus-z0o2p.35, I3)."""
    from nexus.errors import EngineOlderThanClientError

    cat = FakeCat(snapshot=snapshot)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    with pytest.raises(EngineOlderThanClientError, match="pre-run manifest") as exc:
        w.add_batch(*_batch(1, 1))
    assert "older than this client" in str(exc.value) and "Upgrade the local engine" in str(exc.value)
    assert "write_manifest_many" not in cat.names()
    # The stamp is committed when begin answers, so the run must still be abort()-able.
    w.abort("unusable snapshot")
    assert cat.of("fail_index_run") == [{"doc_id": _DOC, "error": "unusable snapshot"}]


@pytest.mark.parametrize("snapshot", [{"prior_chashes": [_h(1)], "prior_count": 0},
                                      {"prior_chashes": [_h(1), _h(2)], "prior_count": 1}])
def test_a_self_contradicting_snapshot_is_an_error_before_any_write(snapshot) -> None:
    """The fields are there but disagree: a corrupt answer, not an old engine."""
    cat = FakeCat(snapshot=snapshot)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    with pytest.raises(BatchWriteFailedError, match="pre-run manifest"):
        w.add_batch(*_batch(1, 1))
    assert "write_manifest_many" not in cat.names()
    w.abort("unusable snapshot")
    assert cat.of("fail_index_run") == [{"doc_id": _DOC, "error": "unusable snapshot"}]


def test_chunks_the_engine_reports_unreferenced_are_a_hard_error() -> None:
    cat = FakeCat()
    real = cat.append_manifest_chunks

    def dropping(*a, **kw):
        out = real(*a, **kw)
        out["chunks_unreferenced"] = 1
        return out

    cat.append_manifest_chunks = dropping  # type: ignore[method-assign]
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 2))
    w.add_batch(*_batch(2, 2))
    with pytest.raises(BatchWriteFailedError, match="referenced by no row"):
        w.finish()


def test_an_append_response_without_chunks_unreferenced_is_a_hard_error() -> None:
    """append and append_many always report it; absence is an engine that cannot say."""
    cat = FakeCat()
    real = cat.append_manifest_chunks

    def silent(*a, **kw):
        out = real(*a, **kw)
        out.pop("chunks_unreferenced", None)
        return out

    cat.append_manifest_chunks = silent  # type: ignore[method-assign]
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 2))
    w.add_batch(*_batch(2, 2))
    with pytest.raises(BatchWriteFailedError, match="chunks_unreferenced"):
        w.finish()


# ── the fence is mandatory for a multi-request document ───────────────────────


def test_a_second_batch_without_a_content_hash_is_refused_before_batch_one_is_sent() -> None:
    cat = FakeCat()
    w = _writer(cat)
    w.add_batch(*_batch(0, 2))
    with pytest.raises(ValueError, match="content_hash"):
        w.add_batch(*_batch(2, 2))
    assert cat.calls == []


def test_a_batch_over_the_cap_without_a_content_hash_is_refused_before_anything_is_sent() -> None:
    cat = FakeCat()
    w = _writer(cat, chunk_cap=4)
    with pytest.raises(ValueError, match="content_hash"):
        w.add_batch(*_batch(0, 10))
    assert cat.calls == []


def test_a_single_request_may_go_unfenced() -> None:
    cat = FakeCat()
    w = _writer(cat, chunk_cap=4)
    w.add_batch(*_batch(0, 4))
    assert w.finish().batches == 1


# ── rows and positions ────────────────────────────────────────────────────────


def test_a_repeated_position_within_a_batch_fails() -> None:
    cat = FakeCat()
    w = _writer(cat)
    rows = [{"chash": _h(1), "position": 0}, {"chash": _h(2), "position": 0}]
    with pytest.raises(RepeatedPositionError):
        w.add_batch(rows, _chunks(1, 2))
    assert cat.calls == []


def test_a_repeated_position_across_batches_fails_before_it_is_sent() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 2))
    with pytest.raises(RepeatedPositionError):
        w.add_batch(*_batch(1, 2, offset=100))    # position 1 again, a different chash
    assert cat.calls == []                          # nothing was sent for the bad batch


def test_a_row_without_an_integer_position_fails() -> None:
    w = _writer(FakeCat())
    with pytest.raises(ValueError, match="position"):
        w.add_batch([{"chash": _h(1)}], _chunks(1, 1))


def test_a_payload_chunk_no_row_references_fails() -> None:
    """A chunk the batch's own rows do not reference would be an ownerless chunk by construction."""
    w = _writer(FakeCat())
    with pytest.raises(ValueError, match="no row"):
        w.add_batch(_rows(0, 1), _chunks(0, 2))


def test_a_batch_over_the_chunk_cap_is_split_into_requests_no_larger_than_it() -> None:
    cat = FakeCat(prior=[_h(900)])
    w = _writer(cat, content_hash="hash1", chunk_cap=4)
    w.add_batch(*_batch(0, 10))
    res = w.finish()
    sizes = [len(c["chunks"]) for c in cat.of("write_manifest_many")] + [
        len(a["chunk_payload"]) for a in cat.of("append_manifest_chunks")]
    assert sizes == [4, 4, 2]
    assert res.batches == 3 and res.distinct_chashes == 10
    # One document across the slices: positions contiguous, the sweep on the LAST slice only.
    appends = cat.of("append_manifest_chunks")
    assert [r["position"] for r in cat.of("write_manifest_many")[0]["docs"][0][1]] == [0, 1, 2, 3]
    assert [r["position"] for a in appends for r in a["rows"]] == [4, 5, 6, 7, 8, 9]
    assert appends[0]["sweep_chashes"] in (None, []) and appends[1]["sweep_chashes"] == [_h(900)]
    assert cat.of("write_manifest_many")[0]["sweep"] is False


@pytest.mark.parametrize("configured", [301, 1000, 100_000])
def test_a_chunk_cap_env_override_above_300_is_clamped_to_300(monkeypatch, configured) -> None:
    """NX_ONNX_LOCAL_UPSERT_CHUNK_CAP has no upper bound, but the engine caps append_many at 300 and
    the 300-record write cap holds everywhere: no request may carry more, whatever the operator
    set."""
    import nexus.db.http_vector_client as hvc

    monkeypatch.setattr(hvc, "_ONNX_LOCAL_UPSERT_CHUNK_CAP", configured)
    monkeypatch.setattr(hvc, "_serving_embedding_mode", lambda *a, **k: "onnx-local")
    assert hvc.per_collection_chunk_cap(_COLLECTION) == configured      # the override is live
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1")                              # no explicit chunk_cap
    w.add_batch(*_batch(0, 700))
    w.finish()
    sizes = [len(c["chunks"]) for c in cat.of("write_manifest_many")] + [
        len(a["chunk_payload"]) for a in cat.of("append_manifest_chunks")]
    assert sizes == [300, 300, 100]
    assert max(sizes) <= 300


def test_a_chunk_referenced_by_two_slices_is_sent_once() -> None:
    """Rows repeating a chash (the same text at two positions) share one chunk: the second slice's
    payload does not resend it, and the completion stamp counts both rows."""
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1", chunk_cap=2)
    rows = [{"chash": _h(1), "position": 0}, {"chash": _h(2), "position": 1},
            {"chash": _h(1), "position": 2}, {"chash": _h(3), "position": 3}]
    chunks = [{"chash": _h(i), "text": f"t{i}", "metadata": {}} for i in (1, 2, 3)]
    w.add_batch(rows, chunks)
    w.finish()
    sent = [c["chash"] for c in cat.of("write_manifest_many")[0]["chunks"]] + [
        c["chash"] for c in cat.of("append_manifest_chunks")[0]["chunk_payload"]]
    assert sent == [_h(1), _h(2), _h(3)]
    assert cat.of("complete_index_run")[0]["chunk_count"] == 4


def test_an_empty_batch_fails() -> None:
    w = _writer(FakeCat())
    with pytest.raises(ValueError):
        w.add_batch([], [])


def test_finish_with_no_batch_writes_an_empty_manifest_and_stamps_zero() -> None:
    """A re-index that yields no chunks must still clear the old manifest."""
    cat = FakeCat(prior=[_h(1), _h(2)])
    w = _writer(cat, content_hash="hash1")
    res = w.finish(allow_empty=True)
    wm = cat.of("write_manifest_many")[0]
    assert wm["docs"] == [(_DOC, [])] and wm["sweep"] is True and wm["chunks"] is None
    assert wm["complete"] == {_DOC: "hash1"}
    assert cat.manifest == {} and res.completed
    assert res.dropped == [_h(1), _h(2)]


def test_a_bare_finish_with_no_batch_raises_and_sends_nothing() -> None:
    cat = FakeCat(prior=[_h(1)])
    w = _writer(cat, content_hash="hash1")
    with pytest.raises(ValueError, match="allow_empty"):
        w.finish()
    assert cat.calls == []


def test_a_writer_that_failed_refuses_more_work() -> None:
    cat = FakeCat(failed=True)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    with pytest.raises(BatchWriteFailedError):
        w.add_batch(*_batch(1, 1))        # sends batch 1, which the engine failed
    with pytest.raises(BatchWriteFailedError, match="earlier"):
        w.add_batch(*_batch(2, 1))
    with pytest.raises(BatchWriteFailedError, match="earlier"):
        w.finish()


# ── the fence and the run ─────────────────────────────────────────────────────


def test_begin_index_run_precedes_the_first_request_and_uses_a_generated_run_id() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 1), _batch(1, 1)):
        w.add_batch(*b)
    w.finish()
    assert cat.names()[0] == "begin_index_run"
    assert cat.of("begin_index_run")[0]["run_id"]


def test_abort_marks_the_run_failed_when_a_fence_was_begun() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    w.add_batch(*_batch(1, 1))            # sends batch 1 (fence begun)
    w.abort("boom")
    assert cat.of("fail_index_run") == [{"doc_id": _DOC, "error": "boom"}]


def test_abort_before_anything_was_sent_is_a_no_op() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    w.abort("boom")
    assert cat.calls == []


def test_the_context_manager_aborts_on_an_exception_and_lets_it_propagate() -> None:
    cat = FakeCat()
    with pytest.raises(RuntimeError, match="caller bug"):
        with _writer(cat, content_hash="hash1") as w:
            w.add_batch(*_batch(0, 1))
            w.add_batch(*_batch(1, 1))
            raise RuntimeError("caller bug")
    assert len(cat.of("fail_index_run")) == 1
    assert "RuntimeError: caller bug" in cat.of("fail_index_run")[0]["error"]


def test_abort_after_finish_does_nothing() -> None:
    """A completed document must never be marked failed by a late exception."""
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    w.add_batch(*_batch(1, 1))
    w.finish()
    w.abort("too late")
    assert cat.of("fail_index_run") == []


def test_an_exception_after_finish_inside_the_with_leaves_the_document_complete() -> None:
    cat = FakeCat()
    with pytest.raises(RuntimeError, match="after finish"):
        with _writer(cat, content_hash="hash1") as w:
            w.add_batch(*_batch(0, 1))
            w.add_batch(*_batch(1, 1))
            w.finish()
            raise RuntimeError("after finish")
    assert cat.of("fail_index_run") == []


class _Refusing:
    """complete_index_run refuses, as the engine does when the manifest does not verify."""

    def __init__(self, cat: FakeCat) -> None:
        def refuse(doc_id, content_hash, chunk_count):
            cat._rec("complete_index_run", doc_id=doc_id, content_hash=content_hash,
                     chunk_count=chunk_count)
            raise IndexRunVerifyRefused(doc_id=doc_id, referenced=9, present=8, missing=1,
                                        chunk_count=chunk_count)

        cat.complete_index_run = refuse  # type: ignore[method-assign]


def _refusals() -> list[str]:
    from nexus import mcp_infra

    return list(mcp_infra._COMPLETE_REFUSALS)


@pytest.fixture
def _clean_refusals():
    from nexus import mcp_infra

    # A CLI run's reset: clears the list AND arms recording (a disarmed process,
    # the MCP server's state, records no refusals). The autouse
    # _isolate_index_run_collectors fixture disarms again afterwards.
    mcp_infra.reset_complete_refusals()
    yield
    mcp_infra.reset_complete_refusals()


def test_a_refused_completion_stamp_is_recorded_and_the_fence_is_left_indexing(
    _clean_refusals,
) -> None:
    """The engine contract (IndexRunFenceTest): a refused /complete leaves index_state exactly as
    /begin left it. abort() must not flip that to 'failed'; the refusal goes to the collector the
    record-level summary reads."""
    cat = FakeCat()
    _Refusing(cat)
    with pytest.raises(IndexRunVerifyRefused):
        with _writer(cat, content_hash="hash1") as w:
            w.add_batch(*_batch(0, 1))
            w.add_batch(*_batch(1, 1))
            w.finish()
    assert cat.of("fail_index_run") == []
    assert _refusals() == [_DOC]


def test_a_refused_single_request_stamp_is_recorded_and_left_indexing(_clean_refusals) -> None:
    cat = FakeCat(complete_refused=True)
    with pytest.raises(IndexRunVerifyRefused):
        write_document(cat, [_batch(0, 2)], doc_id=_DOC, collection=_COLLECTION,
                       content_hash="hash1")
    assert cat.of("fail_index_run") == []
    assert _refusals() == [_DOC]


def test_a_refusal_raised_by_the_caller_inside_the_block_is_treated_the_same(_clean_refusals) -> None:
    cat = FakeCat()
    with pytest.raises(IndexRunVerifyRefused):
        with _writer(cat, content_hash="hash1") as w:
            w.add_batch(*_batch(0, 1))
            w.add_batch(*_batch(1, 1))
            raise IndexRunVerifyRefused(doc_id=_DOC, referenced=2, present=1, missing=1,
                                        chunk_count=2)
    assert cat.of("fail_index_run") == []
    assert _refusals() == [_DOC]


def test_a_failed_request_still_fails_the_fence_once_even_if_abort_is_called_again() -> None:
    cat = FakeCat()
    _Flaky(cat, "append_manifest_chunks", RuntimeError("engine said no"), times=5)
    with pytest.raises(RuntimeError):
        with _writer(cat, content_hash="hash1") as w:
            w.add_batch(*_batch(0, 1))
            w.add_batch(*_batch(1, 1))
            w.finish()
    w.abort("again")                       # the caller's except clause, after the with block
    assert len(cat.of("fail_index_run")) == 1


def test_a_caller_that_failed_the_fence_itself_can_stop_the_with_block_doing_it_again() -> None:
    cat = FakeCat()
    with pytest.raises(RuntimeError):
        with _writer(cat, content_hash="hash1") as w:
            w.add_batch(*_batch(0, 1))
            w.add_batch(*_batch(1, 1))       # sends batch 1: the fence is begun
            cat.fail_index_run(_DOC, "caller's own _fence_fail")
            w.mark_fence_handled()
            raise RuntimeError("caller failure")
    assert len(cat.of("fail_index_run")) == 1


def test_the_context_manager_does_not_abort_on_success() -> None:
    cat = FakeCat()
    with _writer(cat, content_hash="hash1") as w:
        w.add_batch(*_batch(0, 1))
        w.add_batch(*_batch(1, 1))
        w.finish()
    assert cat.of("fail_index_run") == []


# ── retries ───────────────────────────────────────────────────────────────────


class _Flaky:
    """Fails the first ``times`` calls of ``method`` with ``exc``."""

    def __init__(self, cat: FakeCat, method: str, exc: Exception, times: int = 1) -> None:
        self.attempts = 0
        real = getattr(cat, method)

        def wrapper(*a, **kw):
            self.attempts += 1
            if self.attempts <= times:
                raise exc
            return real(*a, **kw)

        setattr(cat, method, wrapper)


@pytest.mark.parametrize("method", ["begin_index_run", "append_manifest_chunks",
                                    "complete_index_run"])
def test_idempotent_requests_are_retried_on_a_connectivity_error(method) -> None:
    cat = FakeCat()
    flaky = _Flaky(cat, method, httpx.ConnectError("reset"))
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    w.add_batch(*_batch(1, 1))
    assert w.finish().completed
    assert flaky.attempts == 2


def test_a_sweep_only_append_is_retried() -> None:
    cat = FakeCat(prior=[_h(10_000 + i) for i in range(301)])
    real = cat.append_manifest_chunks
    state = {"n": 0}

    def wrapper(doc_id, rows, **kw):
        if not rows and state["n"] == 0:
            state["n"] += 1
            raise httpx.ReadError("dropped")
        return real(doc_id, rows, **kw)

    cat.append_manifest_chunks = wrapper  # type: ignore[method-assign]
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    w.add_batch(*_batch(1, 1))
    assert w.finish().completed
    assert state["n"] == 1


def test_an_embed_timeout_is_never_retried() -> None:
    cat = FakeCat()
    exc = CombinedWriteEmbedTimeoutError(collection=_COLLECTION, chunk_count=1, original="slow")
    flaky = _Flaky(cat, "append_manifest_chunks", exc, times=5)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    w.add_batch(*_batch(1, 1))
    with pytest.raises(CombinedWriteEmbedTimeoutError):
        w.finish()
    assert flaky.attempts == 1


def test_the_first_or_only_write_many_is_retried_on_a_connectivity_error() -> None:
    cat = FakeCat()
    flaky = _Flaky(cat, "write_manifest_many", httpx.ConnectError("reset"), times=1)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    assert w.finish().completed
    assert flaky.attempts == 2


def test_the_first_write_many_of_several_is_retried_and_the_sweep_survives_the_resend() -> None:
    cat = FakeCat(prior=[_h(900)])
    flaky = _Flaky(cat, "write_manifest_many", httpx.ReadError("dropped"), times=1)
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 1), _batch(1, 1)):
        w.add_batch(*b)
    w.finish()
    assert flaky.attempts == 2
    assert cat.of("append_manifest_chunks")[-1]["sweep_chashes"] == [_h(900)]


def test_a_write_many_embed_timeout_is_never_retried() -> None:
    cat = FakeCat()
    exc = CombinedWriteEmbedTimeoutError(collection=_COLLECTION, chunk_count=1, original="slow")
    flaky = _Flaky(cat, "write_manifest_many", exc, times=5)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    with pytest.raises(CombinedWriteEmbedTimeoutError):
        w.finish()
    assert flaky.attempts == 1


def _status_error(code: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://x/v1/catalog/manifest/write_many")
    return httpx.HTTPStatusError(
        str(code), request=req, response=httpx.Response(code, headers=headers or {}, request=req))


@pytest.mark.parametrize("multi", [False, True], ids=["single-request", "first-of-several"])
def test_a_429_on_the_first_request_paces_the_shared_brake_and_the_document_completes(
    monkeypatch, multi,
) -> None:
    """Cloud mode answers 429 under load. The first write_many used to bypass the retry wrapper,
    so a 429 failed the document and never told the shared RateLimitBrake to back every other
    writer off."""
    from nexus.rate_brake import get_brake

    brake = get_brake()
    trips: list[float | None] = []
    real_trip = brake.trip

    def spy(retry_after=None, *a, **kw):
        trips.append(retry_after)
        return real_trip(retry_after, *a, **kw)

    monkeypatch.setattr(brake, "trip", spy)
    cat = FakeCat()
    flaky = _Flaky(cat, "write_manifest_many", _status_error(429, {"Retry-After": "1"}), times=1)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    if multi:
        w.add_batch(*_batch(1, 1))
    res = w.finish()
    assert res.completed
    assert flaky.attempts == 2
    assert trips == [1.0]                     # the brake was engaged with the server's Retry-After


def test_a_non_rate_limit_status_error_on_the_first_request_is_not_retried() -> None:
    cat = FakeCat()
    flaky = _Flaky(cat, "write_manifest_many", _status_error(422), times=5)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    with pytest.raises(httpx.HTTPStatusError):
        w.finish()
    assert flaky.attempts == 1


def test_a_non_connectivity_error_is_not_retried() -> None:
    cat = FakeCat()
    flaky = _Flaky(cat, "complete_index_run", ValueError("bad"), times=5)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    w.add_batch(*_batch(1, 1))
    with pytest.raises(ValueError):
        w.finish()
    assert flaky.attempts == 1


# ── vectors and the convenience form ──────────────────────────────────────────


def test_embedding_model_and_force_re_embed_ride_every_chunk_carrying_request() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1", embedding_model="m", force_re_embed=True)
    for b in (_batch(0, 1), _batch(1, 1)):
        w.add_batch(*b)
    w.finish()
    wm = cat.of("write_manifest_many")[0]
    ap = cat.of("append_manifest_chunks")[0]
    assert wm["embedding_model"] == "m" and wm["force_re_embed"] is True
    assert ap["embedding_model"] == "m" and ap["force_re_embed"] is True


def test_write_document_is_the_batches_convenience_form() -> None:
    cat = FakeCat()
    res = write_document(
        cat, [_batch(0, 2), _batch(2, 2)], doc_id=_DOC, collection=_COLLECTION,
        content_hash="hash1")
    assert res.completed and cat.names()[-1] == "complete_index_run"


def test_write_document_aborts_the_fence_when_a_request_fails() -> None:
    cat = FakeCat()
    _Flaky(cat, "append_manifest_chunks", RuntimeError("engine said no"), times=5)
    with pytest.raises(RuntimeError):
        write_document(cat, [_batch(0, 2), _batch(2, 2)], doc_id=_DOC,
                       collection=_COLLECTION, content_hash="hash1")
    assert len(cat.of("fail_index_run")) == 1


# ── deferred completion (nexus-z0o2p.11: the streaming PDF pipeline stamps after its post-pass) ──


def test_deferred_completion_needs_a_content_hash() -> None:
    with pytest.raises(ValueError, match="defer_completion"):
        _writer(FakeCat(), defer_completion=True)


def test_deferred_single_request_writes_no_complete_and_stamps_only_on_complete() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1", defer_completion=True)
    w.add_batch(*_batch(0, 3))
    res = w.finish()
    assert cat.names() == ["begin_index_run", "write_manifest_many"]
    assert cat.of("write_manifest_many")[0]["complete"] in (None, {})
    assert res.completed is False, "the writer does not stamp a deferred run"
    w.complete()
    assert cat.names() == ["begin_index_run", "write_manifest_many", "complete_index_run"]
    assert cat.of("complete_index_run")[0] == {
        "doc_id": _DOC, "content_hash": "hash1", "chunk_count": 3}
    assert res.completed is True


def test_deferred_multi_request_stops_before_the_stamp_and_complete_sends_it() -> None:
    cat = FakeCat(prior=[_h(900)])
    w = _writer(cat, content_hash="hash1", defer_completion=True)
    for b in (_batch(0, 2), _batch(2, 2), _batch(4, 1)):
        w.add_batch(*b)
    w.finish()
    assert cat.names() == [
        "begin_index_run", "write_manifest_many", "append_manifest_chunks",
        "append_manifest_chunks"], "everything but the stamp, including the deferred sweep"
    assert cat.of("append_manifest_chunks")[-1]["sweep_chashes"] == [_h(900)]
    res = w.complete()
    assert cat.names()[-1] == "complete_index_run"
    assert cat.of("complete_index_run")[0]["chunk_count"] == 5
    assert res.completed


def test_complete_before_finish_and_on_a_self_stamping_writer_is_refused() -> None:
    w = _writer(FakeCat(), content_hash="hash1", defer_completion=True)
    w.add_batch(*_batch(0, 1))
    with pytest.raises(ValueError, match="finish"):
        w.complete()
    plain = _writer(FakeCat(), content_hash="hash1")
    plain.add_batch(*_batch(0, 1))
    plain.finish()
    with pytest.raises(ValueError, match="stamps itself"):
        plain.complete()


def test_complete_twice_stamps_once() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1", defer_completion=True)
    w.add_batch(*_batch(0, 2))
    w.finish()
    w.complete()
    w.complete()
    assert cat.names().count("complete_index_run") == 1


def test_a_refused_deferred_stamp_is_recorded_and_leaves_the_fence_indexing() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1", defer_completion=True)
    w.add_batch(*_batch(0, 2))
    w.finish()
    cat.manifest.pop(1)          # the engine now sees fewer rows than the run claims
    with pytest.raises(IndexRunVerifyRefused):
        w.complete()
    w.abort("after the refusal")
    assert "fail_index_run" not in cat.names(), "a refusal leaves index_state as begin left it"


def test_abort_after_a_deferred_finish_fails_the_fence_that_was_never_stamped() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1", defer_completion=True)
    w.add_batch(*_batch(0, 2))
    w.finish()
    w.abort("post-pass failed")
    assert cat.of("fail_index_run") == [{"doc_id": _DOC, "error": "post-pass failed"}]


def test_abort_after_a_deferred_stamp_does_nothing() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1", defer_completion=True)
    w.add_batch(*_batch(0, 2))
    w.finish()
    w.complete()
    w.abort("late")
    assert "fail_index_run" not in cat.names()


def test_a_repeated_chunk_keeps_the_FIRST_occurrences_metadata_in_one_request() -> None:
    """A document that repeats a chunk's text under different metadata writes one chunk; the old
    upsert and the ChunkBatcher keep the first occurrence's metadata, so the writer does too."""
    cat = FakeCat()
    w = _writer(cat, content_hash="h")
    rows = [{"chash": _h(1), "position": 0}, {"chash": _h(1), "position": 1}]
    chunks = [
        {"chash": _h(1), "text": "t", "metadata": {"which": "first"}},
        {"chash": _h(1), "text": "t", "metadata": {"which": "second"}},
    ]
    w.add_batch(rows, chunks)
    w.finish()
    sent = cat.of("write_manifest_many")[0]["chunks"]
    assert [c["metadata"]["which"] for c in sent] == ["first"]


def test_a_repeated_chunk_keeps_the_FIRST_occurrences_metadata_across_requests() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="h")
    first = {"chash": _h(1), "text": "t", "metadata": {"which": "first"}}
    later = {"chash": _h(1), "text": "t", "metadata": {"which": "later"}}
    w.add_batch([{"chash": _h(1), "position": 0}], [first, later])
    w.add_batch([{"chash": _h(2), "position": 1}], [{"chash": _h(2), "text": "u", "metadata": {}}])
    w.finish()
    sent = cat.of("write_manifest_many")[0]["chunks"]
    assert [c["metadata"]["which"] for c in sent] == ["first"]


def test_write_one_request_keeps_the_FIRST_occurrence_of_a_repeated_chunk() -> None:
    from nexus.catalog.multi_batch_write import write_one_request

    cat = FakeCat()
    chunks = [
        {"chash": _h(1), "text": "t", "metadata": {"which": "first"}},
        {"chash": _h(2), "text": "u", "metadata": {}},
        {"chash": _h(1), "text": "t", "metadata": {"which": "second"}},
    ]
    write_one_request(
        cat, doc_id=_DOC, collection=_COLLECTION,
        rows=[{"chash": _h(1), "position": 0}, {"chash": _h(2), "position": 1}], chunks=chunks)
    sent = cat.of("write_manifest_many")[0]["chunks"]
    assert [(c["chash"], c["metadata"].get("which")) for c in sent] == [(_h(1), "first"), (_h(2), None)]
