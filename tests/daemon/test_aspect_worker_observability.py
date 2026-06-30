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

from structlog.testing import capture_logs

import nexus.aspect_worker as aw
import nexus.daemon.aspect_worker_daemon as awd
from nexus.daemon.aspect_worker_daemon import (
    AspectWorkerDaemon,
    ensure_aspect_worker_daemon,
)
from nexus.db import storage_mode


class _FakeWorker:
    def start(self) -> None: ...
    def stop(self, timeout: float = 10.0) -> None: ...


def _patch_service_mode(monkeypatch) -> None:
    monkeypatch.setattr(storage_mode, "storage_backend_for",
                        lambda _s: storage_mode.StorageBackend.SERVICE)
    monkeypatch.setattr("nexus.mcp_infra.t2_index_write", lambda fn: None)  # tripwire no-op


def test_unreachable_daemon_enqueue_emits_loud_signal_with_context(monkeypatch) -> None:
    """When ensure_aspect_worker_daemon fails in SERVICE mode, the store-time
    failure must be LOUD and carry tenant + queue_depth context (item 1)."""
    _patch_service_mode(monkeypatch)

    def _boom(**_k):
        raise RuntimeError("daemon unreachable")

    monkeypatch.setattr("nexus.daemon.aspect_worker_daemon.ensure_aspect_worker_daemon", _boom)
    monkeypatch.setattr(aw, "_best_effort_queue_depth", lambda: 7)  # service up; 7 rows waiting

    with capture_logs() as logs:
        aw._ensure_aspect_worker()

    unreachable = [e for e in logs if e.get("event") == "aspect_worker.daemon_unreachable"]
    assert unreachable, f"no loud daemon-unreachable signal; got {[e.get('event') for e in logs]}"
    ev = unreachable[0]
    assert ev["tenant"] == "default"            # diagnostic context
    assert ev["queue_depth"] == 7               # how many rows the outage is stranding
    assert ev["log_level"] in ("warning", "error")  # LOUD, not info/debug


def test_unreachable_signal_omits_queue_depth_when_unavailable(monkeypatch) -> None:
    """When the depth cannot be obtained, the field is OMITTED rather than logged
    as a -1 sentinel that would poison metric aggregation (review M1)."""
    _patch_service_mode(monkeypatch)
    monkeypatch.setattr("nexus.daemon.aspect_worker_daemon.ensure_aspect_worker_daemon",
                        lambda **_k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(aw, "_best_effort_queue_depth", lambda: None)

    with capture_logs() as logs:
        aw._ensure_aspect_worker()
    ev = [e for e in logs if e.get("event") == "aspect_worker.daemon_unreachable"][0]
    assert "queue_depth" not in ev
    assert ev["tenant"] == "default"


def test_enqueue_hook_service_mode_unreachable_emits_signal(monkeypatch) -> None:
    """END-TO-END: the signal fires through the REAL enqueue hook (AUTOSTART on +
    SERVICE + spawn raises), not just _ensure_aspect_worker (critic significant)."""
    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "1")
    _patch_service_mode(monkeypatch)
    monkeypatch.setattr("nexus.aspect_extractor.select_config", lambda _c: object())
    monkeypatch.setattr("nexus.daemon.aspect_worker_daemon.ensure_aspect_worker_daemon",
                        lambda **_k: (_ for _ in ()).throw(RuntimeError("unreachable")))
    monkeypatch.setattr(aw, "_best_effort_queue_depth", lambda: 3)

    with capture_logs() as logs:
        aw.aspect_extraction_enqueue_hook("/p/doc.pdf", "knowledge__o__m__v1", "content")
    assert [e for e in logs if e.get("event") == "aspect_worker.daemon_unreachable"]


class _CapturingLog:
    """Records structured log calls by replacing the module's ``_log`` —
    independent of structlog config and thread. Used for the daemon-module
    signals because ``capture_logs()`` is not cross-thread (the reclaim loop runs
    on its own thread) and the seam tests call into the daemon module directly."""

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


def test_spawn_child_died_emits_signal(tmp_path, monkeypatch) -> None:
    """A successful Popen whose CHILD exits immediately (the common
    misconfiguration: claude missing / credential failure) must be LOUD at the
    spawning process, not silently swallowed (critic CRITICAL)."""
    cap = _CapturingLog()
    monkeypatch.setattr(awd, "_log", cap)
    monkeypatch.setattr(awd, "_SPAWN_LIVENESS_GRACE_S", 0.0)  # no real wait in the test

    class _DeadChild:
        pid = 4242

        def poll(self):
            return 1   # already exited with code 1

    ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="default", _popen=lambda *a, **k: _DeadChild())

    died = cap.of("aspect_worker_daemon.spawn_child_died")
    assert died, f"no child-died signal; got {[e[1] for e in cap.events]}"
    level, _e, fields = died[0]
    assert level == "error"
    assert fields["returncode"] == 1
    assert fields["tenant"] == "default"


def test_spawn_live_child_emits_no_death_signal(tmp_path, monkeypatch) -> None:
    """A child still running after the grace window must NOT trip the signal."""
    cap = _CapturingLog()
    monkeypatch.setattr(awd, "_log", cap)
    monkeypatch.setattr(awd, "_SPAWN_LIVENESS_GRACE_S", 0.0)

    class _LiveChild:
        pid = 4242

        def poll(self):
            return None   # still running

    awd.ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="default", _popen=lambda *a, **k: _LiveChild())
    assert cap.of("aspect_worker_daemon.spawn_child_died") == []


def test_stop_with_undrained_rows_signals(tmp_path, monkeypatch) -> None:
    """Item 3: a daemon stopping while it owns in_progress rows it could not
    finish must signal that (and reset them) — the detectable worker-death case."""
    cap = _CapturingLog()
    monkeypatch.setattr(awd, "_log", cap)

    class _UndrainedQueue:
        def reclaim_stale(self, timeout_seconds: int = 300) -> int:
            return 2   # the final sweep finds 2 abandoned in_progress rows

        def close(self) -> None: ...

    d = AspectWorkerDaemon(
        config_dir=tmp_path, tenant="tenant-Z", worker_factory=_FakeWorker,
        queue_factory=_UndrainedQueue,
    )
    d._reclaim_queue = _UndrainedQueue()
    d.stop()

    signalled = cap.of("aspect_worker_daemon.stopping_with_undrained_rows")
    assert signalled, f"no undrained-on-exit signal; got {[e[1] for e in cap.events]}"
    level, _e, fields = signalled[0]
    assert level == "warning"
    assert fields["count"] == 2
    assert fields["tenant"] == "tenant-Z"
