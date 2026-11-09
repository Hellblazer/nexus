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

These tests pin the lifecycle contract:
  - a started daemon publishes a per-tenant lease, discoverable by tenant;
  - the lease is tenant-scoped (a different tenant does not resolve it);
  - a second daemon for the SAME tenant converges to one live owner (generation
    fencing — the loser is fenced on its next heartbeat);
  - the hosted worker is started on start() and stopped on stop();
  - graceful stop relinquishes the lease (discover → None);
  - the daemon attaches its claim-outcome hooks to a worker that exposes them,
    and hosts a worker that exposes neither unchanged (nexus-wl9pk);
  - an unreachable engine (claim round trips failing past the bound) stops the
    daemon, and an answered claim clears that clock (nexus-wl9pk);
  - stop releases the worker's held tuple claims and holds no queue handle
    (RDR-235; the table reclaim loop is gone).
"""
from __future__ import annotations

from collections.abc import Callable
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
from tests._settle import FakeClock, settle

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


def _registry(config_dir: Path) -> ServiceRegistry:
    return ServiceRegistry(dir=config_dir, tier=_ASPECT_TIER, ttl=ttl_for_tier(_ASPECT_TIER))


class _BindingFakeWorker(_FakeWorker):
    """A fake exposing the wake-file binding hook (nexus-59611 stage 2)."""

    def __init__(self) -> None:
        super().__init__()
        self.bound: Path | None = None

    def bind_config_dir(self, config_dir: Path) -> None:
        self.bound = config_dir


class _ClaimReportingWorker(_FakeWorker):
    """A fake that exposes both claim-outcome hooks and the tuple-claim release
    hook. The daemon attaches its handlers in ``start()``; a test drives them
    directly, so the worker thread never runs."""

    def __init__(self) -> None:
        super().__init__()
        self.on_failure: Callable[[], None] | None = None
        self.on_contact: Callable[[], None] | None = None
        self.released = 0

    def set_engine_failure_handler(self, handler: Callable[[], None] | None) -> None:
        self.on_failure = handler

    def set_engine_contact_handler(self, handler: Callable[[], None] | None) -> None:
        self.on_contact = handler

    def release_held_claims(self) -> int:
        self.released += 1
        return 0


class _Mono(FakeClock):
    """The daemon's monotonic clock, virtual (tests/_settle.py, nexus-3ceda.11):
    the heartbeat runs only when the test beats it, so "no stop yet" is checked
    after a heartbeat that really ran at the new time."""

    def __init__(self) -> None:
        super().__init__(start=1000.0)

    @property
    def now(self) -> float:
        return self()

    def beat(self, daemon: AspectWorkerDaemon, seconds: float) -> None:
        """Advance *seconds*; return once the heartbeat has run at the new time
        (and parked again, or ended because it stopped the daemon)."""
        self.step(seconds, exited=lambda: not daemon._hb_thread.is_alive())


def test_start_binds_the_daemons_config_dir_to_the_worker(tmp_path: Path) -> None:
    """The wake file is read from the daemon's --config-dir, not the ambient
    one: start() hands the worker the dir it was constructed with."""
    worker = _BindingFakeWorker()
    d = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=lambda: worker)
    d.start()
    try:
        assert worker.bound == tmp_path
    finally:
        d.stop()


def test_start_publishes_per_tenant_lease(tmp_path: Path) -> None:
    worker = _FakeWorker()
    d = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=lambda: worker)
    d.start()
    try:
        rec = _registry(tmp_path).discover("tenant-A")
        assert rec is not None
        assert rec.endpoint.get("pid")  # breadcrumb present
        assert worker.started == 1      # hosts the extraction worker
    finally:
        d.stop()


def test_lease_is_tenant_scoped(tmp_path: Path) -> None:
    d = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=_FakeWorker)
    d.start()
    try:
        reg = _registry(tmp_path)
        assert reg.discover("tenant-A") is not None
        assert reg.discover("tenant-B") is None   # a different tenant is a different scope
    finally:
        d.stop()


def test_stop_relinquishes_lease_and_stops_worker(tmp_path: Path) -> None:
    worker = _FakeWorker()
    d = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=lambda: worker)
    d.start()
    d.stop()
    assert _registry(tmp_path).discover("tenant-A") is None
    assert worker.stopped == 1


def test_second_instance_same_tenant_converges_to_one_owner(tmp_path: Path) -> None:
    """Two daemons for the same tenant: the registry's generation fencing makes
    the earlier owner stale. The later publisher holds the live lease; the first
    daemon's next heartbeat detects it is fenced."""
    d1 = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=_FakeWorker)
    d2 = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=_FakeWorker)
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


class _FencesDuringStopWorker(_FakeWorker):
    """A worker whose ``stop()`` simulates a heartbeat tick's fence landing
    WHILE it drains -- after ``fenced`` was captured at the top of
    ``AspectWorkerDaemon.stop()``, before the later mark/relinquish guard
    is reached. ``daemon`` is set post-construction (the daemon does not
    exist yet when the worker_factory builds this instance)."""

    def __init__(self) -> None:
        super().__init__()
        self.daemon: "AspectWorkerDaemon | None" = None
        self.fenced_applied = False

    def stop(self, timeout: float = 10.0) -> None:
        super().stop(timeout=timeout)
        if self.daemon is not None and self.daemon._supervisor is not None:
            self.daemon._supervisor.fenced = True
            self.fenced_applied = True


def test_mark_relinquish_uses_the_captured_fenced_value(tmp_path: Path) -> None:
    """nexus-cd1k0.6 review-wave2 follow-up: ``fenced`` is captured ONCE at the
    top of stop(). The mark/relinquish guard must use that captured value, not
    re-read ``supervisor.fenced`` live: a fence transition landing mid-drain
    (after the capture) must not change whether a daemon that was NOT fenced
    when stop() began still relinquishes its lease."""
    worker = _FencesDuringStopWorker()
    d = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A",
                           worker_factory=lambda: worker)
    d.start()
    worker.daemon = d
    assert d.is_fenced() is False  # not fenced when stop() will capture it

    d.stop()

    assert worker.fenced_applied is True, "the mid-stop race did not actually fire"
    assert _registry(tmp_path).discover("tenant-A") is None, (
        "mark_shutting_down/relinquish must still run, using the captured "
        "pre-fence value, not the live post-fence one"
    )


def test_cli_spawn_entrypoint_wires_run_with_tenant(tmp_path, monkeypatch) -> None:
    """`nx daemon aspect-worker start --tenant T` is the Phase-1 spawn entrypoint
    (Phase 2's enqueue hook Popens it). It must resolve and call
    run_aspect_worker_daemon with the parsed config-dir + tenant."""
    calls: list[dict] = []
    monkeypatch.setattr(
        "nexus.daemon.aspect_worker_daemon.run_aspect_worker_daemon",
        lambda *, config_dir, tenant, **_kw: calls.append({"config_dir": config_dir, "tenant": tenant}),
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
        AspectWorkerDaemon(config_dir=tmp_path, tenant=bad, worker_factory=_FakeWorker)


def test_empty_tenant_rejected(tmp_path) -> None:
    with pytest.raises(ValueError):
        AspectWorkerDaemon(config_dir=tmp_path, tenant="", worker_factory=_FakeWorker)


def test_live_heartbeat_loop_detects_fencing(tmp_path) -> None:
    """The actual heartbeat THREAD (not the heartbeat_once seam) must detect that
    a newer daemon fenced this one and stand down (set _stop). Exercises the
    live scheduling path, not just the data model (code-review M2)."""
    d1 = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A",
                            worker_factory=_FakeWorker, heartbeat_interval=0.05)
    d2 = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A",
                            worker_factory=_FakeWorker, heartbeat_interval=0.05)
    d1.start()
    d2.start()  # higher generation — fences d1
    try:
        settle(lambda: not d1.is_fenced() and "d1 not fenced yet", what="the live heartbeat")
        assert d1.is_fenced() is True   # the LIVE heartbeat thread detected it
        assert d2.is_fenced() is False
    finally:
        d1.stop()
        d2.stop()


# ── RDR-235 (nexus-5r2sc.3.3): the stop sweep releases tuple claims ──────────


class _ClaimHoldingWorker(_FakeWorker):
    """A worker that holds tuple claims: ``release_held_claims`` is the hook the
    daemon's stop sweep calls after the worker has stopped."""

    def __init__(self, held: int = 3) -> None:
        super().__init__()
        self.held = held
        self.order: list[str] = []

    def stop(self, timeout: float = 10.0) -> None:
        super().stop(timeout)
        self.order.append("stop")

    def release_held_claims(self) -> int:
        self.order.append("release")
        released, self.held = self.held, 0
        return released


def test_stop_releases_held_tuple_claims_after_the_worker_stopped(tmp_path: Path) -> None:
    worker = _ClaimHoldingWorker(held=3)
    d = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=lambda: worker)
    d.start()
    d.stop()
    assert worker.order == ["stop", "release"], "release runs once, after stop, so claims in flight are the worker's last"
    assert worker.held == 0


def test_a_fenced_daemon_still_releases_its_own_claims(tmp_path: Path) -> None:
    """A tuple claim is held under THIS process's claimant strings, so releasing
    it can never touch the successor's work, fenced or not."""
    worker = _ClaimHoldingWorker(held=2)
    d = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=lambda: worker)
    d.start()
    d._supervisor.fenced = True  # a newer owner won the scope
    d.stop()
    assert worker.order == ["stop", "release"]


def test_a_release_that_raises_does_not_block_the_rest_of_the_stop(tmp_path: Path) -> None:
    class _Raising(_ClaimHoldingWorker):
        def release_held_claims(self) -> int:
            raise RuntimeError("engine down")

    d = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=_Raising)
    d.start()
    d.stop()
    assert _registry(tmp_path).discover("tenant-A") is None, "the lease was still relinquished"


def test_a_worker_fake_without_the_hook_stops_as_before(tmp_path: Path) -> None:
    d = AspectWorkerDaemon(config_dir=tmp_path, tenant="tenant-A", worker_factory=_FakeWorker)
    d.start()
    d.stop()
    assert _registry(tmp_path).discover("tenant-A") is None


# ── nexus-wl9pk: the claim-outcome hooks and the stop that follows ───────────


def _claim_daemon(cfg: Path, worker: _FakeWorker, mono: _Mono, *, bound: float = 100.0) -> AspectWorkerDaemon:
    return AspectWorkerDaemon(
        config_dir=cfg, tenant="tenant-U", worker_factory=lambda: worker,
        heartbeat_interval=0.02, engine_unreachable_exit_seconds=bound, monotonic=mono,
    )


def test_the_daemon_attaches_both_claim_hooks_to_a_worker_that_exposes_them(tmp_path: Path) -> None:
    worker = _ClaimReportingWorker()
    d = _claim_daemon(tmp_path, worker, _Mono())
    d.start()
    try:
        assert worker.on_failure == d.note_engine_failure
        assert worker.on_contact == d.note_engine_contact
    finally:
        d.stop()


def test_a_worker_with_neither_claim_hook_is_hosted_unchanged(tmp_path: Path) -> None:
    worker = _FakeWorker()  # no set_engine_*_handler at all
    d = _claim_daemon(tmp_path, worker, _Mono())  # type: ignore[arg-type]
    d.start()
    try:
        assert _registry(tmp_path).discover("tenant-U") is not None
        assert worker.started == 1
    finally:
        d.stop()
    assert worker.stopped == 1
    assert _registry(tmp_path).discover("tenant-U") is None


def test_claim_failures_past_the_bound_stop_the_daemon(tmp_path: Path) -> None:
    """The hosted worker reports each failed claim round trip. Failures that
    outlast the bound make the heartbeat request the daemon's stop; the
    failures alone (no answered claim) are what starts the clock."""
    mono = _Mono()
    worker = _ClaimReportingWorker()
    d = _claim_daemon(tmp_path, worker, mono, bound=100.0)
    d.start()
    try:
        assert worker.on_failure is not None, "non-vacuity: the hook must be attached"
        worker.on_failure()  # the first claim fails at t=0
        mono.beat(d, 99.0)
        assert not d._stop.is_set(), "99 s of a 100 s bound: no exit yet"
        worker.on_failure()  # more failing claims keep the same clock running
        mono.beat(d, 2.0)
        assert d._stop.is_set(), "the heartbeat must request the stop once failures outlast the bound"
        assert d._left_for_unreachable_engine is True
    finally:
        d.stop()


def test_an_answered_claim_clears_the_clock_before_the_bound_so_no_exit(tmp_path: Path) -> None:
    """Failures for 90 s, then an answered claim, then failures again: 180 s of
    failure in all, but the answered claim cleared the clock at 90 s, so the
    second run is only 90 s old and the daemon must still be up. Without the
    clear the first run would have crossed the 100 s bound at 100 s."""
    mono = _Mono()
    worker = _ClaimReportingWorker()
    d = _claim_daemon(tmp_path, worker, mono, bound=100.0)
    d.start()
    try:
        assert worker.on_failure is not None and worker.on_contact is not None
        worker.on_failure()  # the clock starts at t=0
        mono.beat(d, 90.0)
        worker.on_contact()  # an answered claim: the clock is cleared
        assert d._unreachable_since is None, "the answered claim must clear the clock"
        worker.on_failure()  # a new failing run starts at t=90
        mono.beat(d, 90.0)  # 180 s since the first failure, 90 s into the second run
        assert not d._stop.is_set(), "the answered claim must have cleared the clock"
        mono.beat(d, 20.0)  # 110 s into the second run: past the bound
        assert d._stop.is_set(), "the second failing run outlasted the bound and must stop the daemon"
    finally:
        d.stop()


def test_stop_releases_held_claims_and_holds_no_queue_handle(tmp_path: Path) -> None:
    """stop() releases the claims the worker still holds, and the daemon keeps
    no queue handle of its own: the constructor takes no ``queue_factory``."""
    worker = _ClaimReportingWorker()
    d = _claim_daemon(tmp_path, worker, _Mono())
    d.start()
    d.stop()
    assert worker.released == 1, "stop releases the worker's held claims"
    assert [name for name in vars(d) if "queue" in name.lower()] == [], (
        "the daemon must hold no queue handle"
    )
    with pytest.raises(TypeError):
        AspectWorkerDaemon(  # type: ignore[call-arg]
            config_dir=tmp_path, tenant="tenant-U", worker_factory=_FakeWorker,
            queue_factory=_FakeWorker,
        )
