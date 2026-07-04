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

    def flush(self, collection: str, ids: list[str], docs: list[str], metas: list[dict]) -> None:
        idx = len(self.calls)
        self.calls.append((collection, list(ids)))
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
            def flush(self, collection, ids, docs, metas):
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
            def flush(self, collection, ids, docs, metas):
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
            on_batch_complete=lambda coll, ids, docs, metas, files: batch_events.append(
                (coll, len(ids), sorted(files))
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
            def flush(self, collection, ids, docs, metas):
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
            def flush(self, collection, ids, docs, metas):
                idx = len(self.calls)
                self.calls.append((collection, list(ids)))
                if idx == 0:
                    raise RuntimeError("too big")
        rec = FailFirst()
        batch_events: list = []
        b = ChunkBatcher(
            flush=rec.flush,
            on_file_complete=rec.on_complete,
            on_batch_complete=lambda coll, ids, docs, metas, files: batch_events.append(len(ids)),
            max_chunks=10,
        )
        b.add("a.py", "code__x", *_mk(4, "a"))
        b.add("b.py", "code__x", *_mk(4, "b"))
        b.drain()  # 8-chunk batch fails once, bisects to 4+4
        assert batch_events == [4, 4]
