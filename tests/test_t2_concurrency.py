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

import os
import statistics
import threading
import time
from pathlib import Path

import numpy as np

from nexus.db.t2 import T2Database
from tests._t2_fixture_ops import canonical_chunk_id as _cid
from tests.conftest import make_vector_test_client
import pytest

pytestmark = pytest.mark.t2_tenant


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
                    db.save_plan(query=f"plan {i}", plan_json='{"step":"x"}', tags="conc", verb="query")
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
                        chunk_id=_cid(f"c{i}"),
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


def test_memory_search_under_discover_topics_load(tmp_path: Path) -> None:
    """memory.search median must stay within 10x baseline during discover_topics.

    RDR-063 Success Criterion 2c (updated for RDR-070): ``discover_topics``
    does not block ``memory_search`` for its duration. The new sklearn
    HDBSCAN pipeline holds only ``taxonomy._lock`` (for topic/assignment
    INSERTs) and never acquires ``memory._lock``, so contention is
    strictly less than the old ``cluster_and_persist`` which had a
    Phase A ``memory._lock`` acquisition.

    Ratio gate: 10.0x on **median** (not p95).  The gate was previously
    7.0x on p95, which caused intermittent CI failures on GHA runners
    (Python 3.12) when OS scheduling noise inflated a single baseline
    sample and tightened the ratio to within rounding error of the
    threshold (e.g. 7.18x with baseline_p95=1.62ms, nexus-9lzx).

    Switching to median eliminates high-variance tail sensitivity: with
    n=200 samples the median is stable to ±0.1ms even on noisy runners,
    while a real lock regression (``memory._lock`` acquired by the
    taxonomy path) would inflate the median by 50-100x, well above 10x.
    The p95 figures are still printed for diagnostic reference.
    """

    db_path = tmp_path / "discover_underload.db"
    db = T2Database(db_path)
    chroma_client = make_vector_test_client()
    try:
        # Seed memory entries for the search baseline
        vocab_words = (
            "alpha beta gamma delta epsilon zeta eta theta iota kappa "
            "lambda mu nu xi omicron pi rho sigma tau upsilon phi chi "
            "psi omega keyword pattern signal vector matrix cluster"
        ).split()
        for i in range(300):
            picks = " ".join(vocab_words[j % len(vocab_words)] for j in range(i, i + 5))
            db.put(
                project="cluster_load",
                title=f"entry{i}",
                content=f"content {i} {picks}",
            )

        # Pre-compute embeddings for discover_topics
        rng = np.random.default_rng(42)
        n_docs = 300
        embeddings = rng.standard_normal((n_docs, 384)).astype(np.float32) * 0.1
        # Create 3 separated clusters
        embeddings[:100, 0] += 3.0
        embeddings[100:200, 1] += 3.0
        embeddings[200:, 2] += 3.0
        doc_ids = [f"entry{i}" for i in range(n_docs)]
        texts = [f"content {i} {' '.join(vocab_words[j % len(vocab_words)] for j in range(i, i + 5))}" for i in range(n_docs)]

        n_samples = 200

        # --- Phase A: single-threaded baseline ---
        baseline: list[float] = []
        for _ in range(n_samples):
            start = time.perf_counter()
            db.search(query="keyword", project="cluster_load")
            baseline.append((time.perf_counter() - start) * 1000)
        baseline.sort()
        baseline_p95 = baseline[int(n_samples * 0.95) - 1]

        # --- Phase B: same measurement, with discover_topics running ---
        stop_worker = threading.Event()
        worker_started = threading.Event()
        worker_errors: list[BaseException] = []
        discover_iterations = {"n": 0}

        def discover_worker() -> None:
            try:
                worker_started.set()
                while not stop_worker.is_set():
                    db.taxonomy.rebuild_taxonomy(
                        "cluster_load", doc_ids, embeddings, texts, chroma_client,
                    )
                    discover_iterations["n"] += 1
            except BaseException as exc:  # pragma: no cover — failure path
                worker_errors.append(exc)

        worker = threading.Thread(target=discover_worker, daemon=True)
        worker.start()

        # nexus-8g79.26: wait until the worker thread is actually
        # scheduled before we start measuring under-load latency. Pre-fix
        # the test slept 50ms unconditionally which produced spurious
        # failures on loaded CI runners when the worker hadn't started
        # its first iteration yet.
        assert worker_started.wait(timeout=5.0), (
            "discover_worker thread did not start within 5s"
        )

        under_load: list[float] = []
        for _ in range(n_samples):
            start = time.perf_counter()
            db.search(query="keyword", project="cluster_load")
            under_load.append((time.perf_counter() - start) * 1000)

        stop_worker.set()
        worker.join(timeout=30)
    finally:
        db.close()

    assert not worker_errors, f"discover_topics raised: {worker_errors}"
    assert discover_iterations["n"] >= 1, (
        "Background worker never completed a discover_topics run — "
        "the test did not exercise the under-load path"
    )

    under_load.sort()
    load_p50 = statistics.median(under_load)
    load_p95 = under_load[int(n_samples * 0.95) - 1]
    load_p99 = under_load[int(n_samples * 0.99) - 1]

    # Gate on median, not p95.  With n=200 the median is stable to ±0.1ms;
    # p95 of a ~1ms baseline can swing ±30% from a single OS scheduling
    # event, making a ratio gate on p95 unreliable at these timescales.
    baseline_median = statistics.median(baseline)  # baseline already sorted
    ratio = load_p50 / baseline_median if baseline_median else float("inf")

    print(
        f"\n[rdr-070 discover-load] memory_search n={n_samples} entries=300 "
        f"discover_iters={discover_iterations['n']} "
        f"baseline_p95={baseline_p95:.2f}ms baseline_median={baseline_median:.2f}ms "
        f"load_p50={load_p50:.2f}ms load_p95={load_p95:.2f}ms "
        f"load_p99={load_p99:.2f}ms median_ratio={ratio:.2f}x"
    )

    assert load_p50 < baseline_median * 10.0, (
        f"memory_search median inflated during discover_topics: "
        f"baseline_median={baseline_median:.2f}ms load_median={load_p50:.2f}ms "
        f"ratio={ratio:.2f}x (threshold 10.0x)"
    )


#: Rounds of the interleaved measurement: each round reads first with the writers paused, then with them writing, so
#: a change in the runner's own load during the test lands on both samples alike.
_LOAD_ROUNDS = 4
_READS_PER_PHASE = 25
#: A starved read waits behind a writer, which puts the under-load tail well above the same run's write-free tail.
#: The ceiling is this many times the baseline p95, and never below the test's floor: on a fast box the floor binds
#: (it is the old absolute ceiling), on a slow one the run's own tail sets the bar. Develop run 44337073652 on a
#: lab-6c18g pool runner measured baseline_p95=56.15ms with no writers at all and load_p95=60.36ms (median ratio
#: 1.26x): every read was slow and none was starved, and the fixed 50ms ceiling failed it.
_TAIL_RATIO_MAX = 5.0


def _reads_under_write_load(db: T2Database, read) -> tuple[list[float], list[float]]:
    """(write-free latencies, under-write-load latencies) in ms of ``read(i)``, interleaved over ``_LOAD_ROUNDS``
    rounds. Two writers (telemetry and plan library) run only while ``writing`` is set; a baseline phase starts
    only once both have finished any write in flight."""
    writing = threading.Event()
    stop = threading.Event()
    idle = [threading.Event(), threading.Event()]
    errors: list[BaseException] = []

    def telemetry(i: int) -> None:
        db.log_relevance(
            query=f"q{i}", chunk_id=_cid(f"c{i}"), action="click", session_id="load", collection="knowledge__load"
        )

    def plan(i: int) -> None:
        db.save_plan(query=f"plan {i}", plan_json='{"step":"x"}', tags="load", verb="query")

    def writer(slot: int, write) -> None:
        i = 0
        try:
            while not stop.is_set():
                if not writing.is_set():
                    idle[slot].set()
                    writing.wait(timeout=0.05)
                    continue
                idle[slot].clear()
                write(i)
                i += 1
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            idle[slot].set()

    threads = [
        threading.Thread(target=writer, args=(0, telemetry), daemon=True),
        threading.Thread(target=writer, args=(1, plan), daemon=True),
    ]
    for th in threads:
        th.start()
    baseline: list[float] = []
    under_load: list[float] = []
    try:
        n = 0
        for _ in range(_LOAD_ROUNDS):
            writing.clear()
            for ev in idle:
                assert ev.wait(timeout=30.0), "a writer did not finish its write in flight"
            for _ in range(_READS_PER_PHASE):
                start = time.perf_counter()
                read(n)
                baseline.append((time.perf_counter() - start) * 1000)
                n += 1
            writing.set()
            # nexus-8g79.26: measure only once both writers have started a write, not after a fixed sleep.
            deadline = time.monotonic() + 5.0
            while any(ev.is_set() for ev in idle) and time.monotonic() < deadline:
                time.sleep(0.001)
            for _ in range(_READS_PER_PHASE):
                start = time.perf_counter()
                read(n)
                under_load.append((time.perf_counter() - start) * 1000)
                n += 1
    finally:
        stop.set()
        writing.set()
        for th in threads:
            th.join(timeout=30)
    assert not errors, f"Background writers raised: {errors}"
    return sorted(baseline), sorted(under_load)


def _assert_not_starved(name: str, baseline: list[float], under_load: list[float], ceiling_floor_ms: float) -> None:
    """Two gates (nexus-c7l4n), each relative to this run's own write-free reads: the median, which a lock regression
    moves and scheduler jitter does not, and the p95 against ``_TAIL_RATIO_MAX`` times the baseline p95, floored at
    ``ceiling_floor_ms``."""
    assert len(baseline) == len(under_load) == _LOAD_ROUNDS * _READS_PER_PHASE
    baseline_p95 = baseline[int(len(baseline) * 0.95) - 1]
    baseline_median = statistics.median(baseline)
    load_p50 = statistics.median(under_load)
    load_p95 = under_load[int(len(under_load) * 0.95) - 1]
    load_p99 = under_load[int(len(under_load) * 0.99) - 1]
    # nexus-3m01p: the median denominator is floored, so a lucky-fast baseline cannot manufacture a ratio.
    denom = max(baseline_median, 1.0)
    ratio = load_p50 / denom
    ceiling = max(ceiling_floor_ms, _TAIL_RATIO_MAX * baseline_p95)
    print(
        f"\n[rdr-063 under-load] {name} n={len(under_load)} rounds={_LOAD_ROUNDS} "
        f"baseline_median={baseline_median:.2f}ms baseline_p95={baseline_p95:.2f}ms "
        f"load_p50={load_p50:.2f}ms load_p95={load_p95:.2f}ms load_p99={load_p99:.2f}ms "
        f"median_ratio={ratio:.2f}x tail_ceiling={ceiling:.1f}ms"
    )
    # NON-VACUITY: a zeroed sample would satisfy both gates trivially.
    assert load_p95 > 0.0 and load_p50 > 0.0, "under-load sample is degenerate; nothing was measured"
    assert load_p50 < denom * 10.0, (
        f"{name} median inflated under concurrent write load: baseline_median={baseline_median:.2f}ms "
        f"(denom={denom:.2f}ms) load_p50={load_p50:.2f}ms ratio={ratio:.2f}x (threshold 10.0x)"
    )
    assert load_p95 < ceiling, (
        f"{name} p95 under concurrent write load is {load_p95:.2f}ms, over the starvation ceiling {ceiling:.1f}ms "
        f"(max of {ceiling_floor_ms:.0f}ms and {_TAIL_RATIO_MAX:.0f}x the write-free p95 {baseline_p95:.2f}ms): "
        f"reads are being blocked by writers, not merely slowed (load_p99={load_p99:.2f}ms)"
    )


def test_memory_get_under_concurrent_write_load(tmp_path: Path) -> None:
    """memory.get() must not be starved by concurrent write load (see ``_TAIL_RATIO_MAX``). The 50ms floor of the
    tail ceiling is the old absolute ceiling: a point lookup's p95 under load on a GHA runner is about 4.5ms, and
    starvation costs tens to hundreds of ms."""
    db = T2Database(tmp_path / "get_underload.db")
    try:
        row_ids = [db.put(project="load", title=f"entry{i}", content=f"content {i}") for i in range(200)]
        baseline, under_load = _reads_under_write_load(db, lambda i: db.memory.get(id=row_ids[i % len(row_ids)]))
    finally:
        db.close()
    _assert_not_starved("memory_get", baseline, under_load, ceiling_floor_ms=50.0)


def test_memory_search_under_concurrent_write_load(tmp_path: Path) -> None:
    """memory_search must not be starved by concurrent write load. The 150ms floor (nexus-c7l4n round 2): an FTS
    query's whole under-load distribution on a 2-vCPU GHA runner sits near 50ms with a flat tail, and starvation
    shows as outliers of hundreds of ms to seconds."""
    db = T2Database(tmp_path / "underload.db")
    try:
        for i in range(200):
            db.put(project="load", title=f"entry{i}", content=f"content {i} keyword lorem ipsum")
        baseline, under_load = _reads_under_write_load(db, lambda i: db.search(query="keyword", project="load"))
    finally:
        db.close()
    _assert_not_starved("memory_search", baseline, under_load, ceiling_floor_ms=150.0)


# ── RDR-129 B1 (nexus-qi1zb): serving busy_timeout raised 5000 -> 30000 ──────
#
# test_serving_busy_timeout_constant_matches_bootstrap RETIRED (nexus-i711w
# terminal deletion): its subject was the SQLite serving-connection tuning
# constant (``db/t2/_tuning.py``), which was deleted with the local SQLite
# catalog store — the last consumer of the serving PRAGMA profile. The engine
# substrate has no client-side busy_timeout to pin.
