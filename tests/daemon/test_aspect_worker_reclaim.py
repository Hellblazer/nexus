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

DECISION (the bead's Open Question): a SECOND scheduled ``reclaimStale`` in the
Java service is REDUNDANT — the per-tenant daemon is the single reclaim owner.
The Java endpoint stays un-scheduled (it already is); it remains callable for
ops/tests. Avoiding a second scheduler also dodges a per-host cross-tenant sweep
that RLS would (correctly) starve of rows.
"""
from __future__ import annotations

import time
from pathlib import Path

from nexus.daemon.aspect_worker_daemon import AspectWorkerDaemon


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
    assert q.closed >= 1   # the reclaim queue handle is released on stop


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
