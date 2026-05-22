# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-108 Phase 1 S1 -- AspectWorker stop-and-drain protocol (nexus-he24).

Tests for:
  - AspectExtractionQueue.is_drained() precondition check
  - AspectExtractionWorker.drain(timeout=...) drains and blocks
  - drain() raises DrainTimeoutError when stuck in_progress rows remain
  - drain() is idempotent on an already-drained queue
  - Stop signal prevents new claim_next calls
  - Restart (start() after drain) re-arms the worker for new claims
  - Worker-not-running edge case: drain() is just is_drained() once
  - Thread-join timeout warning (S-5, nexus-1091)
  - Default timeout is 120s (SIG-3, nexus-1091)
  - MCP-vs-CLI lock detection (SIG-5, nexus-1091)
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


# nexus-9eaz family: a small set of threading/concurrency tests pass
# consistently in isolation and on local machines but fail intermittently
# on GitHub Actions Ubuntu runners under shared-runner pressure. The race
# is correctly diagnosed as a CI infrastructure artefact (see nexus-9eaz
# bead notes for the full dose-response evidence: pass at 0-3 prior test
# files, fail at 6+). The lock and stop-signal semantics are verified
# correct under load locally. Standing mitigation: skip on GHA by default;
# opt in via NEXUS_RUN_FLAKY_TESTS=1 to run the test in CI (used by the
# isolation-runner race-probe pattern).
_GHA_FLAKE_SKIP_REASON = (
    "GHA-runner pressure flake (nexus-9eaz family). Passes locally and "
    "in isolation; opt in with NEXUS_RUN_FLAKY_TESTS=1."
)
_skip_on_gha_flake = pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true"
    and not os.environ.get("NEXUS_RUN_FLAKY_TESTS"),
    reason=_GHA_FLAKE_SKIP_REASON,
)


# -- Shared fixtures ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_worker():
    """Tear down the worker singleton between tests."""
    from nexus.aspect_worker import reset_worker_for_tests

    reset_worker_for_tests()
    yield
    reset_worker_for_tests()


@pytest.fixture()
def locks_dir(tmp_path: Path) -> Path:
    """Isolated lock file directory so tests do not interact with real MCP
    worker lock files under ~/.config/nexus/locks/.

    All drain_worker calls that are not specifically testing lock-file
    behaviour must pass this directory via _locks_dir= so they skip any
    live MCP process lock that may exist on the developer's machine.
    """
    d = tmp_path / "locks"
    d.mkdir()
    return d


@pytest.fixture()
def queue_path(tmp_path: Path) -> Path:
    """Return a tmp SQLite path with the schema initialised."""
    from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

    q = AspectExtractionQueue(tmp_path / "t2.db")
    q.close()
    return tmp_path / "t2.db"


@pytest.fixture()
def queue(queue_path: Path):
    from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

    q = AspectExtractionQueue(queue_path)
    yield q
    q.close()


# -- is_drained() -------------------------------------------------------------


class TestIsDrained:
    def test_empty_queue_is_drained(self, queue) -> None:
        assert queue.is_drained() is True

    def test_pending_row_is_not_drained(self, queue) -> None:
        queue.enqueue("knowledge__test", "/doc1.pdf")
        assert queue.is_drained() is False

    def test_in_progress_row_is_not_drained(self, queue) -> None:
        queue.enqueue("knowledge__test", "/doc1.pdf")
        queue.claim_next()
        # Row is now in_progress
        assert queue.is_drained() is False

    def test_failed_row_counts_as_drained(self, queue) -> None:
        """Failed rows are terminal -- drain treats them as resolved."""
        queue.enqueue("knowledge__test", "/doc1.pdf")
        queue.claim_next()
        queue.mark_failed("knowledge__test", "/doc1.pdf", "boom")
        assert queue.is_drained() is True

    def test_mixed_failed_and_pending_is_not_drained(self, queue) -> None:
        queue.enqueue("knowledge__test", "/doc1.pdf")
        queue.enqueue("knowledge__test", "/doc2.pdf")
        queue.claim_next()
        queue.mark_failed("knowledge__test", "/doc1.pdf", "boom")
        # doc2 is still pending
        assert queue.is_drained() is False

    def test_only_failed_rows_is_drained(self, queue) -> None:
        queue.enqueue("knowledge__test", "/doc1.pdf")
        queue.enqueue("knowledge__test", "/doc2.pdf")
        queue.claim_next()
        queue.mark_failed("knowledge__test", "/doc1.pdf", "err1")
        queue.claim_next()
        queue.mark_failed("knowledge__test", "/doc2.pdf", "err2")
        assert queue.is_drained() is True


# -- Stop signal --------------------------------------------------------------


class TestStopSignal:
    def test_stop_claiming_on_running_worker_causes_exit(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """After stop_claiming() on a running worker, the thread exits promptly."""
        import nexus.mcp_infra as infra_mod
        from nexus.aspect_worker import AspectExtractionWorker

        original_t2_ctx = infra_mod.t2_ctx
        infra_mod.t2_ctx = lambda: T2Database(queue_path)

        try:
            worker = AspectExtractionWorker(poll_interval=0.05)
            worker.start()
            assert worker.is_running()

            worker.stop_claiming()

            # Thread should exit within one poll interval
            thread = worker._thread
            if thread is not None:
                thread.join(timeout=1.0)
                assert not thread.is_alive(), (
                    "worker thread should have exited after stop_claiming()"
                )
        finally:
            infra_mod.t2_ctx = original_t2_ctx

    def test_stop_claiming_is_idempotent(self, queue) -> None:
        from nexus.aspect_worker import AspectExtractionWorker

        worker = AspectExtractionWorker(poll_interval=0.05)
        worker.stop_claiming()
        worker.stop_claiming()  # second call must not raise


# -- drain() ------------------------------------------------------------------


class TestDrain:
    def test_drain_on_empty_queue_is_noop(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """Drain on already-drained queue returns immediately."""
        from nexus.aspect_worker import drain_worker

        # monkeypatch t2_ctx is unnecessary here -- we need a real queue
        with T2Database(queue_path) as db:
            db.aspect_queue.is_drained()  # just verifies it exists

        # Queue is empty; drain should return without error.
        start = time.monotonic()
        drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, "drain on empty queue took too long"

    def test_drain_waits_for_in_progress_rows_to_resolve(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """drain() blocks until an in_progress row is marked_done."""
        from nexus.aspect_worker import drain_worker

        # Insert a pending row, claim it (make in_progress), then resolve
        # it from a background thread after a short delay.
        with T2Database(queue_path) as db:
            db.aspect_queue.enqueue("knowledge__test", "/async.pdf")
            db.aspect_queue.claim_next()

        def resolve_after_delay():
            time.sleep(0.2)
            with T2Database(queue_path) as db:
                db.aspect_queue.mark_done("knowledge__test", "/async.pdf")

        t = threading.Thread(target=resolve_after_delay, daemon=True)
        t.start()

        drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)
        t.join()

        with T2Database(queue_path) as db:
            assert db.aspect_queue.is_drained()

    def test_drain_raises_on_timeout(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """drain() raises DrainTimeoutError if stuck in_progress exceeds timeout.

        SG-2 (nexus-1091): increased margins to 1.0s timeout / 0.05s poll so
        this test is not flaky on loaded CI.
        """
        from nexus.aspect_worker import DrainTimeoutError, drain_worker

        with T2Database(queue_path) as db:
            db.aspect_queue.enqueue("knowledge__test", "/stuck.pdf")
            db.aspect_queue.claim_next()
            # Never mark_done -- row stays in_progress

        with pytest.raises(DrainTimeoutError) as exc_info:
            drain_worker(
                queue_path=queue_path,
                timeout=1.0,
                poll_interval=0.05,
                _locks_dir=locks_dir,
            )

        assert "stuck" in str(exc_info.value).lower() or "timeout" in str(exc_info.value).lower()

    def test_drain_idempotent_on_already_drained(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """drain() on an already-drained queue is a no-op (no error)."""
        from nexus.aspect_worker import drain_worker

        drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)
        drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)  # second call safe

    def test_drain_with_only_failed_rows_returns_immediately(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """Failed rows are considered terminal; drain treats them as done."""
        from nexus.aspect_worker import drain_worker

        with T2Database(queue_path) as db:
            db.aspect_queue.enqueue("knowledge__test", "/fail.pdf")
            db.aspect_queue.claim_next()
            db.aspect_queue.mark_failed("knowledge__test", "/fail.pdf", "broken")

        start = time.monotonic()
        drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0


# -- worker stop + drain integration ------------------------------------------


class TestWorkerDrainIntegration:
    def test_drain_worker_stops_worker_and_waits(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """drain_worker() stops the singleton worker and waits for queue empty."""
        import nexus.mcp_infra as infra_mod
        from nexus.aspect_worker import drain_worker, ensure_worker_started

        original_t2_ctx = infra_mod.t2_ctx
        infra_mod.t2_ctx = lambda: T2Database(queue_path)

        try:
            worker = ensure_worker_started(poll_interval=0.05, _locks_dir=locks_dir)
            assert worker.is_running()

            drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)

            # Worker thread should be stopped
            assert not worker.is_running()
        finally:
            infra_mod.t2_ctx = original_t2_ctx

    def test_restart_after_drain_allows_claiming(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """After drain, calling start() re-arms the worker to process new rows.

        SG-1 fix (nexus-1091): replaces the vacuous ``or True`` assertion with
        a real verification that the enqueued row was actually claimed (removed
        from the pending queue) within a polling window.
        """
        import nexus.aspect_extractor as extractor_mod
        import nexus.mcp_infra as infra_mod
        from nexus.aspect_worker import drain_worker, ensure_worker_started

        original_t2_ctx = infra_mod.t2_ctx
        original_extract = extractor_mod.extract_aspects
        infra_mod.t2_ctx = lambda: T2Database(queue_path)
        # Stub extraction: return None (unsupported-collection short-circuit)
        # so the worker calls mark_done without real Claude calls.
        extractor_mod.extract_aspects = lambda **_kw: None

        try:
            worker = ensure_worker_started(poll_interval=0.05, _locks_dir=locks_dir)
            drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)
            assert not worker.is_running()

            # Restart -- must clear the stop signal
            worker.start()
            assert worker.is_running()
            assert not worker.is_claiming_stopped(), (
                "restart must clear the stop signal so the worker can claim rows"
            )

            # Enqueue a new row after restart
            with T2Database(queue_path) as db:
                db.aspect_queue.enqueue("knowledge__test", "/new.pdf")

            # Poll until the queue is drained (row claimed + marked_done)
            # or 3 seconds pass.
            deadline = time.monotonic() + 3.0
            drained = False
            while time.monotonic() < deadline:
                time.sleep(0.05)
                with T2Database(queue_path) as db:
                    if db.aspect_queue.is_drained():
                        drained = True
                        break

            assert drained, (
                "worker restarted after drain must claim and process the new row "
                "within 3 seconds"
            )
        finally:
            infra_mod.t2_ctx = original_t2_ctx
            extractor_mod.extract_aspects = original_extract

    def test_worker_not_running_drain_is_just_is_drained(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """When no worker thread is alive, drain() is a simple is_drained() check."""
        from nexus.aspect_worker import drain_worker

        # No worker started -- queue is empty
        drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)  # must not raise

    def test_stop_claiming_then_start_resumes_claiming(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """stop_claiming + start() allows the worker to claim again."""
        import nexus.mcp_infra as infra_mod
        from nexus.aspect_worker import AspectExtractionWorker

        original_t2_ctx = infra_mod.t2_ctx
        infra_mod.t2_ctx = lambda: T2Database(queue_path)

        try:
            worker = AspectExtractionWorker(poll_interval=0.05)
            worker.start()
            assert worker.is_running()

            worker.stop_claiming()
            assert worker.is_claiming_stopped()

            # Wait for thread to exit
            thread = worker._thread
            if thread is not None:
                thread.join(timeout=1.0)

            # Restart: start() clears _stop_event and spawns a new thread
            worker.start()
            assert not worker.is_claiming_stopped()
            assert worker.is_running()

            worker.stop(timeout=1.0)
        finally:
            infra_mod.t2_ctx = original_t2_ctx


# -- Thread-join timeout warning (S-5) ----------------------------------------


class TestThreadJoinTimeoutWarning:
    def test_join_timeout_emits_warning_log(
        self, queue_path: Path, locks_dir: Path, capsys
    ) -> None:
        """drain_worker logs a warning when the worker thread does not exit
        within the 2-second join window.

        S-5 (nexus-1091): the join timeout was silent; the operator had no
        signal that the thread was stuck after stop_claiming().

        structlog is configured with ConsoleRenderer (stdout), so we capture
        via capsys rather than caplog.
        """
        import nexus.mcp_infra as infra_mod
        from nexus.aspect_worker import drain_worker, ensure_worker_started
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        original_t2_ctx = infra_mod.t2_ctx
        infra_mod.t2_ctx = lambda: T2Database(queue_path)

        try:
            worker = ensure_worker_started(poll_interval=0.05, _locks_dir=locks_dir)

            # Replace the worker thread with one that never exits until
            # the barrier is released.
            barrier = threading.Event()

            def _hang():
                barrier.wait(timeout=10.0)

            slow_thread = threading.Thread(target=_hang, daemon=True)
            slow_thread.start()

            with worker._lock:
                # Swap in the slow thread so drain_worker picks it up for
                # the 2s join.
                worker._thread = slow_thread

            # Patch the queue to appear drained immediately so drain_worker
            # skips the polling loop and goes straight to the thread-join.
            _orig_is_drained = AspectExtractionQueue.is_drained

            def _always_drained(self_):
                return True

            AspectExtractionQueue.is_drained = _always_drained  # type: ignore[method-assign]

            try:
                drain_worker(
                    queue_path=queue_path,
                    timeout=5.0,
                    poll_interval=0.05,
                    _locks_dir=locks_dir,
                )
            finally:
                AspectExtractionQueue.is_drained = _orig_is_drained  # type: ignore[method-assign]
                barrier.set()  # Let the hanging thread exit

        finally:
            infra_mod.t2_ctx = original_t2_ctx

        # S-5: drain_worker must emit a warning event via structlog (routed
        # to stdout via ConsoleRenderer in the test environment).
        captured = capsys.readouterr()
        assert "drain_worker_thread_join_timeout" in captured.out, (
            "drain_worker must emit a 'drain_worker_thread_join_timeout' warning "
            "when the worker thread does not exit within 2 seconds. "
            f"stdout was: {captured.out!r}"
        )


# -- Default timeout (SIG-3) --------------------------------------------------


class TestDrainTimeoutDefault:
    def test_drain_worker_default_timeout_is_120s(self) -> None:
        """drain_worker default timeout must be 120s (SIG-3, nexus-1091).

        RDR-089 P1.3 measured ~26.5s median extraction time with a tail
        to 90s for scholarly-paper-v1 extractor. A 30s default is too
        tight; 120s (4x median) provides adequate margin.
        """
        import inspect

        from nexus.aspect_worker import drain_worker

        sig = inspect.signature(drain_worker)
        default_timeout = sig.parameters["timeout"].default
        assert default_timeout == 120.0, (
            f"drain_worker timeout default must be 120.0s (SIG-3), got {default_timeout!r}"
        )


# -- MCP-vs-CLI lock detection (SIG-5) ----------------------------------------


class TestMCPLockDetection:
    def test_drain_raises_when_mcp_lock_file_present(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """drain_worker raises DrainBlockedByActiveWorker when an active
        MCP process holds an aspect_worker lock file.

        SIG-5 (nexus-1091): drain is process-local. If an MCP server is
        running a worker in another process, a CLI-invoked migration that
        drains in its own process will not drain the MCP worker's queue
        rows. The lock file detects this cross-process conflict and
        surfaces operator guidance.

        PID 1 (init / launchd) is always alive on any Unix system and is
        guaranteed to be a different process from the test runner, making
        it the correct stand-in for a live MCP process.
        """
        from nexus.aspect_worker import DrainBlockedByActiveWorker, drain_worker

        # PID 1 is always alive and always a different process.
        mcp_pid = 1
        lock_file = locks_dir / f"aspect_worker.{mcp_pid}"
        lock_file.write_text(str(mcp_pid))

        with pytest.raises(DrainBlockedByActiveWorker) as exc_info:
            drain_worker(
                queue_path=queue_path,
                timeout=5.0,
                _locks_dir=locks_dir,
            )

        err_str = str(exc_info.value)
        assert str(mcp_pid) in err_str, (
            "DrainBlockedByActiveWorker must include the blocking PID"
        )

    def test_drain_ignores_stale_lock_file_for_dead_pid(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """A lock file for a PID that no longer exists is treated as stale
        and removed; drain proceeds normally.
        """
        from nexus.aspect_worker import drain_worker

        # PID 99999999 exceeds the Linux kernel's PID limit (~4M) and is
        # effectively guaranteed not to exist.
        fake_pid = 99999999
        lock_file = locks_dir / f"aspect_worker.{fake_pid}"
        lock_file.write_text(str(fake_pid))

        # drain_worker must NOT raise -- stale lock is cleaned up.
        drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)

        # Stale lock file should be removed.
        assert not lock_file.exists(), (
            "drain_worker must remove stale lock files for dead PIDs"
        )

    def test_drain_no_lock_dir_proceeds_normally(
        self, queue_path: Path
    ) -> None:
        """When _locks_dir does not exist, drain proceeds without error.
        Supports environments where the locks dir has not been created.
        """
        from nexus.aspect_worker import drain_worker

        nonexistent = Path("/tmp/__nexus_locks_nonexistent_12345__")
        drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=nonexistent)

    def test_drain_skips_own_pid_lock_file(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """A lock file for the current process's own PID is not a
        cross-process conflict; drain proceeds normally.

        When drain_worker is called from within the same process that
        started the worker (e.g., during testing or from within the MCP
        process itself), the own-PID lock must not trigger
        DrainBlockedByActiveWorker.
        """
        import os

        from nexus.aspect_worker import drain_worker

        own_pid = os.getpid()
        lock_file = locks_dir / f"aspect_worker.{own_pid}"
        lock_file.write_text(str(own_pid))

        # Must not raise -- own-PID lock is skipped.
        drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)
