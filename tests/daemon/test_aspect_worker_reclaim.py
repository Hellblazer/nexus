# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-173 Phase 3 (bead nexus-6ew6o) — the daemon's reclaim_stale loop (RF-5).

In SERVICE mode nobody resets a stranded ``in_progress`` row: the SQLite path's
``t2_daemon._reclaim_stale_loop`` never fires, the worker poll deliberately does
NOT reclaim (nexus-we61e), and the Java service has the ``reclaimStale`` SQL +
``POST /queue/reclaim_stale`` endpoint but never schedules a call. So a worker
death mid-extraction strands the row permanently — it blocks ``is_drained()``
and the migration gate forever.

The leased daemon is the reclaim owner: a loop calls the service queue's
``reclaim_stale(stale_timeout_seconds)`` on an interval (per-tenant, so the
service applies the tenant GUC), resetting stranded rows to ``pending`` so a
daemon/worker death self-heals — the row is re-claimed and re-extracted.

DECISION (the bead's Open Question): the per-tenant daemon is the PRIMARY reclaim
owner; a second routinely-scheduled Java reclaim is NOT added now. The Java
endpoint stays callable (ops/tests). Avoiding a per-host Java sweep also dodges a
cross-tenant scan that RLS would (correctly) starve of rows.

KNOWN CONSTRAINT (review): this leaves the daemon-down-AND-no-enqueue case
uncovered — if the daemon has crashed and nothing new enqueues (the enqueue hook
is the only respawn trigger), rows the dead daemon stranded stay stranded and
is_drained() does not recover until a new enqueue spawns a daemon. Recovery: any
new store re-spawns the daemon (reclaim-first clears the backlog at once), or an
operator calls POST /queue/reclaim_stale directly. A slow (e.g. hourly) Java
backstop sweep is the optional belt-and-suspenders, filed as a follow-up rather
than shipped here (it needs an engine change + cut). See nexus-t7jeo.
"""
from __future__ import annotations

import time
from pathlib import Path

from nexus.daemon.aspect_worker_daemon import (
    _DEFAULT_RECLAIM_INTERVAL,
    AspectWorkerDaemon,
)
from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue


class _FakeWorker:
    def start(self) -> None: ...
    def stop(self, timeout: float = 10.0) -> None: ...


class _FakeQueue:
    def __init__(self) -> None:
        self.reclaim_calls: list[int] = []
        self.closed = 0
        self._reclaimed = 0

    def reclaim_stale(self, timeout_seconds: int = 300) -> int:
        self.reclaim_calls.append(timeout_seconds)
        return self._reclaimed

    def close(self) -> None:
        self.closed += 1


def test_daemon_reclaims_on_interval_with_stale_window(tmp_path: Path) -> None:
    q = _FakeQueue()
    d = AspectWorkerDaemon(
        config_dir=tmp_path, tenant="default",
        worker_factory=_FakeWorker, queue_factory=lambda: q,
        reclaim_interval=0.05, stale_timeout_seconds=60,
    )
    d.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not q.reclaim_calls:
            time.sleep(0.02)
        assert q.reclaim_calls, "daemon did not run reclaim_stale on its interval"
        # the stale window passed through to the service queue
        assert q.reclaim_calls[0] == 60
    finally:
        d.stop()


def test_daemon_closes_reclaim_queue_on_stop(tmp_path: Path) -> None:
    q = _FakeQueue()
    d = AspectWorkerDaemon(
        config_dir=tmp_path, tenant="default",
        worker_factory=_FakeWorker, queue_factory=lambda: q,
        reclaim_interval=0.05, stale_timeout_seconds=60,
    )
    d.start()
    d.stop()
    assert q.closed == 1   # the reclaim queue handle is released exactly once (review M3)


def test_reclaim_interval_defaults_to_sweep_cadence_not_stale_window(tmp_path: Path) -> None:
    """The sweep cadence is DECOUPLED from the staleness threshold (review M1):
    a large stale_timeout must NOT slow the sweep to a 5-minute interval."""
    d = AspectWorkerDaemon(
        config_dir=tmp_path, tenant="default",
        worker_factory=_FakeWorker, queue_factory=_FakeQueue,
        stale_timeout_seconds=300,  # large threshold
    )
    assert d._reclaim_interval == _DEFAULT_RECLAIM_INTERVAL  # not 300


def test_reclaim_resets_stranded_row_and_drain_recovers(tmp_path: Path) -> None:
    """RF-5 END-TO-END against the REAL SQLite queue (review CRITICAL-1): a row
    stranded in_progress past the stale window is actually reset to pending by
    the daemon's reclaim loop, and is_drained() recovers once it is re-processed.
    Proves the state transition, not just that reclaim_stale was called."""
    db = tmp_path / "q.db"
    q = AspectExtractionQueue(db)
    q.enqueue("knowledge__o__m__v1", "/p/doc.pdf", content_hash="h", content="c")
    claimed = q.claim_next()                     # → in_progress, last_attempt_at = now
    assert claimed is not None
    assert q.pending_count() == 0
    assert q.is_drained() is False               # an in_progress (non-failed) row blocks drain
    # Backdate the claim so it is unambiguously stale (avoids second-granularity flake).
    q.conn.execute("UPDATE aspect_extraction_queue SET last_attempt_at = datetime('now','-1 hour')")
    q.conn.commit()

    # The daemon opens its OWN handle on the same DB (stop() closes it).
    d = AspectWorkerDaemon(
        config_dir=tmp_path, tenant="default", worker_factory=_FakeWorker,
        queue_factory=lambda: AspectExtractionQueue(db),
        reclaim_interval=0.03, stale_timeout_seconds=60,
    )
    d.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and q.pending_count() == 0:
            time.sleep(0.02)
        assert q.pending_count() == 1            # the stranded row was RESET to pending
    finally:
        d.stop()

    # Re-process the now-pending row (the extraction a live worker would do).
    again = q.claim_next()
    assert again is not None
    q.mark_done(again.collection, again.source_path)
    assert q.is_drained() is True                # is_drained recovers after the full cycle
    q.close()


def test_reclaim_failure_does_not_kill_the_loop(tmp_path: Path) -> None:
    """A transient reclaim error must be swallowed (logged) so the loop keeps
    running — a stranded row gets another chance on the next interval."""
    class _FlakyQueue(_FakeQueue):
        def reclaim_stale(self, timeout_seconds: int = 300) -> int:
            self.reclaim_calls.append(timeout_seconds)
            if len(self.reclaim_calls) == 1:
                raise RuntimeError("service blip")
            return 0

    q = _FlakyQueue()
    d = AspectWorkerDaemon(
        config_dir=tmp_path, tenant="default",
        worker_factory=_FakeWorker, queue_factory=lambda: q,
        reclaim_interval=0.03, stale_timeout_seconds=60,
    )
    d.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(q.reclaim_calls) < 2:
            time.sleep(0.02)
        assert len(q.reclaim_calls) >= 2   # survived the first failure, ticked again
    finally:
        d.stop()


def test_reclaim_failure_evicts_and_rebuilds_queue(tmp_path: Path) -> None:
    """nexus-64np7: a reclaim failure must evict the stale queue handle and
    rebuild via queue_factory, so the NEXT sweep re-resolves credentials
    instead of retrying the same broken client forever. Root cause of a
    2026-07-10 incident: a rotated bearer token produced a 401 on
    reclaim_stale every ~30s for 23+ hours with no recovery short of a
    manual daemon restart, because this handle (unlike claim_batch's,
    which already evicts via mcp_infra._service_t2_write_locked) was held
    for the daemon's entire lifetime with no eviction on error."""

    class _OnceFailingQueue(_FakeQueue):
        def __init__(self, fail: bool) -> None:
            super().__init__()
            self._fail = fail

        def reclaim_stale(self, timeout_seconds: int = 300) -> int:
            self.reclaim_calls.append(timeout_seconds)
            if self._fail:
                raise RuntimeError("401 Unauthorized (stale token)")
            return 0

    created: list[_OnceFailingQueue] = []

    def factory() -> _OnceFailingQueue:
        q = _OnceFailingQueue(fail=(len(created) == 0))
        created.append(q)
        return q

    d = AspectWorkerDaemon(
        config_dir=tmp_path, tenant="default",
        worker_factory=_FakeWorker, queue_factory=factory,
        reclaim_interval=0.03, stale_timeout_seconds=60,
    )
    d.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(created) < 2:
            time.sleep(0.02)
        assert len(created) >= 2, "stale queue was never rebuilt after the failure"
        assert created[0].reclaim_calls == [60]
        assert created[0].closed == 1          # the stale (failed) handle was released
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not created[1].reclaim_calls:
            time.sleep(0.02)
        assert created[1].reclaim_calls, "the rebuilt handle never got a chance to reclaim"
    finally:
        d.stop()
