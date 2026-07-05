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
        """Forcing concurrency onto a sqlite catalog is allowed but loud."""
        from nexus.indexer_utils import resolve_index_concurrency

        monkeypatch.setenv("NX_INDEX_CONCURRENCY", "3")
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "sqlite")
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

    def test_non_service_catalog_defaults_one(self, monkeypatch):
        from nexus.indexer_utils import resolve_index_concurrency

        monkeypatch.delenv("NX_INDEX_CONCURRENCY", raising=False)
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "sqlite")
        assert resolve_index_concurrency() == 1

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
