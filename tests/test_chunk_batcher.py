# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nexus.chunk_batcher (nexus-f55fu, duoak Phase 2C.1).

Cross-file chunk batching: files feed chunks into a per-collection
accumulator; flushes fire at the chunk cap (service limit 300) or byte
ceiling or drain; each batch carries file attribution so a failed flush
fails exactly the contributing files, not the run.
"""

from __future__ import annotations

import threading

import pytest

from nexus.chunk_batcher import ChunkBatcher


def _mk(n: int, prefix: str = "c") -> tuple[list[str], list[str], list[dict]]:
    ids = [f"{prefix}{i:04d}" for i in range(n)]
    docs = [f"text-{prefix}{i}" for i in range(n)]
    metas = [{"i": i} for i in range(n)]
    return ids, docs, metas


class Recorder:
    def __init__(self, fail_batches: set[int] | None = None) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.fail_batches = fail_batches or set()
        self.completed: list[str] = []
        self.contexts: list[tuple[str, object]] = []
        self.failed: list[tuple[str, str]] = []
        #: nexus-wxjr6: file_contexts as received by flush(), one entry
        #: per flush call — lets tests assert it matches what
        #: on_batch_begin/on_batch_complete see for the SAME flush.
        self.flush_file_contexts: list[list] = []

    def flush(self, collection: str, ids: list[str], docs: list[str], metas: list[dict], file_contexts: list | None = None) -> None:
        idx = len(self.calls)
        self.calls.append((collection, list(ids)))
        self.flush_file_contexts.append(list(file_contexts or []))
        if idx in self.fail_batches:
            raise RuntimeError(f"flush {idx} boom")

    def on_complete(self, path: str, context: object = None) -> None:
        self.completed.append(path)
        self.contexts.append((path, context))

    def on_failed(self, path: str, error: str, context: object = None) -> None:
        self.failed.append((path, error))


def _batcher(rec: Recorder, **kw) -> ChunkBatcher:
    return ChunkBatcher(
        flush=rec.flush,
        on_file_complete=rec.on_complete,
        on_file_failed=rec.on_failed,
        **kw,
    )


class TestFlushTriggers:
    def test_no_flush_below_cap(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=300)
        ids, docs, metas = _mk(299)
        b.add("a.py", "code__x", ids, docs, metas)
        assert rec.calls == []

    def test_flush_at_exactly_cap(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=300)
        ids, docs, metas = _mk(300)
        b.add("a.py", "code__x", ids, docs, metas)
        assert len(rec.calls) == 1
        assert len(rec.calls[0][1]) == 300

    def test_oversize_single_file_refused(self) -> None:
        # File-atomicity (review Critical): a file bigger than one batch
        # cannot be atomic -> add() refuses; caller uses the legacy path.
        rec = Recorder()
        b = _batcher(rec, max_chunks=300)
        ids, docs, metas = _mk(650)
        assert b.add("big.py", "code__x", ids, docs, metas) is False
        b.drain()
        assert rec.calls == []
        assert rec.completed == []

    def test_byte_ceiling_triggers_flush(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=300, max_bytes=100)
        b.add("a.py", "code__x", ["i1"], ["x" * 80], [{}])
        assert rec.calls == []
        b.add("b.py", "code__x", ["i2"], ["y" * 80], [{}])
        # second add crosses 100 bytes -> accumulated batch flushes
        assert len(rec.calls) == 1

    def test_drain_flushes_partials_all_collections(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=300)
        b.add("a.py", "code__x", *_mk(3, "a"))
        b.add("d.md", "docs__x", *_mk(2, "d"))
        b.drain()
        assert sorted(c[0] for c in rec.calls) == ["code__x", "docs__x"]

    def test_collections_do_not_mix(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=5)
        b.add("a.py", "code__x", *_mk(3, "a"))
        b.add("d.md", "docs__x", *_mk(3, "d"))
        b.drain()
        for coll, ids in rec.calls:
            prefix = "a" if coll == "code__x" else "d"
            assert all(i.startswith(prefix) for i in ids)


class TestFlushReceivesFileContexts:
    """nexus-wxjr6: flush() now receives file_contexts as its 5th arg —
    the SAME (path, context) pairs on_batch_begin/on_batch_complete
    already see, threaded into the flush closure itself so a
    combined-write flush fn can build per-doc manifest rows in the same
    call that uploads chunk content (design memo §2 client-side flush
    restructure)."""

    def test_flush_file_contexts_matches_on_batch_begin_and_complete(self) -> None:
        begin_seen: list = []
        complete_seen: list = []
        rec = Recorder()
        b = ChunkBatcher(
            flush=rec.flush,
            on_file_complete=rec.on_complete,
            on_batch_begin=lambda coll, fcs: begin_seen.append(list(fcs)),
            on_batch_complete=lambda coll, ids, docs, metas, fcs: complete_seen.append(list(fcs)),
            max_chunks=10,
        )
        b.add("a.py", "code__x", *_mk(4, "a"), context={"catalog_doc_id": "1.1"})
        b.add("b.py", "code__x", *_mk(4, "b"), context={"catalog_doc_id": "1.2"})
        b.drain()
        assert len(rec.flush_file_contexts) == 1
        flushed = rec.flush_file_contexts[0]
        paths = sorted(p for p, _ in flushed)
        assert paths == ["a.py", "b.py"]
        contexts = {p: c for p, c in flushed}
        assert contexts["a.py"] == {"catalog_doc_id": "1.1"}
        assert contexts["b.py"] == {"catalog_doc_id": "1.2"}
        # Same shape (not necessarily same list identity) as the
        # begin/complete callbacks for this exact flush.
        assert sorted(p for p, _ in begin_seen[0]) == paths
        assert sorted(p for p, _ in complete_seen[0]) == paths

    def test_empty_file_contexts_when_no_context_supplied(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=5)
        b.add("a.py", "code__x", *_mk(5, "a"))  # no context= kwarg
        b.drain()
        assert len(rec.flush_file_contexts) == 1
        # file_contexts is still populated (path, None) — add() always
        # records SOME context (default None), never drops the file
        # entirely from the list.
        assert [p for p, _ in rec.flush_file_contexts[0]] == ["a.py"]


class TestCompletionAttribution:
    def test_file_completes_when_last_batch_lands(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=10)
        b.add("a.py", "code__x", *_mk(8, "a"))
        assert rec.completed == []  # still buffered
        b.add("b.py", "code__x", *_mk(8, "b"))  # 16 > 10 -> flush(es)
        b.drain()
        assert sorted(rec.completed) == ["a.py", "b.py"]
        assert rec.failed == []

    def test_file_never_straddles_batches(self) -> None:
        # File-atomicity: adding a file that will not fit pre-flushes the
        # buffer, so every file's chunks travel in exactly one batch.
        rec = Recorder()
        b = _batcher(rec, max_chunks=10)
        b.add("a.py", "code__x", *_mk(7, "a"))
        b.add("b.py", "code__x", *_mk(6, "b"))  # 7+6>10 -> pre-flush [a]
        b.drain()
        assert [len(ids) for _, ids in rec.calls] == [7, 6]
        for _, ids in rec.calls:
            prefixes = {i[0] for i in ids}
            assert len(prefixes) == 1  # one file per batch here

    def test_transient_batch_failure_heals_via_bisection(self) -> None:
        # A batch that fails once (transient) bisects; the halves succeed
        # and every file completes -- no false failures from one blip.
        rec = Recorder(fail_batches={0})
        b = _batcher(rec, max_chunks=5)
        b.add("f1.py", "code__x", *_mk(3, "x"))
        b.add("f2.py", "code__x", *_mk(2, "y"))  # fills batch 0 -> fails once
        b.add("good.py", "code__x", *_mk(3, "g"))
        b.drain()
        assert rec.failed == []
        assert sorted(rec.completed) == ["f1.py", "f2.py", "good.py"]

    def test_persistently_failing_single_file_batch_fails_that_file(self) -> None:
        class AlwaysFail(Recorder):
            def flush(self, collection, ids, docs, metas, file_contexts=None):
                self.calls.append((collection, list(ids)))
                raise RuntimeError("boom")
        rec = AlwaysFail()
        b = _batcher(rec, max_chunks=5)
        b.add("only.py", "code__x", *_mk(3, "x"))
        b.drain()
        assert [p for p, _ in rec.failed] == ["only.py"]
        assert "boom" in rec.failed[0][1]
        assert rec.completed == []

    def test_failed_batch_bisects_to_isolate_bad_files(self) -> None:
        # Bisection: a failed multi-file batch splits by files and each
        # half retries -- a too-big-for-gateway batch self-tunes down,
        # and a genuinely poisoned file is isolated to itself.
        class BisectRec(Recorder):
            def flush(self, collection, ids, docs, metas, file_contexts=None):
                self.calls.append((collection, list(ids)))
                if any(i.startswith("bad") for i in ids):
                    raise RuntimeError("poison")
        rec = BisectRec()
        b = _batcher(rec, max_chunks=10)
        b.add("f1.py", "code__x", *_mk(3, "aa"))
        b.add("f2.py", "code__x", *_mk(3, "bad"))
        b.add("f3.py", "code__x", *_mk(3, "cc"))
        b.drain()
        # only f2 fails; f1/f3 complete via bisected sub-batches
        assert [p for p, _ in rec.failed] == ["f2.py"]
        assert sorted(rec.completed) == ["f1.py", "f3.py"]

    def test_failed_files_property(self) -> None:
        rec = Recorder(fail_batches={0})
        b = _batcher(rec, max_chunks=2)
        b.add("f.py", "code__x", *_mk(2, "f"))
        b.drain()
        assert set(b.failed_files) == {"f.py"}


class TestThreadSafety:
    def test_concurrent_adds_lose_nothing(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=50)
        n_threads, files_per, chunks_per = 8, 20, 7

        def worker(t: int) -> None:
            for f in range(files_per):
                b.add(f"t{t}f{f}.py", "code__x", *_mk(chunks_per, f"t{t}f{f}-"))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        b.drain()
        total = sum(len(ids) for _, ids in rec.calls)
        assert total == n_threads * files_per * chunks_per
        assert len(rec.completed) == n_threads * files_per
        all_ids = [i for _, ids in rec.calls for i in ids]
        assert len(all_ids) == len(set(all_ids))


class TestValidation:
    def test_mismatched_lengths_raise(self) -> None:
        rec = Recorder()
        b = _batcher(rec)
        with pytest.raises(ValueError, match="length"):
            b.add("a.py", "code__x", ["i1", "i2"], ["only-one"], [{}, {}])

    def test_empty_add_is_noop(self) -> None:
        rec = Recorder()
        b = _batcher(rec)
        b.add("a.py", "code__x", [], [], [])
        b.drain()
        assert rec.calls == []
        # zero-chunk file still completes (nothing to upload)
        assert rec.completed == ["a.py"]


class TestCompletionContext:
    def test_context_passed_through_to_completion(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=300)
        payload = {"hook_args": ("ids", "docs")}
        b.add("a.py", "code__x", *_mk(3, "a"), context=payload)
        b.drain()
        assert rec.contexts == [("a.py", payload)]

    def test_context_none_by_default(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=300)
        b.add("a.py", "code__x", *_mk(2, "a"))
        b.drain()
        assert rec.contexts == [("a.py", None)]


class TestDoubleAddGuard:
    def test_re_adding_unsettled_file_raises(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=300)
        b.add("a.py", "code__x", *_mk(3, "a"))
        with pytest.raises(ValueError, match="staged twice"):
            b.add("a.py", "code__x", *_mk(2, "b"))

    def test_re_adding_after_settle_is_fine(self) -> None:
        # A file that fully settled (flushed) may legitimately be staged
        # again (e.g. a future retry path re-indexing the file).
        rec = Recorder()
        b = _batcher(rec, max_chunks=3)
        b.add("a.py", "code__x", *_mk(3, "a"))  # exactly cap -> flushed
        assert rec.completed == ["a.py"]
        b.add("a.py", "code__x", *_mk(2, "c"))
        b.drain()
        assert rec.completed == ["a.py", "a.py"]


class TestPerCollectionCap:
    def test_callable_cap_applies_per_collection(self) -> None:
        rec = Recorder()
        b = ChunkBatcher(
            flush=rec.flush,
            on_file_complete=rec.on_complete,
            on_file_failed=rec.on_failed,
            max_chunks=lambda coll: 4 if coll.startswith("docs__") else 300,
        )
        b.add("d1.md", "docs__x", *_mk(3, "d"))
        b.add("d2.md", "docs__x", *_mk(2, "e"))  # 3+2>4 -> pre-flush
        b.add("c1.py", "code__x", *_mk(200, "c"))  # under 300, buffered
        b.drain()
        docs_batches = [ids for coll, ids in rec.calls if coll == "docs__x"]
        assert [len(x) for x in docs_batches] == [3, 2]
        code_batches = [ids for coll, ids in rec.calls if coll == "code__x"]
        assert [len(x) for x in code_batches] == [200]

    def test_oversize_relative_to_collection_cap_refused(self) -> None:
        rec = Recorder()
        b = ChunkBatcher(
            flush=rec.flush,
            max_chunks=lambda coll: 4 if coll.startswith("docs__") else 300,
        )
        assert b.add("d.md", "docs__x", *_mk(5, "d")) is False
        assert b.add("c.py", "code__x", *_mk(5, "c")) is True


class TestBatchCompleteCallback:
    def test_fires_once_per_successful_flush_with_aggregate(self) -> None:
        rec = Recorder()
        batch_events: list[tuple[str, int, int]] = []
        b = ChunkBatcher(
            flush=rec.flush,
            on_file_complete=rec.on_complete,
            on_batch_complete=lambda coll, ids, docs, metas, fcs: batch_events.append(
                (coll, len(ids), sorted(p for p, _ in fcs))
            ),
            max_chunks=10,
        )
        b.add("a.py", "code__x", *_mk(6, "a"))
        b.add("b.py", "code__x", *_mk(6, "b"))  # pre-flush [a]
        b.drain()
        assert batch_events == [
            ("code__x", 6, ["a.py"]),
            ("code__x", 6, ["b.py"]),
        ]

    def test_not_fired_for_failed_flush(self) -> None:
        class AlwaysFail(Recorder):
            def flush(self, collection, ids, docs, metas, file_contexts=None):
                self.calls.append((collection, list(ids)))
                raise RuntimeError("boom")
        rec = AlwaysFail()
        batch_events: list = []
        b = ChunkBatcher(
            flush=rec.flush,
            on_file_failed=rec.on_failed,
            on_batch_complete=lambda *a: batch_events.append(a),
            max_chunks=10,
        )
        b.add("f.py", "code__x", *_mk(3, "f"))
        b.drain()
        assert batch_events == []

    def test_bisected_halves_fire_batch_complete_each(self) -> None:
        class FailFirst(Recorder):
            def flush(self, collection, ids, docs, metas, file_contexts=None):
                idx = len(self.calls)
                self.calls.append((collection, list(ids)))
                if idx == 0:
                    raise RuntimeError("too big")
        rec = FailFirst()
        batch_events: list = []
        b = ChunkBatcher(
            flush=rec.flush,
            on_file_complete=rec.on_complete,
            on_batch_complete=lambda coll, ids, docs, metas, fcs: batch_events.append(len(ids)),
            max_chunks=10,
        )
        b.add("a.py", "code__x", *_mk(4, "a"))
        b.add("b.py", "code__x", *_mk(4, "b"))
        b.drain()  # 8-chunk batch fails once, bisects to 4+4
        assert batch_events == [4, 4]


class TestConcurrentFlushes:
    def test_drain_overlaps_flushes_up_to_limit(self) -> None:
        # duoak follow-up: flushes dispatch to a small pool so drain()
        # (and staging workers) don't serialize the network calls.
        import threading as _t
        import time as _time
        active = []
        peak = [0]
        lock = _t.Lock()

        def slow_flush(collection, ids, docs, metas, file_contexts=None):
            with lock:
                active.append(1)
                peak[0] = max(peak[0], len(active))
            _time.sleep(0.05)
            with lock:
                active.pop()

        b = ChunkBatcher(flush=slow_flush, max_chunks=5, flush_concurrency=3)
        for i in range(6):  # 6 full batches across 6 files
            b.add(f"f{i}.py", "code__x", *_mk(5, f"f{i}-"))
        b.drain()
        assert peak[0] >= 2  # overlapped
        assert peak[0] <= 3  # bounded

    def test_drain_waits_for_all_callbacks(self) -> None:
        import time as _time
        completed: list[str] = []

        def slow_flush(collection, ids, docs, metas, file_contexts=None):
            _time.sleep(0.02)

        b = ChunkBatcher(
            flush=slow_flush,
            on_file_complete=lambda p, c=None: completed.append(p),
            max_chunks=5,
            flush_concurrency=3,
        )
        for i in range(5):
            b.add(f"f{i}.py", "code__x", *_mk(5, f"f{i}-"))
        b.drain()
        assert sorted(completed) == [f"f{i}.py" for i in range(5)]

    def test_default_concurrency_is_sequential_compat(self) -> None:
        # flush_concurrency=1 (or omitted) keeps the v1 synchronous shape.
        rec = Recorder()
        b = _batcher(rec, max_chunks=5)
        b.add("a.py", "code__x", *_mk(5, "a"))
        assert len(rec.calls) == 1  # flushed synchronously at cap


class TestDrainProgress:
    """nexus-uizok: drain(on_progress) — the operator heartbeat for the
    end-of-run flush that previously ran dark for minutes."""

    def test_sync_drain_reports_each_flush(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=100)
        b.add("a.py", "code__x", *_mk(3, "a"))
        b.add("b.py", "docs__x", *_mk(2, "b"))
        progress: list[tuple[int, int]] = []
        b.drain(on_progress=lambda done, total: progress.append((done, total)))
        assert progress == [(1, 2), (2, 2)]

    def test_pooled_drain_reports_every_flush_including_in_flight(self) -> None:
        # Gate keeps the 3 overflow flushes genuinely outstanding at drain
        # time (deterministic — settled work is deliberately NOT counted).
        import threading as _t

        gate = _t.Event()

        def gated_flush(collection, ids, docs, metas, file_contexts=None):
            gate.wait(5.0)

        b = ChunkBatcher(flush=gated_flush, max_chunks=5, flush_concurrency=3)
        # 3 full batches dispatched by add() overflows, all blocked on the
        # gate (pool has exactly 3 workers) + 1 pending buffer.
        for i in range(3):
            b.add(f"f{i}.py", "code__x", *_mk(5, f"f{i}-"))
        b.add("tail.py", "code__x", *_mk(2, "t"))
        progress: list[tuple[int, int]] = []
        _t.Timer(0.05, gate.set).start()
        b.drain(on_progress=lambda done, total: progress.append((done, total)))
        total = progress[0][1]
        assert total == 4, "3 outstanding overflow flushes + 1 drained pending buffer"
        assert [p[0] for p in progress] == [1, 2, 3, 4]
        assert all(p[1] == total for p in progress)

    def test_drain_without_callback_unchanged(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=100)
        b.add("a.py", "code__x", *_mk(3, "a"))
        b.drain()
        assert len(rec.calls) == 1

    def test_in_flight_reflects_outstanding_not_dispatched_total(self) -> None:
        # nexus-uizok critique HIGH-2: _futures used to accumulate every
        # pool-dispatched flush for the run, so "in_flight" meant "every
        # flush ever dispatched". Settled futures are now pruned at
        # dispatch time and in_flight counts not-done only.
        import time as _time

        def quick_flush(collection, ids, docs, metas, file_contexts=None):
            pass

        b = ChunkBatcher(flush=quick_flush, max_chunks=5, flush_concurrency=3)
        # Many overflow dispatches, all long-settled by the time we look.
        for i in range(12):
            b.add(f"f{i}.py", "code__x", *_mk(5, f"f{i}-"))
        deadline = _time.monotonic() + 5.0
        while b.pending_summary["in_flight"] > 1 and _time.monotonic() < deadline:
            _time.sleep(0.01)
        s = b.pending_summary
        assert s["in_flight"] <= 1, (
            f"in_flight={s['in_flight']} — settled futures not pruned"
        )
        # And drain's total reflects real remaining work, not 12.
        progress: list[tuple[int, int]] = []
        b.drain(on_progress=lambda done, total: progress.append((done, total)))
        if progress:
            assert progress[0][1] <= 3, "drain total must not count settled work"

    def test_drain_returns_flush_count(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=100)
        b.add("a.py", "code__x", *_mk(3, "a"))
        b.add("b.py", "docs__x", *_mk(2, "b"))
        assert b.drain() == 2
        assert b.drain() == 0  # idempotent, nothing left

    def test_pending_summary_counts(self) -> None:
        rec = Recorder()
        b = _batcher(rec, max_chunks=100)
        assert b.pending_summary == {"chunks": 0, "collections": 0, "in_flight": 0}
        b.add("a.py", "code__x", *_mk(3, "a"))
        b.add("b.py", "docs__x", *_mk(2, "b"))
        s = b.pending_summary
        assert s["chunks"] == 5 and s["collections"] == 2 and s["in_flight"] == 0
        b.drain()
        assert b.pending_summary == {"chunks": 0, "collections": 0, "in_flight": 0}


class TestFlushInstrumentation:
    """nexus-lde88 G1/G3: complete per-flush attribution.

    G1 — one ``chunk_flush_complete`` structlog event per flush with
    {collection, chunks, files, upload_s, flush_hook_s, settle_s,
    file_hook_s}. G3 — ``upload_s`` is the network write ALONE;
    ``stats()``'s ``flush_seconds`` now covers the FULL flush wall
    (upload + both hook chains + settle), not just the upload leg it
    used to silently stop short of. Review round 2 added ``settle_s``
    (the file-settlement bookkeeping block) after the critic proved a
    0.03s delay injected there vanished from every reported number.
    """

    @pytest.fixture(autouse=True)
    def _info_level(self) -> None:
        # The event is logged at INFO; the suite default structlog filter
        # is WARNING (tests/conftest.py::pytest_configure), which would
        # drop it before capture_logs() ever sees it. The suite's own
        # _restore_structlog_after_test autouse fixture restores the
        # saved config after this test regardless.
        import logging

        import structlog

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))

    def test_chunk_flush_complete_event_fields(self) -> None:
        import time as _time

        import structlog.testing

        rec = Recorder()

        def slow_flush(collection, ids, docs, metas, file_contexts=None):
            _time.sleep(0.02)

        b = ChunkBatcher(
            flush=slow_flush,
            on_file_complete=rec.on_complete,
            on_file_failed=rec.on_failed,
            on_batch_complete=lambda *a: _time.sleep(0.01),
            max_chunks=100,
        )
        with structlog.testing.capture_logs() as logs:
            b.add("a.py", "code__x", *_mk(3, "a"))
            b.drain()

        events = [l for l in logs if l["event"] == "chunk_flush_complete"]
        assert len(events) == 1
        e = events[0]
        assert e["collection"] == "code__x"
        assert e["chunks"] == 3
        assert e["files"] == 1
        # Non-cumulative, genuinely measured: the injected sleeps must show
        # up as distinct, correctly-ordered magnitudes, not zeros or one
        # merged number.
        assert e["upload_s"] >= 0.02
        assert e["flush_hook_s"] >= 0.01
        assert e["settle_s"] >= 0.0
        assert e["file_hook_s"] >= 0.0

    def test_upload_s_excludes_hook_time_g3(self) -> None:
        """G3: upload_s must reflect ONLY the network write, not balloon to
        include the flush-grain hook's sleep — the two are reported
        separately, not conflated into one under-attributed number."""
        import time as _time

        import structlog.testing

        rec = Recorder()

        def quick_flush(collection, ids, docs, metas, file_contexts=None):
            pass

        b = ChunkBatcher(
            flush=quick_flush,
            on_file_complete=rec.on_complete,
            on_file_failed=rec.on_failed,
            on_batch_complete=lambda *a: _time.sleep(0.05),
            max_chunks=100,
        )
        with structlog.testing.capture_logs() as logs:
            b.add("a.py", "code__x", *_mk(2, "a"))
            b.drain()

        e = next(l for l in logs if l["event"] == "chunk_flush_complete")
        assert e["upload_s"] < 0.03, (
            f"upload_s={e['upload_s']} leaked hook time (hook slept 0.05s)"
        )
        assert e["flush_hook_s"] >= 0.05

    def test_stats_flush_seconds_covers_full_wall_g3(self) -> None:
        """stats()['flush_seconds'] used to stop before the hooks
        (chunk_batcher.py:399 pre-fix), under-reporting flush cost by
        ~4x. It must now be >= upload_seconds + the hook sleep injected
        below — the full flush wall, not just the upload leg."""
        import time as _time

        rec = Recorder()

        def quick_flush(collection, ids, docs, metas, file_contexts=None):
            pass

        b = ChunkBatcher(
            flush=quick_flush,
            on_file_complete=rec.on_complete,
            on_file_failed=rec.on_failed,
            on_batch_complete=lambda *a: _time.sleep(0.05),
            max_chunks=100,
        )
        b.add("a.py", "code__x", *_mk(2, "a"))
        b.drain()
        s = b.stats
        assert s["upload_seconds"] < 0.03
        assert s["flush_seconds"] >= 0.05
        assert s["flush_seconds"] > s["upload_seconds"]
        assert s["settle_seconds"] >= 0.0

    def test_no_flush_complete_event_on_bisect_path(self) -> None:
        """A batch that fails and bisects never completes AS ITSELF — no
        event fires for it; each successfully-flushed half fires its own
        event when it completes."""
        import structlog.testing

        calls = {"n": 0}

        def flaky_flush(collection, ids, docs, metas, file_contexts=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")

        rec = Recorder()
        b = ChunkBatcher(
            flush=flaky_flush,
            on_file_complete=rec.on_complete,
            on_file_failed=rec.on_failed,
            max_chunks=100,
        )
        b.add("a.py", "code__x", *_mk(2, "a"))
        b.add("b.py", "code__x", *_mk(2, "b"))
        with structlog.testing.capture_logs() as logs:
            b.drain()
        events = [l for l in logs if l["event"] == "chunk_flush_complete"]
        # The original 4-chunk/2-file batch bisects into two 1-file halves;
        # each half flushes successfully and fires its own event — 2 total,
        # never one for the batch that actually raised.
        assert len(events) == 2
        assert all(ev["files"] == 1 for ev in events)

    def test_settle_s_captures_settle_bookkeeping_delay(self) -> None:
        """Review round 2 (both reviewers): the critic proved a 0.03s delay
        injected into the settle-bookkeeping block (_settle_file_locked,
        called under self._lock between the flush-grain hooks and the
        per-file callbacks) was invisible in upload_s/flush_hook_s/
        file_hook_s AND in stats()['flush_seconds'] — reported sum 0.0000s
        against a real wall of 0.0401s. settle_s must now capture it."""
        import time as _time
        import unittest.mock as mock

        import structlog.testing

        from nexus.chunk_batcher import ChunkBatcher as _CB

        rec = Recorder()

        def quick_flush(collection, ids, docs, metas, file_contexts=None):
            pass

        orig_settle = _CB._settle_file_locked

        def slow_settle(self, path, settled):
            _time.sleep(0.03)
            return orig_settle(self, path, settled)

        b = ChunkBatcher(
            flush=quick_flush,
            on_file_complete=rec.on_complete,
            on_file_failed=rec.on_failed,
            max_chunks=100,
        )
        with mock.patch.object(_CB, "_settle_file_locked", slow_settle):
            with structlog.testing.capture_logs() as logs:
                b.add("a.py", "code__x", *_mk(2, "a"))
                b.drain()

        e = next(l for l in logs if l["event"] == "chunk_flush_complete")
        assert e["settle_s"] >= 0.03, (
            f"settle_s={e['settle_s']} — the injected delay vanished again"
        )
        s = b.stats
        assert s["settle_seconds"] >= 0.03
        assert s["flush_seconds"] >= 0.03

    def test_partition_sums_to_wall_within_epsilon(self) -> None:
        """The exact test the critic's probe sketches: inject a distinct,
        recognizable delay into EACH of the attributed phases
        (begin_hook, upload, flush_hook, settle, file_hook) and assert
        they reconstruct the real wall clock of the flush within a small
        epsilon — the partition-completeness check that would have caught
        the original G3 gap, this round's settle-bookkeeping recurrence,
        and (nexus-jb4pp) the on_batch_begin gap that vw594 F1 opened by
        inserting a round trip ahead of the upload timer. That last one
        slipped through precisely because this test left on_batch_begin
        at its no-op default, so the missing phase cost 0.0s here while
        costing ~0.4s per flush in production."""
        import time as _time
        import unittest.mock as mock

        import structlog.testing

        from nexus.chunk_batcher import ChunkBatcher as _CB

        rec = Recorder()
        DELAY = 0.03

        def slow_flush(collection, ids, docs, metas, file_contexts=None):
            _time.sleep(DELAY)

        def slow_on_complete(path, context=None):
            _time.sleep(DELAY)
            rec.on_complete(path, context)

        orig_settle = _CB._settle_file_locked

        def slow_settle(self, path, settled):
            _time.sleep(DELAY)
            return orig_settle(self, path, settled)

        b = ChunkBatcher(
            flush=slow_flush,
            on_file_complete=slow_on_complete,
            on_file_failed=rec.on_failed,
            on_batch_complete=lambda *a: _time.sleep(DELAY),
            on_batch_begin=lambda *a: _time.sleep(DELAY),
            max_chunks=100,
        )
        b.add("a.py", "code__x", *_mk(2, "a"))  # single file, single flush

        with mock.patch.object(_CB, "_settle_file_locked", slow_settle):
            with structlog.testing.capture_logs() as logs:
                wall_t0 = _time.monotonic()
                b.drain()
                wall = _time.monotonic() - wall_t0

        e = next(l for l in logs if l["event"] == "chunk_flush_complete")
        phases = ("begin_hook_s", "upload_s", "flush_hook_s", "settle_s",
                  "file_hook_s")
        total = sum(e[k] for k in phases)
        # Each phase carries its own DELAY; a real attribution gap would
        # show up as total < wall by roughly one DELAY (0.03s) or more —
        # epsilon well under that so a reintroduced gap still fails loud.
        assert total == pytest.approx(wall, abs=0.05), (
            f"total={total} wall={wall} — a phase is unattributed again"
        )
        assert min(e[k] for k in phases) >= DELAY * 0.5
        # ...and the batcher's own cumulative counter must agree, so the
        # end-of-run report cannot drift from the per-flush event.
        assert b.stats["flush_seconds"] == pytest.approx(wall, abs=0.05)
        # abs=1e-3: the event rounds to 3dp, the counter does not.
        assert b.stats["begin_hook_seconds"] == pytest.approx(e["begin_hook_s"], abs=1e-3)
