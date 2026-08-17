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


def test_memory_get_under_concurrent_write_load(tmp_path: Path) -> None:
    """memory.get() must not be starved by concurrent write load.

    TWO gates, neither of them a tail-vs-tail ratio (nexus-c7l4n):

    * relative, on the MEDIAN — the stable statistic at this timescale;
    * absolute, on p95 — the starvation ceiling an operator cares about.

    The ratio used to be ``load_p95 / baseline_p95`` at 3.0x, which is a
    tail divided by a tail on sub-5ms samples. nexus-3m01p already caught
    that shape once from the denominator side (a lucky-fast baseline
    manufacturing a ratio) and floored the denominator at 1.0ms. GHA fired
    it again from the NUMERATOR side on the 7.7.0 release PR (CI run
    31773244689, py3.13 shard 4/4)::

        baseline_p95=1.50ms (denom=1.50ms) load_p50=2.56ms
        load_p95=4.54ms load_p99=5.95ms ratio=3.03x   FAILED (threshold 3.0x)

    Nothing was starved: load_p95 sat 11x under the 50ms ceiling. The
    baseline landed at 1.50ms — BELOW the 1.70-2.18ms clean-box band the
    1.0ms floor was calibrated against, so the floor did not bind — while
    the load tail landed at 2.2x the highest previously observed load_p95
    (2.08ms). Both tails moved, in opposite directions, on a shared-tenancy
    2-vCPU runner where two spinning writer threads plus the measuring
    thread genuinely oversubscribe the CPU. A tail ratio cannot tell CPU
    oversubscription from lock contention; the median can (a lock
    regression serializes EVERY read, not just the tail), and the absolute
    ceiling can (starvation costs tens to hundreds of ms, not ~4ms).

    The 10.0x median threshold matches
    ``test_memory_search_under_discover_topics_load`` in this file, which reached
    the same conclusion for the same reason ("p95 of a ~1ms baseline can
    swing +/-30% from a single OS scheduling event"), and matches what this
    docstring always claimed the test was for: order-of-magnitude lock
    regressions, not per-core scheduling variance.
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
        writers_started = threading.Barrier(3)  # 2 writers + main
        writer_errors: list[BaseException] = []

        def telemetry_writer() -> None:
            i = 0
            try:
                writers_started.wait(timeout=5.0)
                while not stop_writers.is_set():
                    db.log_relevance(
                        query=f"q{i}",
                        chunk_id=_cid(f"c{i}"),
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
                writers_started.wait(timeout=5.0)
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

        # nexus-8g79.26: 3-way Barrier rendezvous so both writers start
        # their first SQL call before we measure under-load latency.
        # Replaces a 50ms time.sleep that was flaky on loaded CI runners.
        writers_started.wait(timeout=5.0)

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
    # nexus-3m01p: FLOOR the denominator. This was a bare
    # `load_p95 / baseline_p95`, which made the assertion a function of how
    # lucky the BASELINE sample was rather than of how the code behaves under
    # load. Measured on one machine, same tree, minutes apart:
    #
    #   loaded box:  baseline_p95=0.58ms load_p95=1.85ms -> 3.22x  FAILED
    #   quiet box:   baseline_p95=1.61ms load_p95=3.42ms -> 2.13x  passed
    #   quiet box, 5 runs: baseline_p95 1.70-2.18ms, load_p95 1.77-2.08ms, ~1.0x
    #
    # Note the direction: the FAILING run's load_p95 (1.85ms) sits inside the
    # normal band. Nothing was slow. The 0.58ms baseline — a third of every
    # other observation — shrank the denominator and manufactured the ratio.
    # baseline[94] is the 6th-slowest of 100 sub-millisecond samples, so it is
    # dominated by scheduler noise, and the tighter it lands the more likely a
    # spurious failure. That is backwards for a regression gate.
    #
    # The floor sits below every non-anomalous baseline observed (1.70ms was the
    # lowest of five clean runs), so it does not weaken the ratio in the regime
    # the ratio is for; it only stops a sub-millisecond denominator from
    # inventing one.
    #
    # nexus-c7l4n keeps that floor and moves the ratio itself off the tail:
    # the denominator is now the baseline MEDIAN (still floored, for the same
    # reason), and the numerator is the under-load MEDIAN. See the docstring
    # for the GHA observation that forced it.
    _BASELINE_FLOOR_MS = 1.0
    baseline_median = statistics.median(baseline)  # baseline already sorted
    denom = max(baseline_median, _BASELINE_FLOOR_MS)
    ratio = load_p50 / denom
    _MEDIAN_RATIO_MAX = 10.0

    print(
        f"\n[rdr-063 under-load] memory_get n=100 entries=200 "
        f"baseline_p95={baseline_p95:.2f}ms "
        f"baseline_median={baseline_median:.2f}ms (denom={denom:.2f}ms) "
        f"load_p50={load_p50:.2f}ms load_p95={load_p95:.2f}ms "
        f"load_p99={load_p99:.2f}ms median_ratio={ratio:.2f}x"
    )

    # NON-VACUITY: the measurement has to have happened at all. A zeroed or
    # empty sample would satisfy both assertions below trivially.
    assert load_p95 > 0.0, "under-load sample is degenerate; nothing was measured"
    assert load_p50 > 0.0, "under-load median is degenerate; nothing was measured"

    # (1) Relative: reads must not slow down against their OWN baseline on this
    #     machine. Machine-independent, which is why it is worth keeping — but
    #     measured on the median, which a lock regression moves and scheduler
    #     jitter does not.
    assert load_p50 < denom * _MEDIAN_RATIO_MAX, (
        f"memory.get median inflated under concurrent write load: "
        f"baseline_median={baseline_median:.2f}ms (denom={denom:.2f}ms) "
        f"load_p50={load_p50:.2f}ms ratio={ratio:.2f}x "
        f"(threshold {_MEDIAN_RATIO_MAX:.1f}x)"
    )

    # (2) Absolute: the thing an operator actually cares about. Starvation —
    #     a read stuck behind a write transaction — costs tens to hundreds of
    #     ms, not the ~2ms this measures; the ceiling is ~24x the highest
    #     load_p95 observed (2.08ms) and ~11x the highest load_p99 (4.60ms), so
    #     it cannot fire on ordinary noise and cannot be defeated by a lucky
    #     baseline the way the ratio alone could.
    _LOAD_P95_CEILING_MS = 50.0
    assert load_p95 < _LOAD_P95_CEILING_MS, (
        f"memory.get p95 under concurrent write load is {load_p95:.2f}ms, over "
        f"the {_LOAD_P95_CEILING_MS:.0f}ms starvation ceiling — reads are being "
        f"blocked by writers, not merely slowed (load_p99={load_p99:.2f}ms)"
    )


def test_memory_search_under_concurrent_write_load(tmp_path: Path) -> None:
    """memory_search must not be starved by concurrent write load.

    Same two-gate shape as ``test_memory_get_under_concurrent_write_load``
    above — see that docstring for the measurement argument. This test
    carried the ORIGINAL, un-floored ``load_p95 / baseline_p95`` at 3.0x:
    the exact form that fired on a dev box (nexus-3m01p) and then on GHA
    (nexus-c7l4n) in its sibling, and it had no absolute ceiling at all, so
    the only thing standing between it and the same spurious red was which
    of the two the shard scheduler happened to run on a busy runner.
    Converted here in the same change rather than left as a known landmine:
    the relative gate moves to the floored MEDIAN, and it GAINS the 50ms
    absolute starvation ceiling plus a non-vacuity assert it never had.
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
        writers_started = threading.Barrier(3)  # 2 writers + main
        writer_errors: list[BaseException] = []

        def telemetry_writer() -> None:
            i = 0
            try:
                writers_started.wait(timeout=5.0)
                while not stop_writers.is_set():
                    db.log_relevance(
                        query=f"q{i}",
                        chunk_id=_cid(f"c{i}"),
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
                writers_started.wait(timeout=5.0)
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

        # nexus-8g79.26: 3-way Barrier rendezvous (see prior test).
        writers_started.wait(timeout=5.0)

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
    _BASELINE_FLOOR_MS = 1.0
    baseline_median = statistics.median(baseline)  # baseline already sorted
    denom = max(baseline_median, _BASELINE_FLOOR_MS)
    ratio = load_p50 / denom
    _MEDIAN_RATIO_MAX = 10.0

    print(
        f"\n[rdr-063 under-load] memory_search n=100 entries=200 "
        f"baseline_p95={baseline_p95:.2f}ms "
        f"baseline_median={baseline_median:.2f}ms (denom={denom:.2f}ms) "
        f"load_p50={load_p50:.2f}ms load_p95={load_p95:.2f}ms "
        f"load_p99={load_p99:.2f}ms median_ratio={ratio:.2f}x"
    )

    # NON-VACUITY (new here): a zeroed or empty sample would satisfy both
    # gates below trivially.
    assert load_p95 > 0.0, "under-load sample is degenerate; nothing was measured"
    assert load_p50 > 0.0, "under-load median is degenerate; nothing was measured"

    # (1) Relative, on the median — a lock regression moves it, scheduler
    #     jitter does not.
    assert load_p50 < denom * _MEDIAN_RATIO_MAX, (
        f"memory_search median inflated under concurrent write load: "
        f"baseline_median={baseline_median:.2f}ms (denom={denom:.2f}ms) "
        f"load_p50={load_p50:.2f}ms ratio={ratio:.2f}x "
        f"(threshold {_MEDIAN_RATIO_MAX:.1f}x)"
    )

    # (2) Absolute (new here): the starvation ceiling. CALIBRATION (nexus-c7l4n
    #     round 2): 50.0 was the memory_get sibling's constant (a point-lookup,
    #     ~4.5ms p95 under load on GHA). memory_search is an FTS query and on a
    #     2-vCPU GHA runner its whole under-load distribution sits ~50ms UNIFORM
    #     (observed p95=51.26 / p99=52.02 — a FLAT tail, the opposite of the
    #     starvation shape). Genuine reader-blocked-by-writer starvation
    #     manifests as pathological outliers (hundreds of ms to seconds — the
    #     lock-wedge class this assert exists for), so a 150ms ceiling still
    #     catches every real event while tolerating slow-runner uniformity;
    #     the floored-median relative gate above remains the sensitive detector.
    _LOAD_P95_CEILING_MS = 150.0
    assert load_p95 < _LOAD_P95_CEILING_MS, (
        f"memory_search p95 under concurrent write load is {load_p95:.2f}ms, "
        f"over the {_LOAD_P95_CEILING_MS:.0f}ms starvation ceiling — reads are "
        f"being blocked by writers, not merely slowed "
        f"(load_p99={load_p99:.2f}ms)"
    )


# ── RDR-129 B1 (nexus-qi1zb): serving busy_timeout raised 5000 -> 30000 ──────
#
# test_serving_busy_timeout_constant_matches_bootstrap RETIRED (nexus-i711w
# terminal deletion): its subject was the SQLite serving-connection tuning
# constant (``db/t2/_tuning.py``), which was deleted with the local SQLite
# catalog store — the last consumer of the serving PRAGMA profile. The engine
# substrate has no client-side busy_timeout to pin.
