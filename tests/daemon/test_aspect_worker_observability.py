# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-173 Phase 5 (bead nexus-xv5fl) — observability.

Today the failure is silent where it matters (store time) and loud where it
confuses (deferred at migration time) — the inverse of a good signal. Target:
LOUD at store time, observable self-healing.

  1. When a store enqueues but the leased daemon is unreachable / cannot be
     spawned, emit a structured signal with enough context (tenant, queue depth)
     to diagnose — the previously-silent store-time failure becomes observable.
  2. When the daemon's reclaim loop RESETS a stranded in_progress row, emit a
     structured signal so reclaim is observable, not invisible self-healing.

These tests assert the structured event FIELDS, not just that 'something logged'.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

import nexus.aspect_worker as aw
import nexus.daemon.aspect_worker_daemon as awd
from nexus.daemon.aspect_worker_daemon import AspectWorkerDaemon
from nexus.db import storage_mode


class _FakeWorker:
    def start(self) -> None: ...
    def stop(self, timeout: float = 10.0) -> None: ...


def test_unreachable_daemon_enqueue_emits_loud_signal_with_context(monkeypatch) -> None:
    """When ensure_aspect_worker_daemon fails in SERVICE mode, the store-time
    failure must be LOUD and carry tenant + queue_depth context (item 1)."""
    monkeypatch.setattr(storage_mode, "storage_backend_for",
                        lambda _s: storage_mode.StorageBackend.SERVICE)

    def _boom(**_k):
        raise RuntimeError("daemon unreachable")

    monkeypatch.setattr("nexus.daemon.aspect_worker_daemon.ensure_aspect_worker_daemon", _boom)
    # The tripwire persist is best-effort and needs no live service here.
    monkeypatch.setattr("nexus.mcp_infra.t2_index_write", lambda fn: None)

    with capture_logs() as logs:
        aw._ensure_aspect_worker()

    unreachable = [e for e in logs if e.get("event") == "aspect_worker.daemon_unreachable"]
    assert unreachable, f"no loud daemon-unreachable signal; got {[e.get('event') for e in logs]}"
    ev = unreachable[0]
    assert ev["tenant"] == "default"            # diagnostic context
    assert "queue_depth" in ev                  # how many rows are stranded by the outage
    assert ev["log_level"] in ("warning", "error")  # LOUD, not info/debug


class _CapturingLog:
    """Records structured log calls deterministically — avoids the structlog
    logger-caching gotcha (a module-level get_logger materialized before a test's
    capture_logs() keeps the prior config, so cross-test capture is unreliable)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def info(self, event: str, **kw) -> None:
        self.events.append(("info", event, kw))

    def warning(self, event: str, **kw) -> None:
        self.events.append(("warning", event, kw))

    def error(self, event: str, **kw) -> None:
        self.events.append(("error", event, kw))

    def of(self, event: str) -> list[tuple[str, str, dict]]:
        return [e for e in self.events if e[1] == event]


def test_reclaim_reset_emits_structured_signal(tmp_path: Path, monkeypatch) -> None:
    """When a reclaim sweep resets stranded rows, it emits a structured signal
    carrying the tenant + count so self-healing is observable (item 2). Driven
    via the _reclaim_once seam in the main thread."""
    class _ResettingQueue:
        def reclaim_stale(self, timeout_seconds: int = 300) -> int:
            return 3   # reset 3 rows

        def close(self) -> None: ...

    cap = _CapturingLog()
    monkeypatch.setattr(awd, "_log", cap)
    d = AspectWorkerDaemon(
        config_dir=tmp_path, tenant="tenant-X", worker_factory=_FakeWorker,
        queue_factory=_ResettingQueue, reclaim_interval=0.03, stale_timeout_seconds=300,
    )
    d._reclaim_queue = _ResettingQueue()
    d._reclaim_once()

    reclaimed = cap.of("aspect_worker_daemon.reclaimed_stale")
    assert reclaimed, f"no reclaim signal; got {[e[1] for e in cap.events]}"
    level, _event, fields = reclaimed[0]
    assert level == "info"
    assert fields["tenant"] == "tenant-X"
    assert fields["count"] == 3


def test_reclaim_silent_when_nothing_stranded(tmp_path: Path, monkeypatch) -> None:
    """A healthy sweep that resets nothing emits no reclaimed_stale event (the
    signal means 'self-healing happened', not 'loop is alive')."""
    class _EmptyQueue:
        def reclaim_stale(self, timeout_seconds: int = 300) -> int:
            return 0

        def close(self) -> None: ...

    cap = _CapturingLog()
    monkeypatch.setattr(awd, "_log", cap)
    d = AspectWorkerDaemon(
        config_dir=tmp_path, tenant="t", worker_factory=_FakeWorker, queue_factory=_EmptyQueue,
    )
    d._reclaim_queue = _EmptyQueue()
    d._reclaim_once()
    assert cap.of("aspect_worker_daemon.reclaimed_stale") == []
