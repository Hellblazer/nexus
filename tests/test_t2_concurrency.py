# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2 concurrency tests — per-store sqlite3.Connection + lock.

These tests prove the RDR-063 Phase 2 architecture: each T2 domain store
(MemoryStore, PlanLibrary, CatalogTaxonomy, Telemetry) owns its own
``sqlite3.Connection`` and ``threading.Lock``, so writes against different
stores do not block each other, and concurrent writes within a single store
are serialized by the store's lock rather than raising
``OperationalError: database is locked``.

Complementary to ``tests/test_mcp_concurrency.py`` which covers the
multi-process (cross-nx-mcp) WAL case. This file covers the in-process
(multi-thread) case that Phase 2 is designed to make cheap.
"""
from __future__ import annotations

import statistics
import threading
import time
from pathlib import Path

from nexus.db.t2 import T2Database


# ── Cross-domain parallelism ─────────────────────────────────────────────────

def test_concurrent_domain_writes_no_contention(tmp_path: Path) -> None:
    """Memory + plans + telemetry writes on separate threads don't block.

    With Phase 1's shared connection this would have required every write to
    queue behind the single mutex. Phase 2 gives each store its own
    sqlite3.Connection so the only coordination is SQLite's WAL layer.
    """
    db_path = tmp_path / "concurrent.db"
    db = T2Database(db_path)
    try:
        n = 50
        errors: list[BaseException] = []
        timings: dict[str, float] = {}
        barrier = threading.Barrier(3)

        def write_memory() -> None:
            barrier.wait()
            start = time.perf_counter()
            try:
                for i in range(n):
                    db.put(project="conc", title=f"m{i}", content=f"memory content {i}")
            except BaseException as exc:  # pragma: no cover — failure path
                errors.append(exc)
            timings["memory"] = (time.perf_counter() - start) * 1000

        def write_plans() -> None:
            barrier.wait()
            start = time.perf_counter()
            try:
                for i in range(n):
                    db.save_plan(query=f"plan {i}", plan_json='{"step":"x"}', tags="conc")
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
            timings["plans"] = (time.perf_counter() - start) * 1000

        def write_telemetry() -> None:
            barrier.wait()
            start = time.perf_counter()
            try:
                for i in range(n):
                    db.log_relevance(
                        query=f"q{i}",
                        chunk_id=f"c{i}",
                        action="click",
                        session_id="s",
                        collection="knowledge__conc",
                    )
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
            timings["telemetry"] = (time.perf_counter() - start) * 1000

        threads = [
            threading.Thread(target=write_memory),
            threading.Thread(target=write_plans),
            threading.Thread(target=write_telemetry),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Concurrent domain writes raised: {errors}"
        # All three domains committed their writes
        assert len(db.list_entries(project="conc")) == n
        assert len(db.list_plans(limit=200)) >= n
        assert len(db.get_relevance_log(limit=200)) == n

        # Sanity: each thread finished in a reasonable wall-clock time.
        # This is not a hard performance gate — just a smoke check that no
        # single domain was starved for more than a few seconds.
        for domain, ms in timings.items():
            assert ms < 5000, f"{domain} took {ms:.1f}ms — expected < 5s"
    finally:
        db.close()


def test_concurrent_memory_put_serialized(tmp_path: Path) -> None:
    """Parallel writes against a single store are serialized by its lock.

    Multiple threads hammering ``db.put`` must all succeed — no entries lost,
    no ``OperationalError: database is locked``.
    """
    db_path = tmp_path / "single_store.db"
    db = T2Database(db_path)
    try:
        n_threads = 8
        per_thread = 25
        errors: list[BaseException] = []
        barrier = threading.Barrier(n_threads)

        def worker(tid: int) -> None:
            barrier.wait()
            try:
                for i in range(per_thread):
                    db.put(
                        project="single",
                        title=f"t{tid}-{i}",
                        content=f"thread {tid} row {i}",
                    )
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Parallel memory puts raised: {errors}"
        entries = db.list_entries(project="single")
        assert len(entries) == n_threads * per_thread
    finally:
        db.close()


# ── Single-threaded baseline ─────────────────────────────────────────────────

def test_single_threaded_memory_search_baseline(tmp_path: Path) -> None:
    """Capture p95 latency for memory_search — reference for nexus-s8o5.

    This test establishes the single-threaded baseline that the Phase 2
    review bead uses to set the ``< 1.5x baseline`` acceptance threshold.
    On failure it prints the measurement so the dev can transcribe it into
    ``nx memory`` via::

        nx memory put "p95=<measured>ms, single-threaded memory_search, \\
            n=100 queries, 200 entries" \\
            --project nexus --title rdr-063-concurrency-baseline
    """
    db_path = tmp_path / "baseline.db"
    db = T2Database(db_path)
    try:
        for i in range(200):
            db.put(
                project="bench",
                title=f"entry{i}",
                content=f"content {i} keyword lorem ipsum",
            )

        latencies: list[float] = []
        for _ in range(100):
            start = time.perf_counter()
            db.search(query="keyword", project="bench")
            latencies.append((time.perf_counter() - start) * 1000)
    finally:
        db.close()

    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[94]
    p99 = latencies[98]
    # Informational — surfaces in pytest -s output for the baseline capture.
    print(
        f"\n[rdr-063 baseline] memory_search n=100 entries=200 "
        f"p50={p50:.2f}ms p95={p95:.2f}ms p99={p99:.2f}ms"
    )
    # Sanity — generous bound. The real threshold is set in nexus-s8o5.
    assert p95 < 500, f"p95={p95:.2f}ms exceeds sanity bound (500ms)"


def test_memory_get_under_concurrent_write_load(tmp_path: Path) -> None:
    """memory.get() p95 must stay within 1.5x baseline under write load.

    Same acceptance gate as ``memory.search``: ``get()`` runs
    ``SELECT`` → ``UPDATE access_count`` → ``commit`` inside the memory
    store's lock, which the Phase 1 global mutex used to serialize
    behind all other writes. Phase 2 removes that serialization so
    ``get()``'s write leg contends with telemetry / plan writers at
    the SQLite WAL layer; the same best-effort fast-fail treatment
    applied to ``search`` also applies to ``get``. This test proves it.
    """
    db_path = tmp_path / "get_underload.db"
    db = T2Database(db_path)
    try:
        # Seed entries and remember the row ids so we can probe get(id=...)
        # directly — cheaper than get(project, title) lookups and isolates
        # the access-tracking write leg from the lookup cost.
        row_ids: list[int] = []
        for i in range(200):
            row_ids.append(
                db.put(project="load", title=f"entry{i}", content=f"content {i}")
            )

        # --- Phase A: single-threaded baseline ---
        baseline: list[float] = []
        for i in range(100):
            start = time.perf_counter()
            db.memory.get(id=row_ids[i % len(row_ids)])
            baseline.append((time.perf_counter() - start) * 1000)
        baseline.sort()
        baseline_p95 = baseline[94]

        # --- Phase B: same measurement, under concurrent write load ---
        stop_writers = threading.Event()
        writer_errors: list[BaseException] = []

        def telemetry_writer() -> None:
            i = 0
            try:
                while not stop_writers.is_set():
                    db.log_relevance(
                        query=f"q{i}",
                        chunk_id=f"c{i}",
                        action="click",
                        session_id="load",
                        collection="knowledge__load",
                    )
                    i += 1
            except BaseException as exc:  # pragma: no cover
                writer_errors.append(exc)

        def plan_writer() -> None:
            i = 0
            try:
                while not stop_writers.is_set():
                    db.save_plan(
                        query=f"plan {i}",
                        plan_json='{"step":"x"}',
                        tags="load",
                    )
                    i += 1
            except BaseException as exc:  # pragma: no cover
                writer_errors.append(exc)

        writers = [
            threading.Thread(target=telemetry_writer, daemon=True),
            threading.Thread(target=plan_writer, daemon=True),
        ]
        for t in writers:
            t.start()

        # Let writers warm up
        time.sleep(0.05)

        under_load: list[float] = []
        for i in range(100):
            start = time.perf_counter()
            db.memory.get(id=row_ids[i % len(row_ids)])
            under_load.append((time.perf_counter() - start) * 1000)

        stop_writers.set()
        for t in writers:
            t.join(timeout=5)
    finally:
        db.close()

    assert not writer_errors, f"Background writers raised: {writer_errors}"

    under_load.sort()
    load_p50 = statistics.median(under_load)
    load_p95 = under_load[94]
    load_p99 = under_load[98]
    ratio = load_p95 / baseline_p95 if baseline_p95 else float("inf")

    print(
        f"\n[rdr-063 under-load] memory_get n=100 entries=200 "
        f"baseline_p95={baseline_p95:.2f}ms "
        f"load_p50={load_p50:.2f}ms load_p95={load_p95:.2f}ms "
        f"load_p99={load_p99:.2f}ms ratio={ratio:.2f}x"
    )

    assert load_p95 < baseline_p95 * 1.5, (
        f"memory.get p95 inflated under concurrent write load: "
        f"baseline_p95={baseline_p95:.2f}ms load_p95={load_p95:.2f}ms "
        f"ratio={ratio:.2f}x (threshold 1.5x)"
    )


def test_memory_search_under_concurrent_write_load(tmp_path: Path) -> None:
    """memory_search p95 must stay within 1.5x baseline under write load.

    This is the RDR-063 Phase 2 acceptance gate (nexus-s8o5 F5): with the
    per-store connection architecture, concurrent telemetry + plan writes
    must not inflate memory_search latency by more than 50%.

    The test measures the baseline and the under-load p95 in the same
    process to eliminate platform variance — the assertion is a ratio,
    not an absolute bound.
    """
    db_path = tmp_path / "underload.db"
    db = T2Database(db_path)
    try:
        for i in range(200):
            db.put(
                project="load",
                title=f"entry{i}",
                content=f"content {i} keyword lorem ipsum",
            )

        # --- Phase A: single-threaded baseline ---
        baseline: list[float] = []
        for _ in range(100):
            start = time.perf_counter()
            db.search(query="keyword", project="load")
            baseline.append((time.perf_counter() - start) * 1000)
        baseline.sort()
        baseline_p95 = baseline[94]

        # --- Phase B: same measurement, under concurrent write load ---
        stop_writers = threading.Event()
        writer_errors: list[BaseException] = []

        def telemetry_writer() -> None:
            i = 0
            try:
                while not stop_writers.is_set():
                    db.log_relevance(
                        query=f"q{i}",
                        chunk_id=f"c{i}",
                        action="click",
                        session_id="load",
                        collection="knowledge__load",
                    )
                    i += 1
            except BaseException as exc:  # pragma: no cover
                writer_errors.append(exc)

        def plan_writer() -> None:
            i = 0
            try:
                while not stop_writers.is_set():
                    db.save_plan(
                        query=f"plan {i}",
                        plan_json='{"step":"x"}',
                        tags="load",
                    )
                    i += 1
            except BaseException as exc:  # pragma: no cover
                writer_errors.append(exc)

        writers = [
            threading.Thread(target=telemetry_writer, daemon=True),
            threading.Thread(target=plan_writer, daemon=True),
        ]
        for t in writers:
            t.start()

        # Give writers a beat to actually start hammering before we measure.
        time.sleep(0.05)

        under_load: list[float] = []
        for _ in range(100):
            start = time.perf_counter()
            db.search(query="keyword", project="load")
            under_load.append((time.perf_counter() - start) * 1000)

        stop_writers.set()
        for t in writers:
            t.join(timeout=5)
    finally:
        db.close()

    assert not writer_errors, f"Background writers raised: {writer_errors}"

    under_load.sort()
    load_p50 = statistics.median(under_load)
    load_p95 = under_load[94]
    load_p99 = under_load[98]
    ratio = load_p95 / baseline_p95 if baseline_p95 else float("inf")

    print(
        f"\n[rdr-063 under-load] memory_search n=100 entries=200 "
        f"baseline_p95={baseline_p95:.2f}ms "
        f"load_p50={load_p50:.2f}ms load_p95={load_p95:.2f}ms "
        f"load_p99={load_p99:.2f}ms ratio={ratio:.2f}x"
    )

    # The acceptance gate: <1.5x baseline.
    assert load_p95 < baseline_p95 * 1.5, (
        f"memory_search p95 inflated under concurrent write load: "
        f"baseline_p95={baseline_p95:.2f}ms load_p95={load_p95:.2f}ms "
        f"ratio={ratio:.2f}x (threshold 1.5x)"
    )
