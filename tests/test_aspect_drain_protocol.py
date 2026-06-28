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


# nexus-9eaz family flake-skip helper retired 2026-05-22: RDR-120 P3b
# made the daemon the sole apply_pending caller, removing the
# cross-process migration race surface these tests were guarding.


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


# -- RDR-173 P4.1 (nexus-4st62): service-aware drain ---------------------------


class TestServiceModeDrain:
    """drain_worker must poll the SERVICE queue (HttpAspectQueue.is_drained())
    when the aspect-queue backend is SERVICE — not a local-sqlite
    AspectExtractionQueue, which is empty/stale in service mode and yields a
    spurious 'drained' (the nx aspects drain / migration-gate inertness gap)."""

    def test_drain_polls_http_queue_in_service_mode(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        from nexus.aspect_worker import drain_worker
        from nexus.db import storage_mode as smod
        from nexus.db.t2 import http_aspect_queue as hmod
        from nexus.db.t2 import aspect_extraction_queue as sqmod

        monkeypatch.setattr(
            smod, "storage_backend_for",
            lambda key: smod.StorageBackend.SERVICE
            if key == "aspect_queue" else smod.StorageBackend.SQLITE,
        )
        calls = {"http_is_drained": 0, "http_closed": False, "http_built": 0}

        class _FakeHttpQueue:
            def __init__(self, *a, **k) -> None:
                calls["http_built"] += 1

            def is_drained(self) -> bool:
                calls["http_is_drained"] += 1
                return True

            def close(self) -> None:
                calls["http_closed"] = True

        monkeypatch.setattr(hmod, "HttpAspectQueue", _FakeHttpQueue)
        # Guard: the local sqlite queue must NOT be opened in service mode.
        def _boom(*a, **k):
            raise AssertionError("service mode must not open local AspectExtractionQueue")
        monkeypatch.setattr(sqmod, "AspectExtractionQueue", _boom)

        # queue_path is irrelevant in service mode; pass an unused path.
        drain_worker(tmp_path / "unused.db", _locks_dir=locks_dir, timeout=1.0)

        assert calls["http_built"] == 1, "service mode must construct HttpAspectQueue"
        assert calls["http_is_drained"] >= 1, "must poll the SERVICE queue's is_drained()"
        assert calls["http_closed"] is True, "must close the http queue"

    def test_only_failed_rows_is_drained(self, queue) -> None:
        queue.enqueue("knowledge__test", "/doc1.pdf")
        queue.enqueue("knowledge__test", "/doc2.pdf")
        queue.claim_next()
        queue.mark_failed("knowledge__test", "/doc1.pdf", "err1")
        queue.claim_next()
        queue.mark_failed("knowledge__test", "/doc2.pdf", "err2")
        assert queue.is_drained() is True

    def test_drain_timeout_raises_DrainTimeoutError_with_pending_count(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        """OBS-1 (timeout path): when is_drained() never returns True, drain_worker
        must raise DrainTimeoutError with stuck_count from pending_count() -- NOT
        AttributeError from queue.conn (HttpAspectQueue has no .conn).

        This is the CRITICAL fix: the pre-fix code called queue.conn.execute(...)
        unconditionally, which raises AttributeError on HttpAspectQueue.
        """
        from nexus.aspect_worker import DrainTimeoutError, drain_worker
        from nexus.db import storage_mode as smod
        from nexus.db.t2 import http_aspect_queue as hmod
        from nexus.db.t2 import aspect_extraction_queue as sqmod

        monkeypatch.setattr(
            smod, "storage_backend_for",
            lambda key: smod.StorageBackend.SERVICE
            if key == "aspect_queue" else smod.StorageBackend.SQLITE,
        )
        _STUCK = 3

        class _FakeHttpQueue:
            def __init__(self, *a, **k) -> None:
                pass

            def is_drained(self) -> bool:
                return False  # never drains -> timeout fires

            def pending_count(self) -> int:
                return _STUCK

            def close(self) -> None:
                pass

        monkeypatch.setattr(hmod, "HttpAspectQueue", _FakeHttpQueue)
        def _boom(*a, **k):
            raise AssertionError("service mode must not open local AspectExtractionQueue")
        monkeypatch.setattr(sqmod, "AspectExtractionQueue", _boom)

        with pytest.raises(DrainTimeoutError) as exc_info:
            drain_worker(
                tmp_path / "unused.db",
                _locks_dir=locks_dir,
                timeout=0.15,
                poll_interval=0.05,
            )

        err = exc_info.value
        assert err.stuck_count == _STUCK, (
            f"stuck_count must come from pending_count() ({_STUCK}), got {err.stuck_count}"
        )
        # Normal timeout path (pending > 0) must NOT attach the in_progress hint.
        assert err.detail is None, (
            f"detail must be None on the normal (pending>0) timeout path; got {err.detail!r}"
        )

    def test_drain_timeout_in_progress_only_gives_honest_message(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        """Service-mode timeout with pending_count()==0 but is_drained()==False.

        The common crashed-worker scenario: all rows are in_progress (not
        pending), so pending_count() returns 0 while is_drained() is still
        False.  DrainTimeoutError must still be raised (timeout fires
        correctly), and the error message / detail must mention 'in_progress'
        and 'reclaim-stale' so the operator knows the recovery action.
        """
        from nexus.aspect_worker import DrainTimeoutError, drain_worker
        from nexus.db import storage_mode as smod
        from nexus.db.t2 import http_aspect_queue as hmod
        from nexus.db.t2 import aspect_extraction_queue as sqmod

        monkeypatch.setattr(
            smod, "storage_backend_for",
            lambda key: smod.StorageBackend.SERVICE
            if key == "aspect_queue" else smod.StorageBackend.SQLITE,
        )

        class _FakeHttpQueueInProgressOnly:
            def __init__(self, *a, **k) -> None:
                pass

            def is_drained(self) -> bool:
                return False  # rows stuck in in_progress -> never drains

            def pending_count(self) -> int:
                return 0  # no pending rows; all stuck in in_progress

            def close(self) -> None:
                pass

        monkeypatch.setattr(hmod, "HttpAspectQueue", _FakeHttpQueueInProgressOnly)
        def _boom(*a, **k):
            raise AssertionError("service mode must not open local AspectExtractionQueue")
        monkeypatch.setattr(sqmod, "AspectExtractionQueue", _boom)

        with pytest.raises(DrainTimeoutError) as exc_info:
            drain_worker(
                tmp_path / "unused.db",
                _locks_dir=locks_dir,
                timeout=0.15,
                poll_interval=0.05,
            )

        err = exc_info.value
        assert err.stuck_count == 0, (
            f"stuck_count must be 0 (pending_count() returns 0), got {err.stuck_count}"
        )
        msg = str(err)
        assert "in_progress" in msg, (
            f"error message must mention 'in_progress' for the crashed-worker hint; got: {msg!r}"
        )
        assert "reclaim" in msg, (
            f"error message must mention 'reclaim' (reclaim-stale recovery); got: {msg!r}"
        )

    def test_drain_poll_loop_exits_when_is_drained_becomes_true(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        """OBS-1 (poll loop): drain_worker must keep polling until is_drained()
        returns True — False x 2 then True must succeed without error."""
        from nexus.aspect_worker import drain_worker
        from nexus.db import storage_mode as smod
        from nexus.db.t2 import http_aspect_queue as hmod
        from nexus.db.t2 import aspect_extraction_queue as sqmod

        monkeypatch.setattr(
            smod, "storage_backend_for",
            lambda key: smod.StorageBackend.SERVICE
            if key == "aspect_queue" else smod.StorageBackend.SQLITE,
        )
        _answers = iter([False, False, True])

        class _FakeHttpQueue:
            def __init__(self, *a, **k) -> None:
                pass

            def is_drained(self) -> bool:
                try:
                    return next(_answers)
                except StopIteration:
                    return True  # safety valve

            def close(self) -> None:
                pass

        monkeypatch.setattr(hmod, "HttpAspectQueue", _FakeHttpQueue)
        def _boom(*a, **k):
            raise AssertionError("service mode must not open local AspectExtractionQueue")
        monkeypatch.setattr(sqmod, "AspectExtractionQueue", _boom)

        # Must NOT raise: the loop should see False, False, True and return.
        drain_worker(
            tmp_path / "unused.db",
            _locks_dir=locks_dir,
            timeout=5.0,
            poll_interval=0.05,
        )

    def test_drain_skips_mcp_lock_check_in_service_mode(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        """SIG-1: the MCP file-lock check must be skipped in SERVICE mode.

        In service mode the MCP process writes a local aspect_worker lock via
        ensure_worker_started. If _check_mcp_worker_lock ran, drain_worker would
        always raise DrainBlockedByActiveWorker while the MCP server is running,
        defeating the primary migration use case.

        Write a PID-1 lock file (always-alive, always-foreign) — in local mode
        this would block drain. In SERVICE mode drain must succeed.
        """
        from nexus.aspect_worker import drain_worker
        from nexus.db import storage_mode as smod
        from nexus.db.t2 import http_aspect_queue as hmod
        from nexus.db.t2 import aspect_extraction_queue as sqmod

        monkeypatch.setattr(
            smod, "storage_backend_for",
            lambda key: smod.StorageBackend.SERVICE
            if key == "aspect_queue" else smod.StorageBackend.SQLITE,
        )

        # Place a PID-1 lock file — would block drain in LOCAL mode.
        (locks_dir / "aspect_worker.1").write_text("1")

        class _FakeHttpQueue:
            def __init__(self, *a, **k) -> None:
                pass

            def is_drained(self) -> bool:
                return True

            def close(self) -> None:
                pass

        monkeypatch.setattr(hmod, "HttpAspectQueue", _FakeHttpQueue)
        def _boom(*a, **k):
            raise AssertionError("service mode must not open local AspectExtractionQueue")
        monkeypatch.setattr(sqmod, "AspectExtractionQueue", _boom)

        # Must NOT raise DrainBlockedByActiveWorker — lock is ignored in SERVICE mode.
        drain_worker(tmp_path / "unused.db", _locks_dir=locks_dir, timeout=1.0)

    def test_cli_drain_surfaces_detail_hint_on_timeout(self, monkeypatch) -> None:
        """OBS / Medium-1: `nx aspects drain` must surface DrainTimeoutError.detail
        (the reclaim-stale hint) to the operator — not swallow it behind the
        generic 'Re-run...' message. The crashed-worker case (stuck_count==0
        with a detail hint) is exactly when the operator needs the hint.
        """
        from click.testing import CliRunner
        from nexus.aspect_worker import DrainTimeoutError
        from nexus.commands import aspects as aspects_mod

        hint = (
            "Note (service mode): pending_count is 0 ... Run "
            "'nx aspects reclaim-stale' to reset them back to pending."
        )

        def _raise(*a, **k):
            raise DrainTimeoutError(stuck_count=0, timeout=1.0, detail=hint)

        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: "/tmp/x.db")
        monkeypatch.setattr("nexus.aspect_worker.drain_worker", _raise)

        result = CliRunner().invoke(aspects_mod.aspects_drain, ["--timeout", "1"])
        assert result.exit_code == 1
        assert "reclaim-stale" in result.output, (
            f"CLI must surface the reclaim-stale hint; got: {result.output!r}"
        )


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
        original_t2_index_write = infra_mod.t2_index_write
        original_extract = extractor_mod.extract_aspects
        infra_mod.t2_ctx = lambda: T2Database(queue_path)
        # RDR-128 P3: the worker's hot poll (reclaim + claim) now routes
        # through t2_index_write; point it at the test queue DB so the
        # restarted worker claims the row enqueued below (no daemon in tests).
        def _direct_poll(write_fn):  # noqa: ANN001
            with T2Database(queue_path) as db:
                return write_fn(db)
        infra_mod.t2_index_write = _direct_poll
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
            infra_mod.t2_index_write = original_t2_index_write
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
