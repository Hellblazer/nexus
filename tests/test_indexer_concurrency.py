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

    # ── nexus-deyd5: per-record-survivable extraction failures must not
    # abort the whole run. ──────────────────────────────────────────────

    def test_survivable_exception_sequential_indexes_every_other_file(self):
        """One PER_RECORD_SURVIVABLE_EXCEPTIONS-class raise among N leaves
        the other N-1 files indexed and the run returns normally (no
        exception) — the literal RED-first repro for nexus-deyd5: a single
        unextractable file must not abort finalization."""
        from nexus.errors import UnextractableContentError
        from nexus.indexer_utils import run_file_loop

        processed: list[str] = []

        def index_one(file, score, timers):
            processed.append(file.name)
            if file.name == "f2.py":
                raise UnextractableContentError("blank_document.pdf: no text extracted")
            return 1

        # Deliberately the LAST file in submission order, mirroring the
        # real incident (3159 files succeeded, the last one raised).
        files = self._files(3)  # f2.py, f1.py, f0.py by descending score
        written = run_file_loop(
            files, index_one, concurrency=1,
            on_file=None, on_stage_timers=None,
        )
        assert sorted(processed) == ["f0.py", "f1.py", "f2.py"]
        assert written == 2  # f0, f1 wrote; f2 skipped

    def test_survivable_exception_concurrent_indexes_every_other_file(self):
        """Same contract under the ThreadPoolExecutor branch — the shape
        of the actual laravel/framework incident (concurrency=2)."""
        from nexus.errors import UnextractableContentError
        from nexus.indexer_utils import run_file_loop

        processed: set[str] = set()
        lock = threading.Lock()

        def index_one(file, score, timers):
            with lock:
                processed.add(file.name)
            if file.name == "f4.py":
                raise UnextractableContentError("blank fixture: no text extracted")
            return 1

        written = run_file_loop(
            self._files(8), index_one, concurrency=3,
            on_file=None, on_stage_timers=None,
        )
        assert processed == {f"f{i}.py" for i in range(8)}
        assert written == 7

    def test_survivable_exception_reported_via_on_skip_with_path_and_reason(self):
        """The skip must be reported (path + reason), not merely swallowed —
        loud, not silent."""
        from nexus.errors import UnextractableContentError
        from nexus.indexer_utils import run_file_loop

        skips: list[tuple[Path, str]] = []

        def index_one(file, score, timers):
            if file.name == "f1.py":
                raise UnextractableContentError("f1.py: produced empty output")
            return 1

        written = run_file_loop(
            self._files(3), index_one, concurrency=1,
            on_file=None, on_stage_timers=None,
            on_skip=lambda file, reason: skips.append((file, reason)),
        )
        assert written == 2
        assert len(skips) == 1
        skipped_file, reason = skips[0]
        assert skipped_file.name == "f1.py"
        assert "produced empty output" in reason

    def test_survivable_exception_logged_loudly(self):
        """Skips are not silent — a structured event names the file and the
        error, even when the caller supplies no on_skip callback."""
        import structlog.testing

        from nexus.errors import UnextractableContentError
        from nexus.indexer_utils import run_file_loop

        def index_one(file, score, timers):
            if file.name == "f0.py":
                raise UnextractableContentError("f0.py: produced empty output")
            return 1

        with structlog.testing.capture_logs() as logs:
            run_file_loop(
                self._files(2), index_one, concurrency=1,
                on_file=None, on_stage_timers=None,
            )
        skip_events = [l for l in logs if l["event"] == "index_file_skipped_unextractable"]
        assert len(skip_events) == 1
        assert "f0.py" in skip_events[0]["file"]

    def test_unclassified_exception_still_fails_the_run(self):
        """NEGATIVE case, pinning the boundary in the other direction: an
        exception NOT in PER_RECORD_SURVIVABLE_EXCEPTIONS — e.g. a
        credentials/auth failure, the data-loss-adjacent class the bead
        explicitly distinguishes from a per-file extraction failure — must
        still cancel pending files and fail the whole run. A blanket
        except-and-continue would silently pass this test's inverse; this
        pins that we did NOT write one."""
        from nexus.errors import CredentialsMissingError
        from nexus.indexer_utils import run_file_loop

        started: list[str] = []

        def index_one(file, score, timers):
            started.append(file.name)
            if file.name == "f0.py":
                raise CredentialsMissingError("voyage API key missing mid-run")
            return 1

        with pytest.raises(CredentialsMissingError, match="voyage API key missing"):
            run_file_loop(
                self._files(50), index_one, concurrency=2,
                on_file=None, on_stage_timers=None,
            )
        assert len(started) < 50

    def test_unclassified_exception_sequential_still_fails_immediately(self):
        from nexus.errors import CredentialsMissingError
        from nexus.indexer_utils import run_file_loop

        started: list[str] = []

        def index_one(file, score, timers):
            started.append(file.name)
            raise CredentialsMissingError("seq auth boom")

        with pytest.raises(CredentialsMissingError, match="seq auth boom"):
            run_file_loop(
                self._files(3), index_one, concurrency=1,
                on_file=None, on_stage_timers=None,
            )
        assert started == ["f0.py"]

    def test_other_per_record_survivable_members_are_not_caught(self):
        """nexus-deyd5 round 2 (code-review finding): run_file_loop catches
        ONLY UnextractableContentError by name, deliberately NOT the whole
        PER_RECORD_SURVIVABLE_EXCEPTIONS tuple -- the tripwire audit that
        makes tuple membership safe for its dt.py/commands/index.py
        per-record consumers does not scan indexer_utils.py, so catching
        the whole tuple here would be an unguarded coupling. Pin it: a
        SIBLING tuple member (IndexRunVerifyRefused -- a RUNFENCE signal
        explicitly documented as never safe to swallow into a green
        summary) must still cancel pending files and fail the run, exactly
        like any other unclassified exception."""
        from nexus.errors import IndexRunVerifyRefused
        from nexus.indexer_utils import run_file_loop

        started: list[str] = []

        def index_one(file, score, timers):
            started.append(file.name)
            if file.name == "f0.py":
                raise IndexRunVerifyRefused(
                    doc_id="d1", referenced=3, present=1, missing=2, chunk_count=3,
                )
            return 1

        with pytest.raises(IndexRunVerifyRefused):
            run_file_loop(
                self._files(50), index_one, concurrency=2,
                on_file=None, on_stage_timers=None,
            )
        assert len(started) < 50

    # ── nexus-deyd5 round 3: run_file_loop NEVER raises for the systemic-
    # skip condition — that verdict moved to a run-level check in
    # nexus.indexer._run_index (coordinator directive, closing a round-2
    # HIGH finding: a mid-loop raise skipped the batcher's drain and the
    # remaining categories, discarding already-completed work). These pin
    # that run_file_loop itself is now INDIFFERENT to the skip ratio —
    # it always just returns. The boundary-condition math itself is
    # pinned separately below, directly against the pure function
    # nexus.indexer_utils.skip_floor_breached.

    def test_one_bad_fixture_among_many_still_returns_normally(self):
        """The bead's own literal scenario, at scale: 1 skip out of 3160
        attempted (0.03%). run_file_loop must return normally, not raise —
        true regardless of ratio now, since it no longer judges one."""
        from nexus.errors import UnextractableContentError
        from nexus.indexer_utils import run_file_loop

        def index_one(file, score, timers):
            if file.name == "f0.py":
                raise UnextractableContentError("blank_document.pdf: no text extracted")
            return 1

        written = run_file_loop(
            self._files(3160), index_one, concurrency=4,
            on_file=None, on_stage_timers=None,
        )
        assert written == 3159

    def test_total_loss_still_just_returns_zero_never_raises(self):
        """nexus-deyd5 round 3: even 100% of a batch skipping (the
        shape that used to raise SystemicExtractionFailureError in round
        2) must NOT raise from run_file_loop — the run-level verdict
        belongs to the caller (_run_index), evaluated only after every
        category and the batcher's drain have completed. A raise from
        inside this loop would, again, skip the drain and the remaining
        categories -- exactly the round-2 regression."""
        from nexus.errors import UnextractableContentError
        from nexus.indexer_utils import run_file_loop

        def index_one(file, score, timers):
            raise UnextractableContentError(f"{file.name}: no text extracted")

        written = run_file_loop(
            self._files(50), index_one, concurrency=4,
            on_file=None, on_stage_timers=None,
        )
        assert written == 0

    def test_skip_summary_logged_regardless_of_ratio(self):
        import structlog.testing

        from nexus.errors import UnextractableContentError
        from nexus.indexer_utils import run_file_loop

        def index_one(file, score, timers):
            raise UnextractableContentError(f"{file.name}: no text extracted")

        with structlog.testing.capture_logs() as logs:
            written = run_file_loop(
                self._files(2), index_one, concurrency=1,
                on_file=None, on_stage_timers=None,
            )
        assert written == 0
        summaries = [l for l in logs if l["event"] == "index_file_loop_skip_summary"]
        assert len(summaries) == 1
        assert summaries[0]["skipped"] == 2
        assert summaries[0]["total"] == 2


class TestSkipFloorBreached:
    """Unit tests for the pure boundary function itself (nexus-deyd5 round
    3) -- the run-level verdict's math, isolated from run_file_loop /
    _run_index entirely. Pins the two trip conditions in BOTH directions:
    a breach and a non-breach are equally load-bearing, since without the
    negative pin the floor is decoration."""

    def test_total_loss_at_small_n_breaches(self):
        """Every file in a SMALL batch (below the min-sample-size gate)
        skipping is still a breach — a pure ratio-with-minimum-N rule
        alone would miss this; total-loss trips at ANY batch size."""
        from nexus.indexer_utils import skip_floor_breached

        assert skip_floor_breached(3, 3) is True

    def test_majority_at_scale_breaches(self):
        """A large batch (>= the min-sample-size gate) going majority-
        unextractable breaches WITHOUT needing literally every file to
        fail — the shape of a bad dependency/model/permissions bump, or a
        scanned-PDF archive without OCR, chewing through half a corpus."""
        from nexus.indexer_utils import skip_floor_breached

        assert skip_floor_breached(30, 50) is True

    def test_below_ratio_at_scale_does_not_breach(self):
        """The inverse pin: a large batch skipping LESS than half stays
        under the floor — the check must not be so aggressive it flags a
        corpus merely containing a substantial-but-minority chunk of bad
        files."""
        from nexus.indexer_utils import skip_floor_breached

        assert skip_floor_breached(20, 50) is False

    def test_one_bad_fixture_at_scale_does_not_breach(self):
        from nexus.indexer_utils import skip_floor_breached

        assert skip_floor_breached(1, 3160) is False

    def test_below_min_sample_partial_skip_does_not_breach(self):
        """Below the minimum-sample-size gate, a PARTIAL (non-total) skip
        must not breach -- only total loss trips at small N; a ratio rule
        alone would misfire here (1/3 is already 33%)."""
        from nexus.indexer_utils import skip_floor_breached

        assert skip_floor_breached(1, 3) is False

    def test_zero_skipped_never_breaches(self):
        from nexus.indexer_utils import skip_floor_breached

        assert skip_floor_breached(0, 500) is False

    def test_zero_attempted_never_breaches(self):
        from nexus.indexer_utils import skip_floor_breached

        assert skip_floor_breached(0, 0) is False


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
        """nexus-itpdc / nexus-eslkl: a fire that would invoke NO hook must
        not acquire ANY per-hook lock — including one held by a DIFFERENT,
        registered hook.

        Post-eslkl there is no longer a single process-wide mutex (each
        registered hook gets its OWN lock, keyed by callable identity), so
        this is asserted by holding the REGISTERED hook's own lock (via the
        internal ``_lock_for`` accessor) from another thread and requiring
        each empty fire to return anyway; a fire that (incorrectly)
        acquired that same lock would block until the timeout and fail."""
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
            with locked._lock_for(flush_hook):
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
                "an empty hook chain blocked on a registered hook's lock — "
                "the zero-match fast path regressed"
            )
            probe.join(3)
        finally:
            release.set()
            t.join(5)

    def test_different_hooks_do_not_serialize_against_each_other(self):
        """T3 (nexus-eslkl): per-chain locking means TWO DIFFERENT
        registered hooks never wait on each other — only concurrent fires
        of the SAME hook do. Holding hook A's lock from another thread
        must not block a fire that dispatches only hook B.

        Non-vacuity mirror (required by the same design memo): the
        analogous fire that DOES dispatch A while A's lock is held must
        block until release — proving the probe thread's completion above
        is a real "did not need this lock" result, not a fluke of a
        fast/no-op dispatch path.
        """
        from nexus.hook_registry import HookRegistry, LockedHookRegistry

        registry = HookRegistry()

        def hook_a(ids, col, docs, embs, metas):
            pass
        hook_a.batch_grain = "flush"

        def hook_b(source_path, collection, content):
            pass

        registry.register_batch(hook_a)
        registry.register_document(hook_b)
        locked = LockedHookRegistry(registry)

        held = threading.Event()
        release = threading.Event()

        def hold_a():
            with locked._lock_for(hook_a):
                held.set()
                release.wait(5)

        t = threading.Thread(target=hold_a)
        t.start()
        try:
            assert held.wait(5)

            # B is unrelated to A's lock -> must complete without waiting.
            done_b = threading.Event()

            def fire_b():
                locked.fire_document("/p", "col", "x")
                done_b.set()

            probe_b = threading.Thread(target=fire_b)
            probe_b.start()
            assert done_b.wait(3), (
                "firing hook B blocked on hook A's lock — chains that "
                "should be independent are still serializing"
            )
            probe_b.join(3)

            # Non-vacuity mirror: firing A itself, while A's lock is held,
            # MUST block until release — otherwise the predicate above is
            # meaningless (nothing was actually exclusive).
            done_a = threading.Event()

            def fire_a():
                locked.fire_batch(["d1"], "col", ["x"], None, [{}], grain="flush")
                done_a.set()

            probe_a = threading.Thread(target=fire_a)
            probe_a.start()
            assert not done_a.wait(0.2), (
                "firing hook A completed while A's own lock was held — "
                "self-serialization is broken"
            )
        finally:
            release.set()
            t.join(5)
        probe_a.join(5)
        assert done_a.wait(5)

    def test_self_serialization_no_interleaved_enter(self):
        """T4 (nexus-eslkl): concurrent fires of the SAME hook must never
        interleave — no ``enter, enter`` pair without an intervening
        ``exit``. Deterministic via a barrier the hook body waits on
        (bounded rendezvous), not a sleep-based race window."""
        from nexus.hook_registry import HookRegistry, LockedHookRegistry

        registry = HookRegistry()
        ordinals: list[str] = []
        ordinals_lock = threading.Lock()
        in_hook = threading.Event()
        release = threading.Event()

        def hook(source_path, collection, content):
            with ordinals_lock:
                assert not in_hook.is_set(), "interleaved enter,enter observed"
                in_hook.set()
                ordinals.append(f"enter:{source_path}")
            release.wait(5)
            with ordinals_lock:
                ordinals.append(f"exit:{source_path}")
                in_hook.clear()

        registry.register_document(hook)
        locked = LockedHookRegistry(registry)

        # First thread enters and parks on `release`; the second must be
        # BLOCKED (not interleaved) until the first exits.
        t1 = threading.Thread(target=locked.fire_document, args=("/a", "col", "x"))
        t1.start()
        assert in_hook.wait(5), "first fire never entered the hook"

        entered_while_first_held = threading.Event()

        def second():
            locked.fire_document("/b", "col", "x")
            entered_while_first_held.set()

        t2 = threading.Thread(target=second)
        t2.start()
        # The second thread must NOT complete yet — it should be queued on
        # the same per-hook lock the first thread is holding.
        assert not entered_while_first_held.wait(0.2), (
            "second fire completed before the first released the lock"
        )

        release.set()
        t1.join(5)
        t2.join(5)
        assert entered_while_first_held.is_set()
        assert ordinals == ["enter:/a", "exit:/a", "enter:/b", "exit:/b"]

    def test_serialize_false_opts_out_of_the_lock_entirely(self):
        """A hook declaring ``serialize = False`` must run completely
        unlocked — concurrent fires of THAT hook may interleave (the
        opt-out is a no-op on locking, not a narrower lock).

        nexus-s3mhu (mutation-confirmed vacuous, FIXED): the original
        version asserted ``not errors`` where ``errors`` was populated
        only if ``locked.fire_batch(...)`` itself raised. But
        ``HookRegistry.fire_batch`` has its own per-hook
        ``except Exception: log + persist hook_failures`` wrapper that
        NEVER re-raises — so when the opt-out was broken (every hook
        always locks), thread 1 entered the hook and blocked forever on
        the barrier (thread 2 was queued behind the SAME lock and never
        reached its own ``barrier.wait()``); the eventual
        ``BrokenBarrierError`` was swallowed by ``fire_batch``, and
        ``errors`` stayed empty either way — genuinely-concurrent
        (correct) and serialized-then-silently-broken (should fail) were
        indistinguishable to that assertion. Falsified by deleting the
        opt-out check from ``LockedHookRegistry._invoke`` — the old
        version passed unmodified.

        Fixed by asserting on a signal ``fire_batch``'s exception
        swallowing CANNOT intercept: each thread records its own
        ``barrier.wait()`` outcome (rendezvous success, or the
        ``BrokenBarrierError`` itself) directly into a shared list FROM
        INSIDE the hook body, before control ever returns to
        ``fire_batch``. A silently-swallowed timeout now shows up as a
        recorded ``"broken"`` entry, not as an empty error list. Re-ran
        the same falsify-by-deleting-the-code check against this version:
        it correctly FAILS when the opt-out is removed.
        """
        from nexus.hook_registry import HookRegistry, LockedHookRegistry

        registry = HookRegistry()
        barrier = threading.Barrier(2, timeout=2)
        outcomes: list[str] = []
        outcomes_lock = threading.Lock()

        def unlocked_hook(ids, col, docs, embs, metas):
            # Only satisfiable AS "rendezvous" for BOTH threads if they
            # are inside the hook body AT THE SAME TIME — impossible if
            # this hook were still serialized under a lock (the second
            # thread would never reach barrier.wait() until the first's
            # 2s timeout breaks the barrier for both).
            try:
                barrier.wait()
                outcome = "rendezvous"
            except threading.BrokenBarrierError:
                outcome = "broken"
            with outcomes_lock:
                outcomes.append(outcome)
        unlocked_hook.batch_grain = "flush"
        unlocked_hook.serialize = False
        registry.register_batch(unlocked_hook)
        locked = LockedHookRegistry(registry)

        threads = [
            threading.Thread(
                target=locked.fire_batch,
                args=([f"d{i}"], "col", ["x"], None, [{}]),
                kwargs={"grain": "flush"},
            )
            for i in range(2)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(5)

        assert outcomes == ["rendezvous", "rendezvous"], (
            f"expected both threads to rendezvous concurrently, got "
            f"{outcomes} — a 'broken' entry means the barrier timed out, "
            "i.e. the hooks were actually serialized (the opt-out is not "
            "working)"
        )

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
