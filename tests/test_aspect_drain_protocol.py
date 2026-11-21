# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-108 Phase 1 S1 -- AspectWorker stop-and-drain protocol (nexus-he24).

Tests for:
  - AspectExtractionWorker.drain(timeout=...) drains and blocks
  - drain() raises DrainTimeoutError when aspects tasks remain (available or claimed)
  - drain() is idempotent on an already-drained queue
  - Stop signal prevents new claims
  - Restart (start() after drain) re-arms the worker for new claims
  - Worker-not-running edge case: drain() is one census read
  - Thread-join timeout warning (S-5, nexus-1091)
  - Default timeout is 120s (SIG-3, nexus-1091)
  - MCP-vs-CLI lock detection (SIG-5, nexus-1091)

The queue is the engine's ``aspects`` tuple subspace (RDR-235), read through
``HttpTupleStore`` by ``drain_worker``. Tests here run against the hermetic
engine substrate (autouse ``_pin_t2_substrate`` -> ``t2_service_env``, fresh
tenant per test), or against a stub tuple side where a test pins a read shape
the engine cannot be made to produce cheaply. The queue's own state machine
(available/claimed/dead) is owned engine-side by
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
from tests._settle import settle


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


def _claim_one(claimant: str):
    """Claim the oldest available ``aspects`` task: ``(row, claim_id)`` or None."""
    from nexus.db.t2.http_tuple_store import HttpTupleStore

    tuples = HttpTupleStore()
    try:
        return tuples.inp("aspects", {"queue": "aspects"}, claimant=claimant, lease_s=60)
    finally:
        tuples.close()


def _settle(claim_id: str, claimant: str, *, dead: bool = False) -> None:
    """Ack a claimed task, or nack it dead."""
    from nexus.db.t2.http_tuple_store import HttpTupleStore

    tuples = HttpTupleStore()
    try:
        if dead:
            tuples.nack(claim_id, claimant, dead=True)
        else:
            tuples.ack(claim_id, claimant)
    finally:
        tuples.close()


def _aspects_live() -> int:
    """available + claimed on ``aspects``, read the way drain_worker reads it."""
    from nexus.db.t2.http_aspect_queue import aspects_census
    from nexus.db.t2.http_tuple_store import HttpTupleStore

    tuples = HttpTupleStore()
    try:
        census = aspects_census(tuples)
    finally:
        tuples.close()
    return 0 if census is None else census.available + census.claimed


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
    """The unit stubs below touch no engine; the
    tuple side (RDR-235) must not reach one either. Reads as an engine whose
    `aspects` subspace is empty."""
    from nexus.db.t2 import http_tuple_store as tmod

    class _Empty(_TupleSide):
        pass

    _Empty.answers = [_aspects_census()]
    monkeypatch.setattr(tmod, "HttpTupleStore", _Empty)


@pytest.mark.usefixtures("_empty_tuple_side")
class TestServiceModeDrain:
    """drain_worker's poll, timeout and lock-skip contracts, on a stub tuple side.

    The census is read through HttpTupleStore, which ``_empty_tuple_side``
    replaces with an empty ``aspects`` subspace. The queue-routing seam
    (RDR-173 P4.1) died with the table (nexus-hbnml); the contracts below are
    still live drain_worker behaviour."""

    def test_a_thread_the_drain_cannot_join_stays_on_record_so_start_refuses_beside_it(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        """nexus-22out: drain dropped a thread that outlived its 2 s join from every record, so a later start()
        cleared the shared stop event under it and two loops claimed. It is kept, as after a timed-out stop()."""
        import threading

        from nexus import aspect_worker
        from nexus.aspect_worker import AspectExtractionWorker, drain_worker

        release = threading.Event()
        worker = AspectExtractionWorker()
        thread = threading.Thread(target=release.wait, args=(30,), daemon=True)
        thread.start()
        worker._thread = thread
        monkeypatch.setattr(aspect_worker, "_worker", worker)
        ran = threading.Event()
        monkeypatch.setattr(worker, "_run_loop", ran.set)
        try:
            drain_worker(tmp_path / "unused.db", _locks_dir=locks_dir, timeout=1.0)
            assert worker._stopping == [thread], "the unjoined thread is on record"
            worker.start()
            assert worker._thread is None and not ran.is_set(), "start() refuses beside it"
        finally:
            release.set()
            thread.join(5)

    def test_drain_poll_loop_exits_when_the_census_drains(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        """OBS-1 (poll loop): drain_worker must keep polling until the aspects census
        has no live task. Live, live, then empty must succeed without error, and the
        census is read once per poll."""
        from nexus.aspect_worker import drain_worker

        side = _wire_sides(monkeypatch, census=[
            _aspects_census(available=1, total=1),
            _aspects_census(claimed=1, total=1),
            _aspects_census(),
        ])

        # Must NOT raise: the loop should see live, live, empty and return.
        drain_worker(
            tmp_path / "unused.db",
            _locks_dir=locks_dir,
            timeout=5.0,
            poll_interval=0.05,
        )
        assert side.calls == 3, f"one census read per poll: {side.calls}"

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

        # Place a PID-1 lock file — would block drain in LOCAL mode.
        (locks_dir / "aspect_worker.1").write_text("1")

        # Must NOT raise — lock files are ignored.
        drain_worker(tmp_path / "unused.db", _locks_dir=locks_dir, timeout=1.0)

    def test_cli_drain_surfaces_detail_hint_on_timeout(self, monkeypatch) -> None:
        """OBS / Medium-1: `nx aspects drain` must surface DrainTimeoutError.detail
        (the lapsed-claim hint) to the operator — not swallow it behind the
        generic 'Re-run...' message. The stuck_count==0 case with a detail hint is
        exactly when the operator needs the hint.
        """
        from click.testing import CliRunner
        from nexus.aspect_worker import DrainTimeoutError
        from nexus.commands import aspects as aspects_mod

        hint = (
            "Tuple queue 'aspects': 0 available, 1 claimed (1 of them lapsed). "
            "A running aspect worker claims the available tasks and releases "
            "the lapsed ones."
        )

        def _raise(*a, **k):
            raise DrainTimeoutError(stuck_count=1, timeout=1.0, detail=hint)

        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: "/tmp/x.db")
        monkeypatch.setattr("nexus.aspect_worker.drain_worker", _raise)

        result = CliRunner().invoke(aspects_mod.aspects_drain, ["--timeout", "1"])
        assert result.exit_code == 1
        assert "releases the lapsed ones" in result.output, (
            f"CLI must surface the lapsed-claim hint; got: {result.output!r}"
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
            # A 1 s park (not the 25 s default): the loop exits once its parked
            # ``in`` returns, so the stop is seen inside the join window below.
            worker = AspectExtractionWorker(
                poll_interval=0.05, tuple_options={"park_timeout_s": 1},
            )
            worker.start()
            assert worker.is_running()

            worker.stop_claiming()

            # The thread exits once its park returns (at most park_timeout_s).
            thread = worker._thread
            if thread is not None:
                thread.join(timeout=3.0)
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
        """Drain on an already-drained queue returns immediately."""
        from nexus.aspect_worker import drain_worker

        # nexus-scc9t: drain_worker's own census short-circuit (the
        # ``if _drained(): return`` before the poll loop) fires before
        # time.sleep(poll_interval) is ever called for an already-drained
        # queue. Asserted directly
        # -- a loaded box can blow a wall-clock elapsed bound with no
        # regression present, since the assertion cares about REAL
        # scheduler-delay-inflated seconds rather than whether the
        # short-circuit engaged.
        with patch_time("nexus.aspect_worker", "sleep") as mock_sleep:
            drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)
        mock_sleep.assert_not_called()

    def test_drain_waits_for_a_claimed_task_to_be_acked(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """drain() blocks until a claimed aspects task is acked."""
        from nexus.aspect_worker import drain_worker

        # Enqueue a task, claim it (it is in flight), then ack it from a
        # background thread after a short delay.
        with T2Database(queue_path) as db:
            db.aspect_queue.enqueue("knowledge__test", "/async.pdf", doc_id=_shared_doc_id())
        got = _claim_one("t-async")
        assert got is not None, "the enqueued task must be claimable"
        claim_id = got[1]

        def ack_after_delay():
            time.sleep(0.2)
            _settle(claim_id, "t-async")

        t = threading.Thread(target=ack_after_delay, daemon=True)
        t.start()

        drain_worker(queue_path=queue_path, timeout=5.0, _locks_dir=locks_dir)
        t.join()

        assert _aspects_live() == 0

    def test_drain_raises_on_timeout(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """drain() raises DrainTimeoutError if claimed aspects tasks outlive timeout.

        SG-2 (nexus-1091): increased margins to 1.0s timeout / 0.05s poll so
        this test is not flaky on loaded CI.
        """
        from nexus.aspect_worker import DrainTimeoutError, drain_worker

        with T2Database(queue_path) as db:
            db.aspect_queue.enqueue("knowledge__test", "/stuck.pdf", doc_id=_shared_doc_id())
        assert _claim_one("t-stuck") is not None
        # Never ack -- the task stays claimed

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

    def test_drain_with_only_dead_tasks_returns_immediately(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """Dead tasks are terminal; drain treats them as done."""
        from nexus.aspect_worker import drain_worker

        with T2Database(queue_path) as db:
            db.aspect_queue.enqueue("knowledge__test", "/fail.pdf", doc_id=_shared_doc_id())
        got = _claim_one("t-fail")
        assert got is not None
        _settle(got[1], "t-fail", dead=True)

        # nexus-scc9t: see test_drain_on_empty_queue_is_noop -- a queue
        # with only terminal (dead) tasks is drained at once, so the
        # short-circuit fires and time.sleep is never called.
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
        self, queue_path: Path, locks_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After drain, calling start() re-arms the worker to process new rows.

        The restarted worker must claim and process the enqueued ``aspects``
        task within the 3 s window below.

        SG-1 fix (nexus-1091): a real verification that the enqueued task was
        claimed and acked (no live task left) within a polling window.
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
        # so the worker acks each task without real Claude calls.
        extractor_mod.extract_aspects = lambda **_kw: None
        # The singleton gets a 1 s park. At the 25 s default a parked thread
        # outlives drain's 2 s join, drain records it, and the restart below is
        # refused beside it. The park is set at construction; ensure_worker_started
        # takes no tuple options, so the built worker is installed as the singleton.
        import nexus.aspect_worker as worker_mod
        from nexus.aspect_worker import AspectExtractionWorker

        worker = AspectExtractionWorker(poll_interval=0.05, tuple_options={"park_timeout_s": 1})
        monkeypatch.setattr(worker_mod, "_worker", worker)

        try:
            ensure_worker_started(poll_interval=0.05, _locks_dir=locks_dir)
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

            # The restarted worker claims and acks the new row: the aspects
            # census reaches no live task (available + claimed).
            settle(
                _aspects_live, 3.0, interval=0.05,
                what="the restarted worker's claim of the new row (live aspects tasks):",
            )
        finally:
            infra_mod.t2_ctx = original_t2_ctx
            infra_mod.t2_index_write = original_t2_index_write
            extractor_mod.extract_aspects = original_extract

    def test_worker_not_running_drain_is_one_census_read(
        self, queue_path: Path, locks_dir: Path
    ) -> None:
        """When no worker thread is alive, drain() is a single census check."""
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
            # 1 s park, as in the stop test above: the thread exits within the join.
            worker = AspectExtractionWorker(
                poll_interval=0.05, tuple_options={"park_timeout_s": 1},
            )
            worker.start()
            assert worker.is_running()

            worker.stop_claiming()
            assert worker.is_claiming_stopped()

            # Wait for thread to exit (at most one park_timeout_s)
            thread = worker._thread
            if thread is not None:
                thread.join(timeout=3.0)

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

            # The aspects census is empty on this tenant, so drain_worker skips
            # the polling loop and goes straight to the thread-join.
            try:
                drain_worker(
                    queue_path=queue_path,
                    timeout=5.0,
                    poll_interval=0.05,
                    _locks_dir=locks_dir,
                )
            finally:
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


def _wire_sides(monkeypatch, *, census=None):
    """Stub the `aspects` census read. `census` is one census (or an exception to
    raise), or a list with one entry per poll (the last one repeats)."""
    from nexus.db.t2 import http_tuple_store as tmod

    class Side(_TupleSide):
        pass

    Side.answers = list(census) if isinstance(census, list) else [census or _aspects_census()]
    Side.calls = 0
    Side.closed = False
    monkeypatch.setattr(tmod, "HttpTupleStore", Side)
    return Side


class TestDrainCountsTheTupleSide:
    """The drained test is `available + claimed == 0` on `aspects`. Dead tasks
    never block. Backoff-delayed tasks count as available, which makes the gate
    conservative."""

    def _drain(self, tmp_path, locks_dir, timeout=0.2):
        from nexus.aspect_worker import drain_worker

        drain_worker(tmp_path / "unused.db", _locks_dir=locks_dir, timeout=timeout, poll_interval=0.02)

    @pytest.mark.parametrize("census", [
        {"available": 1, "total": 1},
        {"claimed": 1, "total": 1},
        {"available": 2, "claimed": 3, "total": 5},
    ])
    def test_live_aspects_tasks_are_not_drained(
        self, monkeypatch, locks_dir: Path, tmp_path: Path, census,
    ) -> None:
        from nexus.aspect_worker import DrainTimeoutError

        _wire_sides(monkeypatch, census=_aspects_census(**census))
        with pytest.raises(DrainTimeoutError) as exc:
            self._drain(tmp_path, locks_dir)
        assert exc.value.stuck_count == census.get("available", 0) + census.get("claimed", 0)

    def test_dead_tasks_alone_do_not_block(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        side = _wire_sides(monkeypatch, census=_aspects_census(dead=4, total=4))
        self._drain(tmp_path, locks_dir)
        assert side.calls >= 1, "the tuple side must have been read"
        assert side.closed is True, "the tuple store must be closed"

    def test_the_census_drains_during_the_poll(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        side = _wire_sides(monkeypatch, census=[
            _aspects_census(available=2, total=2), _aspects_census(claimed=1, total=1),
            _aspects_census(),
        ])
        self._drain(tmp_path, locks_dir, timeout=5.0)
        assert side.calls >= 3

    def test_an_engine_without_the_aspects_template_drains(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        from nexus.db.t2.http_tuple_store import UnknownSubspaceError

        _wire_sides(monkeypatch, census=UnknownSubspaceError("none"))
        self._drain(tmp_path, locks_dir)

    def test_a_tuple_read_that_fails_is_not_read_as_drained(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        _wire_sides(monkeypatch, census=RuntimeError("503"))
        with pytest.raises(RuntimeError, match="503"):
            self._drain(tmp_path, locks_dir)

    def test_the_timeout_names_the_aspects_side_with_claimed_and_lapsed_counts(
        self, monkeypatch, locks_dir: Path, tmp_path: Path,
    ) -> None:
        from nexus.aspect_worker import DrainTimeoutError

        _wire_sides(
            monkeypatch, census=_aspects_census(available=3, claimed=2, lapsed=1, total=5),
        )
        with pytest.raises(DrainTimeoutError) as exc:
            self._drain(tmp_path, locks_dir)
        err = exc.value
        assert err.stuck_count == 5
        text = str(err)
        assert "aspects" in text and "claimed" in text and "lapsed" in text
        assert "releases the lapsed ones" in text, "the lapsed-claim hint for the aspects side stays"


# The drain CLI against the real engine (Test Plan scenario 21, drain half: exit 0
# only when the `aspects` tuple side is drained) is a step of the journeys in
# tests/test_aspects_requeue_failed_tuples.py (nexus-9edxe): it needs a tenant,
# and those journeys already hold one.
