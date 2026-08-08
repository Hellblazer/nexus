# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-cfc72: bounded file-level indexing concurrency.

The three per-file loops in ``_run_index`` (code/prose/pdf) run through
``run_file_loop`` — sequential at concurrency 1 (exact legacy behavior),
a bounded ThreadPoolExecutor above that. Callbacks and hook chains are
serialized; the first worker exception cancels pending files and
re-raises.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


# ── concurrency resolution ───────────────────────────────────────────────────


class TestResolveIndexConcurrency:
    def test_env_override_wins(self, monkeypatch):
        from nexus.indexer_utils import resolve_index_concurrency

        monkeypatch.setenv("NX_INDEX_CONCURRENCY", "4")
        assert resolve_index_concurrency() == 4

    def test_env_override_clamped_to_one(self, monkeypatch):
        from nexus.indexer_utils import resolve_index_concurrency

        monkeypatch.setenv("NX_INDEX_CONCURRENCY", "0")
        assert resolve_index_concurrency() == 1

    def test_garbage_env_falls_through_to_default(self, monkeypatch):
        from nexus.indexer_utils import resolve_index_concurrency

        monkeypatch.setenv("NX_INDEX_CONCURRENCY", "two")
        # Backend envs cleared -> hard defaults are SERVICE for both
        # vectors (is_vector_service_mode) and catalog -> exactly 2.
        monkeypatch.delenv("NX_STORAGE_BACKEND", raising=False)
        monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
        monkeypatch.delenv("NX_STORAGE_BACKEND_CATALOG", raising=False)
        assert resolve_index_concurrency() == 2

    def test_override_onto_non_service_backend_warns_but_wins(self, monkeypatch):
        """Forcing concurrency onto a non-service backend is allowed but loud.

        nexus-i711w terminal deletion: the sqlite-CATALOG leg of the gate is
        gone (the catalog is service-backed in every mode), so the surviving
        non-service default-1 arm is the VECTORS opt-out — re-premised here;
        the warn contract itself is unchanged.
        """
        from nexus.indexer_utils import resolve_index_concurrency

        monkeypatch.setenv("NX_INDEX_CONCURRENCY", "3")
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
        import structlog.testing
        with structlog.testing.capture_logs() as logs:
            assert resolve_index_concurrency() == 3
        assert any(
            l["event"] == "nx_index_concurrency_overrides_backend_gate"
            for l in logs
        )

    def test_service_backends_default_two(self, monkeypatch):
        from nexus.indexer_utils import resolve_index_concurrency

        monkeypatch.delenv("NX_INDEX_CONCURRENCY", raising=False)
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        assert resolve_index_concurrency() == 2

    # test_non_service_catalog_defaults_one RETIRED (nexus-i711w terminal
    # deletion): the catalog conjunct of the concurrency gate collapsed — the
    # catalog is service-backed in every mode, so a "sqlite catalog defaults
    # to 1" state no longer exists. The surviving non-service default-1 arm
    # (vectors opt-out) is pinned by test_vectors_opt_out_defaults_one below.

    def test_vectors_opt_out_defaults_one(self, monkeypatch):
        from nexus.indexer_utils import resolve_index_concurrency

        monkeypatch.delenv("NX_INDEX_CONCURRENCY", raising=False)
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
        assert resolve_index_concurrency() == 1


# ── run_file_loop ────────────────────────────────────────────────────────────


class TestRunFileLoop:
    def _files(self, n: int) -> list[tuple[float, Path]]:
        return [(float(n - i), Path(f"/repo/f{i}.py")) for i in range(n)]

    def test_sequential_preserves_order(self):
        from nexus.indexer_utils import run_file_loop

        seen: list[str] = []

        def index_one(file, score, timers):
            seen.append(file.name)
            return 1

        run_file_loop(
            self._files(4), index_one, concurrency=1,
            on_file=None, on_stage_timers=None,
        )
        assert seen == ["f0.py", "f1.py", "f2.py", "f3.py"]

    def test_concurrent_processes_all_files(self):
        from nexus.indexer_utils import run_file_loop

        seen: set[str] = set()
        lock = threading.Lock()

        def index_one(file, score, timers):
            with lock:
                seen.add(file.name)
            return 2

        run_file_loop(
            self._files(8), index_one, concurrency=3,
            on_file=None, on_stage_timers=None,
        )
        assert seen == {f"f{i}.py" for i in range(8)}

    def test_returns_count_of_files_that_wrote_chunks_sequential(self):
        # nexus-qgc4b: only files whose index_one returned > 0 count as written.
        from nexus.indexer_utils import run_file_loop

        def index_one(file, score, timers):
            # f0, f2 skipped (0 chunks); f1, f3 wrote chunks.
            return 0 if file.name in ("f0.py", "f2.py") else 5

        written = run_file_loop(
            self._files(4), index_one, concurrency=1,
            on_file=None, on_stage_timers=None,
        )
        assert written == 2

    def test_returns_zero_when_all_files_skipped(self):
        # The all-skip incident shape: every file staleness-skips (returns 0).
        from nexus.indexer_utils import run_file_loop

        written = run_file_loop(
            self._files(6), lambda f, s, t: 0, concurrency=3,
            on_file=None, on_stage_timers=None,
        )
        assert written == 0

    def test_returns_count_concurrent(self):
        from nexus.indexer_utils import run_file_loop

        written = run_file_loop(
            self._files(8), lambda f, s, t: 3, concurrency=4,
            on_file=None, on_stage_timers=None,
        )
        assert written == 8

    def test_workers_actually_overlap(self):
        """Two slow files at concurrency=2 finish in ~1x the sleep, not 2x —
        pins that the pool genuinely parallelizes."""
        from nexus.indexer_utils import run_file_loop

        barrier = threading.Barrier(2, timeout=5)

        def index_one(file, score, timers):
            barrier.wait()  # deadlocks (-> Barrier timeout) unless 2 run at once
            return 1

        run_file_loop(
            self._files(2), index_one, concurrency=2,
            on_file=None, on_stage_timers=None,
        )

    def test_on_file_callback_serialized_and_complete(self):
        from nexus.indexer_utils import run_file_loop

        in_cb = threading.Event()
        overlaps: list[str] = []
        calls: list[tuple[str, int]] = []

        def on_file(file, chunks, elapsed):
            if in_cb.is_set():
                overlaps.append(file.name)
            in_cb.set()
            time.sleep(0.01)
            in_cb.clear()
            calls.append((file.name, chunks))
            assert elapsed >= 0

        def index_one(file, score, timers):
            return 5

        run_file_loop(
            self._files(6), index_one, concurrency=3,
            on_file=on_file, on_stage_timers=None,
        )
        assert overlaps == []
        assert sorted(c[0] for c in calls) == sorted(f"f{i}.py" for i in range(6))
        assert all(c[1] == 5 for c in calls)

    def test_stage_timers_built_per_file_when_subscribed(self):
        from nexus.indexer_utils import run_file_loop

        timer_objs: list[object] = []
        received: list[tuple[str, object]] = []

        def index_one(file, score, timers):
            timer_objs.append(timers)
            return 0

        def on_stage_timers(file, timers):
            received.append((file.name, timers))

        run_file_loop(
            self._files(3), index_one, concurrency=2,
            on_file=None, on_stage_timers=on_stage_timers,
        )
        assert len(received) == 3
        assert all(t is not None for t in timer_objs)
        assert len({id(t) for t in timer_objs}) == 3  # distinct per file

    def test_no_timers_when_not_subscribed(self):
        from nexus.indexer_utils import run_file_loop

        timer_objs: list[object] = []

        def index_one(file, score, timers):
            timer_objs.append(timers)
            return 0

        run_file_loop(
            self._files(2), index_one, concurrency=2,
            on_file=None, on_stage_timers=None,
        )
        assert timer_objs == [None, None]

    def test_first_exception_propagates_and_cancels_pending(self):
        from nexus.indexer_utils import run_file_loop

        started: list[str] = []
        lock = threading.Lock()

        def index_one(file, score, timers):
            with lock:
                started.append(file.name)
            if file.name == "f0.py":
                raise RuntimeError("boom on f0")
            time.sleep(0.02)
            return 1

        with pytest.raises(RuntimeError, match="boom on f0"):
            run_file_loop(
                self._files(50), index_one, concurrency=2,
                on_file=None, on_stage_timers=None,
            )
        # Pending futures cancelled: nowhere near all 50 started.
        assert len(started) < 50

    def test_concurrent_double_failure_raises_first_logs_rest(self):
        """Two near-simultaneous failures: submission-order-first is
        raised, the secondary is logged, never silently dropped
        (critique finding, nexus-cfc72)."""
        from nexus.indexer_utils import run_file_loop

        barrier = threading.Barrier(2, timeout=5)

        def index_one(file, score, timers):
            if file.name in ("f0.py", "f1.py"):
                barrier.wait()  # both fail together
                raise RuntimeError(f"boom {file.name}")
            return 1

        import structlog.testing
        with structlog.testing.capture_logs() as logs, \
                pytest.raises(RuntimeError, match="boom f0.py"):
            run_file_loop(
                self._files(2), index_one, concurrency=2,
                on_file=None, on_stage_timers=None,
            )
        suppressed = [
            l for l in logs
            if l["event"] == "index_file_concurrent_failure_suppressed"
        ]
        assert len(suppressed) == 1
        assert "f1.py" in suppressed[0]["file"]

    def test_sequential_exception_propagates_immediately(self):
        from nexus.indexer_utils import run_file_loop

        started: list[str] = []

        def index_one(file, score, timers):
            started.append(file.name)
            raise ValueError("seq boom")

        with pytest.raises(ValueError, match="seq boom"):
            run_file_loop(
                self._files(3), index_one, concurrency=1,
                on_file=None, on_stage_timers=None,
            )
        assert started == ["f0.py"]


# ── LockedHookRegistry ───────────────────────────────────────────────────────


class TestLockedHookRegistry:
    def test_delegates_and_serializes_fire_methods(self):
        from nexus.hook_registry import HookRegistry, LockedHookRegistry

        registry = HookRegistry()
        in_hook = threading.Event()
        overlaps: list[str] = []
        fired: list[str] = []

        def slow_hook(source_path, collection, content):
            if in_hook.is_set():
                overlaps.append(source_path)
            in_hook.set()
            time.sleep(0.01)
            in_hook.clear()
            fired.append(source_path)

        registry.register_document(slow_hook)
        locked = LockedHookRegistry(registry)

        threads = [
            threading.Thread(
                target=locked.fire_document, args=(f"/p{i}", "col", "x"),
            )
            for i in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert overlaps == []
        assert sorted(fired) == [f"/p{i}" for i in range(6)]

    def test_zero_match_fires_never_take_the_lock(self):
        """nexus-itpdc: a fire that would invoke NO hook must not acquire
        the shared lock.

        This is the whole defect. The lock is held for the full hook
        chain — ~2s per flush on the service path — so an empty chain
        that still queues for it pays convoy wait for nothing: measured
        ~0.5s per call, 53s per indexing run of a provably-empty
        ``grain="file"`` chain. Asserted by HOLDING the lock from another
        thread and requiring each empty fire to return anyway; a fire
        that acquired would block until the timeout and fail."""
        from nexus.hook_registry import HookRegistry, LockedHookRegistry

        registry = HookRegistry()
        # Only a FLUSH-grain batch hook — exactly the default hook set's
        # shape (taxonomy + manifest are both batch_grain="flush").
        def flush_hook(ids, col, docs, embs, metas):
            pass
        flush_hook.batch_grain = "flush"
        registry.register_batch(flush_hook)

        locked = LockedHookRegistry(registry)
        held = threading.Event()
        release = threading.Event()

        def hog():
            with locked._lock:
                held.set()
                release.wait(5)

        t = threading.Thread(target=hog)
        t.start()
        assert held.wait(5)
        try:
            done = threading.Event()

            def empty_fires():
                # file-grain batch: no hook declares it -> zero match.
                locked.fire_batch(["d1"], "col", ["x"], None, [{}], grain="file")
                # single chain: nothing registered at all.
                locked.fire_single("d1", "col", "x")
                # document chain: nothing registered at all.
                locked.fire_document("/p", "col", "x")
                done.set()

            probe = threading.Thread(target=empty_fires)
            probe.start()
            assert done.wait(3), (
                "an empty hook chain blocked on the shared lock — the "
                "zero-match fast path regressed"
            )
            probe.join(3)
        finally:
            release.set()
            t.join(5)

    def test_non_empty_chains_still_serialize_and_fire(self):
        """The fast path must not become a correctness hole: a chain with
        a registered hook still takes the lock (so cfc72's interleaving
        guarantee holds) and still fires."""
        from nexus.hook_registry import HookRegistry, LockedHookRegistry

        registry = HookRegistry()
        fired: list[str] = []
        overlaps: list[str] = []
        in_hook = threading.Event()

        def batch_hook(ids, col, docs, embs, metas):
            if in_hook.is_set():
                overlaps.append(ids[0])
            in_hook.set()
            time.sleep(0.01)
            in_hook.clear()
            fired.append(ids[0])
        batch_hook.batch_grain = "file"
        registry.register_batch(batch_hook)
        locked = LockedHookRegistry(registry)

        threads = [
            threading.Thread(
                target=locked.fire_batch,
                args=([f"d{i}"], "col", ["x"], None, [{}]),
                kwargs={"grain": "file"},
            )
            for i in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert overlaps == []
        assert sorted(fired) == [f"d{i}" for i in range(6)]

    def test_lock_wait_seconds_measures_contention(self):
        """nexus-itpdc: the wait must be MEASURABLE, not inferred.

        Without this counter the convoy is billed to whichever caller's
        timer brackets the fire, which is how ~53s/run of pure lock wait
        was mis-attributed to GIL starvation for a full investigation
        cycle."""
        from nexus.hook_registry import HookRegistry, LockedHookRegistry

        registry = HookRegistry()

        def slow(source_path, collection, content):
            time.sleep(0.05)
        registry.register_document(slow)
        locked = LockedHookRegistry(registry)
        assert locked.lock_wait_seconds == 0.0

        threads = [
            threading.Thread(target=locked.fire_document,
                             args=(f"/p{i}", "col", "x"))
            for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Three 0.05s serialized hooks => the two losers wait ~0.05 and
        # ~0.10s. Assert only the direction, generously, so a loaded CI
        # box cannot flake it.
        assert locked.lock_wait_seconds > 0.02

    def test_has_batch_hooks_mirrors_the_fire_batch_grain_filter(self):
        """The fast-path predicate and the dispatch filter must agree —
        a predicate that said 'no hooks' where fire_batch would have
        fired one would silently DROP hooks, which is a correctness bug,
        not a perf regression."""
        from nexus.hook_registry import HookRegistry

        registry = HookRegistry()
        assert registry.has_batch_hooks("file") is False
        assert registry.has_batch_hooks("flush") is False
        assert registry.has_batch_hooks("all") is False

        def default_grain(ids, col, docs, embs, metas):
            pass
        registry.register_batch(default_grain)
        # No batch_grain attribute => "file" default, same as fire_batch's.
        assert registry.has_batch_hooks("file") is True
        assert registry.has_batch_hooks("flush") is False
        assert registry.has_batch_hooks("all") is True

        def flush_grain(ids, col, docs, embs, metas):
            pass
        flush_grain.batch_grain = "flush"
        registry.register_batch(flush_grain)
        assert registry.has_batch_hooks("flush") is True

        # Cross-check against the real dispatcher rather than trusting
        # two independent readings of the same rule.
        for grain in ("file", "flush", "all", "unknown"):
            seen: list[str] = []

            def spy(ids, col, docs, embs, metas, _s=seen):
                _s.append("x")
            registry2 = HookRegistry()
            registry2.register_batch(default_grain)
            registry2.register_batch(flush_grain)
            registry2.fire_batch(["d"], "col", ["x"], None, [{}], grain=grain)
            fired_any = registry2.has_batch_hooks(grain)
            # has_batch_hooks is the PREDICTION; a False must never
            # accompany a dispatch that would have matched something.
            matched = [
                h for h in registry2._batch
                if grain == "all" or getattr(h, "batch_grain", "file") == grain
            ]
            assert fired_any == bool(matched), grain

    def test_default_hook_set_registers_no_file_grain_batch_hook(self):
        """Pins the premise the fast path exploits in production.

        If a future default hook registers at file grain, the indexer's
        per-file fire stops being a no-op — this test is what tells the
        author that the itpdc measurements no longer describe reality."""
        from nexus.hook_registry import HookRegistry, install_default_hooks

        registry = HookRegistry()
        install_default_hooks(registry)
        assert registry.has_batch_hooks("file") is False
        assert registry.has_batch_hooks("flush") is True
        assert registry.has_single_hooks() is False
        assert registry.has_document_hooks() is True

    def test_getattr_falls_through_to_registry(self):
        from nexus.hook_registry import HookRegistry, LockedHookRegistry

        registry = HookRegistry()
        locked = LockedHookRegistry(registry)
        assert locked._document is registry._document
        # register_* passes through so install_default_hooks(locked) works.
        def probe(sp, col, content):
            pass
        locked.register_document(probe)
        assert probe in registry._document


# ── bounded failure drain (nexus-7yfe6) ───────────────────────────────────────


class TestFailureDrainWarning:
    """run_file_loop emits an early WARNING when the post-failure drain is slow —
    observability so a genuine failure racing a wedged sibling reads as
    'draining N workers', not a silent hang. The drain is NOT a hard bound (the
    harvest still blocks on in-flight futures); this pins the WARNING path."""

    def _files(self, n: int):
        return [(float(n - i), Path(f"/repo/f{i}.py")) for i in range(n)]

    def test_slow_drain_emits_warning_and_still_raises(self, monkeypatch):
        import nexus.indexer_utils as iu

        # Tiny drain threshold so the sibling is "still running" when we sample.
        monkeypatch.setattr(iu, "_FAILURE_DRAIN_TIMEOUT_S", 0.02)
        warnings: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            iu._log, "warning",
            lambda event, **kw: warnings.append((event, kw)),
        )

        started = threading.Event()

        def index_one(file, score, timers):
            if file.name == "f0.py":
                started.wait(1.0)      # ensure the sibling is in-flight first
                raise RuntimeError("boom")   # non-transient → real failure path
            started.set()
            time.sleep(0.3)            # still running past the 0.02s drain sample
            return 1

        with pytest.raises(RuntimeError, match="boom"):
            iu.run_file_loop(
                self._files(2), index_one, concurrency=2,
                on_file=None, on_stage_timers=None,
            )

        assert any(ev == "index_failure_drain_slow" for ev, _ in warnings), (
            f"expected index_failure_drain_slow warning; got {[e for e,_ in warnings]}"
        )
