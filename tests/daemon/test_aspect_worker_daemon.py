# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-173 Phase 1 (bead nexus-plzhp) — the leased aspect-worker daemon.

Foundation phase: the aspect-worker is registered as one more leased tier on the
RDR-149 service-registry substrate (``service_registry.py``), NOT a bespoke
daemon class. It mirrors the T1/T3 lease/heartbeat/single-flight discipline,
scoped PER-TENANT (per-host would need BYPASSRLS, which RDR-152 forbids for the
service role). The daemon hosts the ``AspectExtractionWorker`` loop; the spawn
entrypoint (``run_aspect_worker_daemon``) inherits its parent's environment so
``claude -p`` credentials flow automatically (the credential model is
established + tested in Phase 2).

These tests pin the Phase-1 lifecycle contract:
  - a started daemon publishes a per-tenant lease, discoverable by tenant;
  - the lease is tenant-scoped (a different tenant does not resolve it);
  - a second daemon for the SAME tenant converges to one live owner (generation
    fencing — the loser is fenced on its next heartbeat);
  - the hosted worker is started on start() and stopped on stop();
  - graceful stop relinquishes the lease (discover → None).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.aspect_worker import AspectExtractionWorker
from nexus.cli import main
from nexus.daemon.aspect_worker_daemon import (
    AspectWorkerDaemon,
    _default_worker_factory,
)
from nexus.daemon.service_registry import ServiceRegistry, ttl_for_tier

_ASPECT_TIER = "aspect_worker"


class _FakeWorker:
    """Stands in for AspectExtractionWorker — records start/stop without
    touching the service queue."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self, timeout: float = 10.0) -> None:
        self.stopped += 1


class _NoopQueue:
    """No-op reclaim queue so lifecycle tests don't build a real HttpAspectQueue
    (which needs a live service). Reclaim behavior itself is covered in
    tests/daemon/test_aspect_worker_reclaim.py."""

    def reclaim_stale(self, timeout_seconds: int = 300) -> int:
        return 0

    def close(self) -> None:
        ...


def _registry(config_dir: Path) -> ServiceRegistry:
    return ServiceRegistry(dir=config_dir, tier=_ASPECT_TIER, ttl=ttl_for_tier(_ASPECT_TIER))


def test_start_publishes_per_tenant_lease(tmp_path: Path) -> None:
    worker = _FakeWorker()
    d = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=lambda: worker, queue_factory=_NoopQueue)
    d.start()
    try:
        rec = _registry(tmp_path).discover("tenant-A")
        assert rec is not None
        assert rec.endpoint.get("pid")  # breadcrumb present
        assert worker.started == 1      # hosts the extraction worker
    finally:
        d.stop()


def test_lease_is_tenant_scoped(tmp_path: Path) -> None:
    d = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=_FakeWorker, queue_factory=_NoopQueue)
    d.start()
    try:
        reg = _registry(tmp_path)
        assert reg.discover("tenant-A") is not None
        assert reg.discover("tenant-B") is None   # a different tenant is a different scope
    finally:
        d.stop()


def test_stop_relinquishes_lease_and_stops_worker(tmp_path: Path) -> None:
    worker = _FakeWorker()
    d = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=lambda: worker, queue_factory=_NoopQueue)
    d.start()
    d.stop()
    assert _registry(tmp_path).discover("tenant-A") is None
    assert worker.stopped == 1


def test_second_instance_same_tenant_converges_to_one_owner(tmp_path: Path) -> None:
    """Two daemons for the same tenant: the registry's generation fencing makes
    the earlier owner stale. The later publisher holds the live lease; the first
    daemon's next heartbeat detects it is fenced."""
    d1 = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=_FakeWorker, queue_factory=_NoopQueue)
    d2 = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=_FakeWorker, queue_factory=_NoopQueue)
    d1.start()
    d2.start()  # higher generation — becomes the live owner
    try:
        # Exactly one live record resolves for the tenant.
        rec = _registry(tmp_path).discover("tenant-A")
        assert rec is not None
        # d1 is fenced (a newer generation owns the scope); its heartbeat says so.
        d1.heartbeat_once()
        assert d1.is_fenced() is True
        assert d2.is_fenced() is False
    finally:
        d1.stop()
        d2.stop()


def test_cli_spawn_entrypoint_wires_run_with_tenant(tmp_path, monkeypatch) -> None:
    """`nx daemon aspect-worker start --tenant T` is the Phase-1 spawn entrypoint
    (Phase 2's enqueue hook Popens it). It must resolve and call
    run_aspect_worker_daemon with the parsed config-dir + tenant."""
    calls: list[dict] = []
    monkeypatch.setattr(
        "nexus.daemon.aspect_worker_daemon.run_aspect_worker_daemon",
        lambda *, config_dir, tenant: calls.append({"config_dir": config_dir, "tenant": tenant}),
    )
    result = CliRunner().invoke(
        main,
        ["daemon", "aspect-worker", "start", "--config-dir", str(tmp_path), "--tenant", "tenant-A"],
    )
    assert result.exit_code == 0, result.output
    assert calls == [{"config_dir": tmp_path, "tenant": "tenant-A"}]


def test_default_worker_factory_builds_real_extraction_worker() -> None:
    """Phase 1's deliverable is hosting the REAL AspectExtractionWorker. Pin the
    default factory so a broken deferred import / constructor-signature change is
    caught (it is zero-arg, in-process — needs no service queue)."""
    worker = _default_worker_factory()
    assert isinstance(worker, AspectExtractionWorker)


@pytest.mark.parametrize("bad", ["a/b", ".hidden", "../escape"])
def test_invalid_tenant_rejected(tmp_path, bad) -> None:
    """The tenant is a discovery-file scope-key suffix; path-special values are
    rejected before Phase 2 wires arbitrary tenant strings (code-review M3)."""
    with pytest.raises(ValueError):
        AspectWorkerDaemon(config_dir=tmp_path, tenant=bad, worker_factory=_FakeWorker, queue_factory=_NoopQueue)


def test_empty_tenant_rejected(tmp_path) -> None:
    with pytest.raises(ValueError):
        AspectWorkerDaemon(config_dir=tmp_path, tenant="", worker_factory=_FakeWorker, queue_factory=_NoopQueue)


def test_live_heartbeat_loop_detects_fencing(tmp_path) -> None:
    """The actual heartbeat THREAD (not the heartbeat_once seam) must detect that
    a newer daemon fenced this one and stand down (set _stop). Exercises the
    live scheduling path, not just the data model (code-review M2)."""
    d1 = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A",
                            worker_factory=_FakeWorker, heartbeat_interval=0.05, queue_factory=_NoopQueue)
    d2 = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A",
                            worker_factory=_FakeWorker, heartbeat_interval=0.05, queue_factory=_NoopQueue)
    d1.start()
    d2.start()  # higher generation — fences d1
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not d1.is_fenced():
            time.sleep(0.02)
        assert d1.is_fenced() is True   # the LIVE heartbeat thread detected it
        assert d2.is_fenced() is False
    finally:
        d1.stop()
        d2.stop()
