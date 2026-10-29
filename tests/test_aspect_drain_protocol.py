# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-108 Phase 1 S1 -- AspectWorker stop-and-drain protocol (nexus-he24).

Tests for:
  - AspectExtractionWorker.drain(timeout=...) drains and blocks
  - drain() raises DrainTimeoutError when stuck in_progress rows remain
  - drain() is idempotent on an already-drained queue
  - Stop signal prevents new claim_next calls
  - Restart (start() after drain) re-arms the worker for new claims
  - Worker-not-running edge case: drain() is just is_drained() once
  - Thread-join timeout warning (S-5, nexus-1091)
  - Default timeout is 120s (SIG-3, nexus-1091)
  - MCP-vs-CLI lock detection (SIG-5, nexus-1091)

Substrate (nexus-i711w Stage 2 sub-stage A): the SQLite
``AspectExtractionQueue`` died with the SQLite T2 stores; the queue is now
``HttpAspectQueue`` unconditionally, backed here by the hermetic engine
substrate (autouse ``_pin_t2_substrate`` -> ``t2_service_env``, fresh tenant
per test). The queue's own state-machine semantics (pending/in_progress/
failed vs. is_drained) are owned engine-side by
``service/src/test/java/dev/nexus/service/AspectRepositoryTest.java``; this
file tests the drain ORCHESTRATION in ``nexus.aspect_worker`` on top of it.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.db.t2 import T2Database

from tests._catalog_fixture_ops import register_real_doc_id
from tests._module_seam import patch_time

# hygiene-001-1 (nexus-tk070.p6a follow-on): aspect_extraction_queue.doc_id
# now carries a REAL FK to catalog_documents(tenant_id, tumbler) via the
# engine's POST /v1/aspects/queue/enqueue 400 -- see the identical cache in
# tests/test_document_aspects_store.py for the full rationale. One real
# registration per test, memoized on the test's freshly-minted
# NX_SERVICE_TOKEN.
_doc_id_cache: dict[str, str] = {}


def _shared_doc_id() -> str:
    key = os.environ.get("NX_SERVICE_TOKEN", "")
    if key not in _doc_id_cache:
        _doc_id_cache[key] = register_real_doc_id()
    return _doc_id_cache[key]


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
    """Vestigial local T2 path handed to T2Database / drain_worker.

    Post-i711w the aspect queue lives engine-side (``HttpAspectQueue``), so
    no schema initialisation happens here — the path only satisfies the
    ``queue_path`` parameter shape and the facade's local-path plumbing.
    """
    return tmp_path / "t2.db"


# TestIsDrained (the SQLite queue's own is_drained() state-machine semantics)
# died with the SQLite AspectExtractionQueue (nexus-i711w Stage 2 sub-stage A).
# The surviving contract is engine-side, pinned by AspectRepositoryTest.java
# (enqueue/claim/markFailed vs isDrained) and exercised end-to-end through the
# Python client by the TestDrain cases below against the hermetic engine.


# -- RDR-173 P4.1 (nexus-4st62): service-aware drain ---------------------------


@pytest.fixture
def _empty_tuple_side(monkeypatch):
    """The unit stubs below replace `HttpAspectQueue` and touch no engine; the
    tuple side (RDR-235) must not reach one either. Reads as an engine whose
    `aspects` subspace is empty."""
    from nexus.db.t2 import http_tuple_store as tmod

    class _Empty(_TupleSide):
        pass

    _Empty.answers = [_aspects_census()]
    monkeypatch.setattr(tmod, "HttpTupleStore", _Empty)


@pytest.mark.usefixtures("_empty_tuple_side")
class TestServiceModeDrain:
    """drain_worker must poll the SERVICE queue (HttpAspectQueue.is_drained()).

    Historically this class guarded the RDR-173 P4.1 routing seam (SERVICE
    backend -> HttpAspectQueue, never the local-sqlite queue). The seam is
    COLLAPSED (nexus-i711w): HttpAspectQueue is the only queue, so the
    negative guard is structurally impossible to violate; the poll/timeout/
    lock-skip contracts below are still live drain_worker behaviour."""

    def test_drain_polls_http_queue_in_service_mode(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        from nexus.aspect_worker import drain_worker
        from nexus.db.t2 import http_aspect_queue as hmod

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

        # queue_path is irrelevant in service mode; pass an unused path.
        drain_worker(tmp_path / "unused.db", _locks_dir=locks_dir, timeout=1.0)

        assert calls["http_built"] == 1, "service mode must construct HttpAspectQueue"
        assert calls["http_is_drained"] >= 1, "must poll the SERVICE queue's is_drained()"
        assert calls["http_closed"] is True, "must close the http queue"

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
        from nexus.db.t2 import http_aspect_queue as hmod

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
        and the stale-reclaim recovery so the operator knows the action.
        """
        from nexus.aspect_worker import DrainTimeoutError, drain_worker
        from nexus.db.t2 import http_aspect_queue as hmod


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
            f"error message must mention 'reclaim' (stale-reclaim recovery); got: {msg!r}"
        )

    def test_drain_poll_loop_exits_when_is_drained_becomes_true(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        """OBS-1 (poll loop): drain_worker must keep polling until is_drained()
        returns True — False x 2 then True must succeed without error."""
        from nexus.aspect_worker import drain_worker
        from nexus.db.t2 import http_aspect_queue as hmod

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
        """SIG-1 heritage: a foreign worker lock file must never block drain.

        On the service queue PG's FOR UPDATE SKIP LOCKED owns cross-process
        claim safety; the SQLite-era file-lock pre-check died with the
        =sqlite opt-out (RDR-158 P3, nexus-7bomn). This pins that a stray
        lock file (PID 1: always alive, always foreign) cannot re-block a
        drain if anyone reintroduces a lock check.
        """
        from nexus.aspect_worker import drain_worker
        from nexus.db.t2 import http_aspect_queue as hmod


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

        # Must NOT raise — lock files are ignored.
        drain_worker(tmp_path / "unused.db", _locks_dir=locks_dir, timeout=1.0)

    def test_cli_drain_surfaces_detail_hint_on_timeout(self, monkeypatch) -> None:
        """OBS / Medium-1: `nx aspects drain` must surface DrainTimeoutError.detail
        (the stale-reclaim hint) to the operator — not swallow it behind the
        generic 'Re-run...' message. The crashed-worker case (stuck_count==0
        with a detail hint) is exactly when the operator needs the hint.
        """
        from click.testing import CliRunner
        from nexus.aspect_worker import DrainTimeoutError
        from nexus.commands import aspects as aspects_mod

        hint = (
            "Note (service mode): pending_count is 0 ... a running "
            "aspect-worker's stale-reclaim loop resets them to pending."
        )

        def _raise(*a, **k):
            raise DrainTimeoutError(stuck_count=0, timeout=1.0, detail=hint)

        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: "/tmp/x.db")
        monkeypatch.setattr("nexus.aspect_worker.drain_worker", _raise)

        result = CliRunner().invoke(aspects_mod.aspects_drain, ["--timeout", "1"])
        assert result.exit_code == 1
        assert "stale-reclaim" in result.output, (
            f"CLI must surface the stale-reclaim hint; got: {result.output!r}"
        )


# -- Stop signal --------------------------------------------------------------


class TestStopSignal:
    @pytest.mark.t2_tenant
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

    def test_stop_claiming_is_idempotent(self) -> None:
        from nexus.aspect_worker import AspectExtractionWorker

        worker = AspectExtractionWorker(poll_interval=0.05)
        worker.stop_claiming()
        worker.stop_claiming()  # second call must not raise


# -- drain() ------------------------------------------------------------------


@pytest.mark.t2_tenant
class TestDrain:
    def test_drain_on_empty_queue_is_noop(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """Drain on already-drained queue returns immediately."""
        from nexus.aspect_worker import drain_worker

        # monkeypatch t2_ctx is unnecessary here -- we need a real queue
        with T2Database(queue_path) as db:
            db.aspect_queue.is_drained()  # just verifies it exists

        # nexus-scc9t: drain_worker's own is_drained() short-circuit
        # (src/nexus/aspect_worker.py: "if queue.is_drained(): ...
        # return") fires before the poll loop, so time.sleep(poll_interval)
        # is never called for an already-drained queue. Asserted directly
        # -- a loaded box can blow a wall-clock elapsed bound with no
        # regression present, since the assertion cares about REAL
        # scheduler-delay-inflated seconds rather than whether the
        # short-circuit engaged.
        with patch_time("nexus.aspect_worker", "sleep") as mock_sleep:
            drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)
        mock_sleep.assert_not_called()

    def test_drain_waits_for_in_progress_rows_to_resolve(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """drain() blocks until an in_progress row is marked_done."""
        from nexus.aspect_worker import drain_worker

        # Insert a pending row, claim it (make in_progress), then resolve
        # it from a background thread after a short delay.
        with T2Database(queue_path) as db:
            db.aspect_queue.enqueue("knowledge__test", "/async.pdf", doc_id=_shared_doc_id())
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
            db.aspect_queue.enqueue("knowledge__test", "/stuck.pdf", doc_id=_shared_doc_id())
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
            db.aspect_queue.enqueue("knowledge__test", "/fail.pdf", doc_id=_shared_doc_id())
            db.aspect_queue.claim_next()
            db.aspect_queue.mark_failed("knowledge__test", "/fail.pdf", "broken")

        # nexus-scc9t: see test_drain_on_empty_queue_is_noop -- a queue
        # with only terminal (failed) rows is_drained() immediately, so
        # the short-circuit fires and time.sleep is never called.
        with patch_time("nexus.aspect_worker", "sleep") as mock_sleep:
            drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)
        mock_sleep.assert_not_called()


# -- worker stop + drain integration ------------------------------------------


@pytest.mark.t2_tenant
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
                db.aspect_queue.enqueue("knowledge__test", "/new.pdf", doc_id=_shared_doc_id())

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
    @pytest.mark.t2_tenant
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
        from nexus.db.t2.http_aspect_queue import HttpAspectQueue

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
            # (HttpAspectQueue is the only queue post-i711w; drain_worker
            # constructs one internally, so a class-attribute patch reaches it.)
            _orig_is_drained = HttpAspectQueue.is_drained

            def _always_drained(self_):
                return True

            HttpAspectQueue.is_drained = _always_drained  # type: ignore[method-assign]

            try:
                drain_worker(
                    queue_path=queue_path,
                    timeout=5.0,
                    poll_interval=0.05,
                    _locks_dir=locks_dir,
                )
            finally:
                HttpAspectQueue.is_drained = _orig_is_drained  # type: ignore[method-assign]
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




# -- RDR-235 (nexus-5r2sc.3.4): drain counts the `aspects` tuple side too ------


def _aspects_census(**kw):
    from nexus.db.t2.records import SubspaceCensus

    base = dict(
        subspace="aspects", total=0, available=0, claimed=0, dead=0, consumed=0,
        expired_unpurged=0, oldest_created_at=None, newest_created_at=None, lapsed=0,
    )
    base.update(kw)
    return SubspaceCensus(**base)


class _TupleSide:
    """The `HttpTupleStore` stand-in drain_worker builds. `answers` is the
    census sequence one poll at a time (the last one repeats); an Exception
    instance in it is raised instead."""

    answers: list = []
    calls: int = 0
    closed: bool = False

    def __init__(self, *a, **k) -> None:
        pass

    def subspace_stats(self, subspace: str, **_kw):
        cls = type(self)
        i = min(cls.calls, len(cls.answers) - 1)
        cls.calls += 1
        out = cls.answers[i]
        if isinstance(out, Exception):
            raise out
        return out

    def close(self) -> None:
        type(self).closed = True


def _wire_sides(monkeypatch, *, table_drained, pending: int = 0, census=None):
    """Stub both sides. `table_drained` is a bool or a list of bools."""
    from nexus.db.t2 import http_aspect_queue as hmod
    from nexus.db.t2 import http_tuple_store as tmod

    answers = iter(table_drained) if not isinstance(table_drained, bool) else None

    class _Q:
        def __init__(self, *a, **k) -> None:
            pass

        def is_drained(self) -> bool:
            if answers is None:
                return table_drained
            return next(answers, True)

        def pending_count(self) -> int:
            return pending

        def close(self) -> None:
            pass

    class Side(_TupleSide):
        pass

    Side.answers = list(census) if isinstance(census, list) else [census or _aspects_census()]
    Side.calls = 0
    Side.closed = False
    monkeypatch.setattr(hmod, "HttpAspectQueue", _Q)
    monkeypatch.setattr(tmod, "HttpTupleStore", Side)
    return Side


class TestDrainCountsTheTupleSide:
    """The drained test is the table drained AND `available + claimed == 0` on
    `aspects`. Dead tasks never block (a failed row does not today). Backoff-
    delayed tasks count as available, which makes the gate conservative."""

    def _drain(self, tmp_path, locks_dir, timeout=0.2):
        from nexus.aspect_worker import drain_worker

        drain_worker(tmp_path / "unused.db", _locks_dir=locks_dir, timeout=timeout, poll_interval=0.02)

    @pytest.mark.parametrize("census", [
        {"available": 1, "total": 1},
        {"claimed": 1, "total": 1},
        {"available": 2, "claimed": 3, "total": 5},
    ])
    def test_a_drained_table_with_live_tasks_is_not_drained(
        self, monkeypatch, locks_dir: Path, tmp_path: Path, census,
    ) -> None:
        from nexus.aspect_worker import DrainTimeoutError

        _wire_sides(monkeypatch, table_drained=True, census=_aspects_census(**census))
        with pytest.raises(DrainTimeoutError) as exc:
            self._drain(tmp_path, locks_dir)
        assert exc.value.stuck_count == census.get("available", 0) + census.get("claimed", 0)

    def test_a_drained_tuple_side_with_a_busy_table_is_not_drained(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        from nexus.aspect_worker import DrainTimeoutError

        _wire_sides(monkeypatch, table_drained=False, pending=2)
        with pytest.raises(DrainTimeoutError) as exc:
            self._drain(tmp_path, locks_dir)
        assert exc.value.stuck_count == 2

    def test_dead_tasks_alone_do_not_block(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        side = _wire_sides(
            monkeypatch, table_drained=True, census=_aspects_census(dead=4, total=4),
        )
        self._drain(tmp_path, locks_dir)
        assert side.calls >= 1, "the tuple side must have been read"
        assert side.closed is True, "the tuple store must be closed"

    def test_both_sides_drain_during_the_poll(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        side = _wire_sides(
            monkeypatch, table_drained=[False, True],
            census=[_aspects_census(available=2, total=2), _aspects_census(claimed=1, total=1),
                    _aspects_census()],
        )
        self._drain(tmp_path, locks_dir, timeout=5.0)
        assert side.calls >= 3

    def test_an_engine_without_the_aspects_template_drains_on_the_table_alone(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        from nexus.db.t2.http_tuple_store import UnknownSubspaceError

        _wire_sides(monkeypatch, table_drained=True, census=UnknownSubspaceError("none"))
        self._drain(tmp_path, locks_dir)

    def test_a_tuple_read_that_fails_is_not_read_as_drained(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        _wire_sides(monkeypatch, table_drained=True, census=RuntimeError("503"))
        with pytest.raises(RuntimeError, match="503"):
            self._drain(tmp_path, locks_dir)

    def test_the_timeout_names_both_sides_and_reads_claimed_and_lapsed_for_tasks(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        from nexus.aspect_worker import DrainTimeoutError

        _wire_sides(
            monkeypatch, table_drained=False, pending=0,
            census=_aspects_census(available=3, claimed=2, lapsed=1, total=5),
        )
        with pytest.raises(DrainTimeoutError) as exc:
            self._drain(tmp_path, locks_dir)
        err = exc.value
        assert err.stuck_count == 5
        text = str(err)
        assert "aspects" in text and "claimed" in text and "lapsed" in text
        assert "in_progress" in text, "the crashed-worker hint for the table side stays"

    def test_the_table_side_message_is_unchanged_when_the_tuple_side_is_empty(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        from nexus.aspect_worker import DrainTimeoutError

        _wire_sides(monkeypatch, table_drained=False, pending=3)
        with pytest.raises(DrainTimeoutError) as exc:
            self._drain(tmp_path, locks_dir)
        assert exc.value.detail is None


@pytest.mark.t2_tenant
class TestDrainCliAgainstTheEngine:
    """Test Plan scenario 21, drain half: the old table holds pending and
    failed rows, `aspects` holds available, claimed and dead tasks, and
    `nx aspects drain` exits 0 only when both are drained."""

    _KEYS = {"queue": "aspects"}

    @pytest.fixture(autouse=True)
    def _cli(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: tmp_path / "t2.db")

    def _drain_cli(self) -> int:
        from click.testing import CliRunner
        from nexus.commands.aspects import aspects_group

        res = CliRunner().invoke(
            aspects_group, ["drain", "--timeout", "0.4", "--poll-interval", "0.05"],
        )
        return res.exit_code

    def _task(self, store) -> str:
        import uuid

        doc = f"9.{uuid.uuid4().int % 10**9}.1"
        return store.out("aspects", self._KEYS, None, "{}", nonce=doc)

    def test_exit_zero_only_when_both_sides_are_drained(self, queue_path: Path) -> None:
        from nexus.db.t2.http_tuple_store import HttpTupleStore

        store = HttpTupleStore()
        assert self._drain_cli() == 0, "empty on both sides"

        # A failed table row and a dead task never block.
        with T2Database(queue_path) as db:
            db.aspect_queue.enqueue("knowledge__d", "/f.pdf", doc_id=_shared_doc_id())
            assert db.aspect_queue.claim_next() is not None
            db.aspect_queue.mark_failed("knowledge__d", "/f.pdf", "boom")
        self._task(store)
        got = store.inp("aspects", self._KEYS, claimant="drain-dead", lease_s=60)
        store.nack(got[1], "drain-dead", dead=True)
        census = store.subspace_stats("aspects")
        assert (census.dead, census.available, census.claimed) == (1, 0, 0)
        assert self._drain_cli() == 0, "failed row + dead task only"

        # An available task blocks it.
        self._task(store)
        assert self._drain_cli() == 1, "available task"

        # A claimed task blocks it.
        got = store.inp("aspects", self._KEYS, claimant="drain-live", lease_s=60)
        assert got is not None
        assert self._drain_cli() == 1, "claimed task"

        # Acked, it is drained.
        store.ack(got[1], "drain-live")
        assert self._drain_cli() == 0, "acked"
