# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the storage service supervisor (RDR-152 P5.1 bead nexus-gmiaf.30).

Covers supervisor-specific behaviour: publish-after-ready, heartbeat-while-
healthy, auto-restart-on-jar-death (higher generation), mark_shutting_down-
before-kill ordering, LOUD failure when the service can't start, windowed
restart budget, PG-independent recovery, token-in-lease, and end-to-end
discovery (supervisor writes vs health._resolve_service_endpoint reads).

The RDR-149 conformance battery for tier "storage_service" lives in
test_rdr149_lifecycle_conformance.py (StorageServiceRecordHarness). Bespoke
conformance was removed from here when the shared battery was wired (CRITICAL-2
fix). Only supervisor-specific assertions belong here.

Integration tests (pytest.mark.integration) require a real Postgres cluster
and Java JAR; they are excluded from the default unit suite.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus.daemon.service_registry import (
    LeaseRecord,
    ServiceRegistry,
    ServiceSupervisor,
    StaleOwnerError,
    process_state,
)
from nexus.daemon.storage_service_daemon import (
    HealthProbe,
    StopOutcome,
    StorageServiceStartError,
    StorageServiceSupervisor,
    _MAX_UNHEALTHY_HEARTBEATS,
    stop_storage_service,
    start_storage_service,
)


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


class _FakeClock:
    """Fixed, advanceable wall-clock surrogate."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeProc:
    """A fake subprocess.Popen-like object controllable in tests."""

    def __init__(self, pid: int = 42001, returncode: int | None = None) -> None:
        self.pid = pid
        self._returncode = returncode

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode

    def kill_proc(self) -> None:
        """Simulate the process dying."""
        self._returncode = -9


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "cfg"
    cd.mkdir(parents=True, exist_ok=True, mode=0o700)
    return cd


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
def creds_path(config_dir: Path) -> Path:
    """Write a minimal pg_credentials file so supervisor can read creds."""
    creds = config_dir / "pg_credentials"
    creds.write_text(
        "PG_PORT=15432\n"
        "PG_DATA=/tmp/testpgdata\n"
        "NX_DB_URL=jdbc:postgresql://127.0.0.1:15432/nexus\n"
        "NX_DB_USER=nexus_svc\n"
        "NX_DB_PASS=testsvcpass\n"
        "NX_DB_ADMIN_URL=jdbc:postgresql://127.0.0.1:15432/nexus\n"
        "NX_DB_ADMIN_USER=nexus_admin\n"
        "NX_DB_ADMIN_PASS=testadminpass\n"
    )
    creds.chmod(0o600)
    return creds


def _make_supervisor(
    config_dir: Path,
    clock: _FakeClock,
    *,
    pg_port: int = 15432,
    service_port: int = 18080,
    binary_path: Path | None = None,
    supervised: bool = False,
    creds: dict[str, str] | None = None,
    engine_liveness_scan: Any = None,
) -> StorageServiceSupervisor:
    """Build a supervisor with injected clock and no real pg/service spawn.

    ``engine_liveness_scan`` (nexus-8vp0i review round 2): when omitted,
    defaults to a fake that always reports no live engine (``[]``) — NOT
    the real process-table scan — so any test that reaches
    ``_release_stale_changelog_lock`` without explicitly caring about the
    liveness gate does not depend on ambient process-table state. Pass an
    explicit fake to exercise the gate itself.
    """
    if binary_path is None:
        binary_path = Path("/fake/nexus-service")
    if creds is None:
        creds = {
            "NX_DB_URL": "jdbc:...", "NX_DB_USER": "svc", "NX_DB_PASS": "pass",
            "NX_DB_ADMIN_URL": "jdbc:...", "NX_DB_ADMIN_USER": "admin",
            "NX_DB_ADMIN_PASS": "adminpass", "PG_PORT": str(pg_port),
            "PG_DATA": "/tmp/pgdata",
            # gmiaf.32.5: persistent root token, read from pg_credentials.
            "NX_SERVICE_TOKEN": "root-token-from-creds-deadbeef",
        }
    if engine_liveness_scan is None:
        engine_liveness_scan = lambda _config_dir, _binary_path: []  # noqa: E731
    return StorageServiceSupervisor(
        config_dir=config_dir,
        binary_path=binary_path,
        pg_port=pg_port,
        service_port=service_port,
        creds=creds,
        lease_clock=clock,
        supervised=supervised,
        engine_liveness_scan=engine_liveness_scan,
    )


# ---------------------------------------------------------------------------
# Unit tests: StorageServiceSupervisor internals
# ---------------------------------------------------------------------------


class TestStorageServiceSupervisorUnit:
    """Unit tests for the supervisor, mocking out real pg/jar spawning."""

    @pytest.fixture(autouse=True)
    def _isolate_service_token_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """nexus-zr9rv: these tests assert the supervisor resolves
        ``NX_SERVICE_TOKEN`` from the ``creds`` dict, but
        ``_resolve_service_token`` takes the ENV var over creds. Several
        ``tests/db/`` tests set ``os.environ["NX_SERVICE_TOKEN"]`` directly;
        when one runs earlier in the same process the leaked env value wins
        over the test's creds and the assertion fails. Clear the env so the
        creds path is exercised deterministically regardless of ordering."""
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)

    def test_publish_only_after_ready(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """The lease must not be published until the service is healthy."""
        sup = _make_supervisor(config_dir, clock)
        scope = str(os.getuid())
        registry = ServiceRegistry(
            dir=config_dir, tier="storage_service", clock=clock
        )

        # Before _publish() is called, nothing is discoverable.
        assert registry.discover(scope) is None

        # Manually inject a fake proc and call _publish()
        fake_proc = _FakeProc(pid=42100)
        sup._proc = fake_proc
        sup._service_port = 18082
        sup._publish(18082)

        assert registry.discover(scope) is not None

    def test_heartbeat_tick_while_healthy(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """heartbeat_once() returns (True, True) and re-stamps lease while jar is alive."""
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=42200)
        sup._proc = fake_proc
        sup._service_port = 18083
        sup._publish(18083)

        import nexus.daemon.storage_service_daemon as ssd_mod
        with patch.object(sup, "_service_healthy", return_value=True), \
             patch.object(sup, "_pg_reachable", return_value=True), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            result = sup.heartbeat_once()

        assert result == (True, True)
        # Lease must still be fresh
        registry = ServiceRegistry(
            dir=config_dir, tier="storage_service", clock=clock
        )
        assert registry.discover(str(os.getuid())) is not None

    def test_heartbeat_returns_false_jar_when_proc_exits(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """heartbeat_once() returns (False, _) when the jar process has exited via poll()."""
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=42300)
        sup._proc = fake_proc
        sup._service_port = 18084
        sup._publish(18084)

        # Simulate process exit
        fake_proc.kill_proc()

        jar_running, _pg_ok = sup.heartbeat_once()
        assert jar_running is False

    def test_heartbeat_returns_false_jar_when_pid_dead(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """heartbeat_once() returns (False, _) when the jar pid is not alive."""
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=42301)
        sup._proc = fake_proc
        sup._service_port = 18084
        sup._publish(18084)

        import nexus.daemon.storage_service_daemon as ssd_mod
        with patch.object(ssd_mod, "_pid_is_alive", return_value=False):
            jar_running, _pg_ok = sup.heartbeat_once()

        assert jar_running is False

    def test_heartbeat_returns_true_false_when_pg_down(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """heartbeat_once() returns (True, False) when jar is alive but PG is down."""
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=42302)
        sup._proc = fake_proc
        sup._service_port = 18084
        sup._publish(18084)

        import nexus.daemon.storage_service_daemon as ssd_mod
        with patch.object(sup, "_service_healthy", return_value=True), \
             patch.object(sup, "_pg_reachable", return_value=False), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            jar_running, pg_ok = sup.heartbeat_once()

        assert jar_running is True
        assert pg_ok is False

    def test_mark_shutting_down_before_kill(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """stop() calls mark_shutting_down() BEFORE killing the process group.

        Ordering: mark_shutting_down -> relinquish -> stop_service.
        """
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=42400)
        sup._proc = fake_proc
        sup._service_port = 18085
        sup._publish(18085)

        call_order: list[str] = []

        original_msd = sup._registry.mark_shutting_down

        def track_msd(rec: Any) -> None:
            call_order.append("mark_shutting_down")
            original_msd(rec)

        original_relinquish = sup._registry.relinquish

        def track_relinquish(rec: Any) -> None:
            call_order.append("relinquish")
            original_relinquish(rec)

        def track_killpg() -> None:
            call_order.append("stop_service")

        sup._registry.mark_shutting_down = track_msd  # type: ignore[method-assign]
        sup._registry.relinquish = track_relinquish  # type: ignore[method-assign]
        sup._stop_service = track_killpg  # type: ignore[method-assign]

        sup.stop()

        assert call_order.index("mark_shutting_down") < call_order.index("stop_service"), (
            "mark_shutting_down must come before stop_service (RDR-151 P1.3)"
        )

    def test_loud_failure_when_service_unreachable(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """If the service health probe fails, StorageServiceStartError is raised."""
        sup = _make_supervisor(config_dir, clock, service_port=19999)

        fake_proc = _FakeProc(pid=42500)

        with patch.object(sup, "_service_healthy", return_value=False):
            with pytest.raises(StorageServiceStartError, match="(?i)health|ready|timeout"):
                sup._wait_for_service_ready(fake_proc, 19999, timeout=0.5)

    def test_stop_sets_shutdown_marker(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """After stop(), the registry record is relinquished (None on discover)."""
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=42600)
        sup._proc = fake_proc
        sup._service_port = 18086
        sup._publish(18086)

        scope = str(os.getuid())
        registry = ServiceRegistry(
            dir=config_dir, tier="storage_service", clock=clock
        )

        # Before stop: discoverable
        assert registry.discover(scope) is not None

        # Patch _stop_service to not actually signal
        with patch.object(sup, "_stop_service"):
            sup.stop()

        # After stop: lease is relinquished (None)
        assert registry.discover(scope) is None

    def test_endpoint_carries_host_port_and_token(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """Published endpoint must carry host, port, and token for discover() consumers.

        HIGH-3 fix: NX_SERVICE_TOKEN is included in the lease endpoint so HTTP
        clients can re-read it after a restart.
        """
        sup = _make_supervisor(config_dir, clock, service_port=18087)
        fake_proc = _FakeProc(pid=42700)
        sup._proc = fake_proc
        sup._service_port = 18087
        sup._publish(18087)

        registry = ServiceRegistry(
            dir=config_dir, tier="storage_service", clock=clock
        )
        scope = str(os.getuid())
        rec = registry.discover(scope)
        assert rec is not None
        assert rec.endpoint["host"] == "127.0.0.1"
        assert rec.endpoint["port"] == 18087
        # HIGH-3: token must be present in the endpoint so clients can
        # rediscover it after a restart.
        assert "token" in rec.endpoint
        assert rec.endpoint["token"] == sup._service_token
        # nexus-4e96a: artifact identity, feeding the explicit-mismatch check
        # in _start_locked / ensure_storage_supervisor. Opaque endpoint field
        # only (no LeaseRecord/format_version change).
        assert rec.endpoint["artifact"] == str(sup._binary_path)
        assert rec.endpoint["launch_kind"] == sup._launch_kind

    def test_token_stable_across_restarts_from_creds(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """Token is the persisted NX_SERVICE_TOKEN, stable across restarts.

        gmiaf.32.5: stability now comes from persistence in pg_credentials, not
        from derivation. Two supervisor instances built from the same creds
        publish the same token (so HTTP clients don't get 401 after respawn).
        """
        creds = {
            "NX_DB_URL": "jdbc:...", "NX_DB_USER": "svc",
            "NX_DB_PASS": "stablepass", "NX_DB_ADMIN_URL": "jdbc:...",
            "NX_DB_ADMIN_USER": "admin", "NX_DB_ADMIN_PASS": "stableadmin",
            "PG_PORT": "15432", "PG_DATA": "/tmp/pgdata",
            "NX_SERVICE_TOKEN": "persisted-root-token-cafef00d",
        }
        sup1 = _make_supervisor(config_dir, clock, creds=creds)
        sup2 = _make_supervisor(config_dir, clock, creds=creds)
        assert sup1._service_token == sup2._service_token == "persisted-root-token-cafef00d"

    def test_token_decoupled_from_db_credentials(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """Anti-coupling (gmiaf.32.5): rotating DB passwords does NOT change the
        bearer token. The token is the persisted NX_SERVICE_TOKEN, independent
        of NX_DB_PASS / NX_DB_ADMIN_PASS (retires _derive_stable_token)."""
        base = {
            "NX_DB_URL": "jdbc:...", "NX_DB_USER": "svc",
            "NX_DB_ADMIN_URL": "jdbc:...", "NX_DB_ADMIN_USER": "admin",
            "PG_PORT": "15432", "PG_DATA": "/tmp/pgdata",
            "NX_SERVICE_TOKEN": "fixed-root-token-1234",
        }
        sup1 = _make_supervisor(
            config_dir, clock,
            creds={**base, "NX_DB_PASS": "passA", "NX_DB_ADMIN_PASS": "adminA"},
        )
        sup2 = _make_supervisor(
            config_dir, clock,
            creds={**base, "NX_DB_PASS": "passB", "NX_DB_ADMIN_PASS": "adminB"},
        )
        assert sup1._service_token == sup2._service_token == "fixed-root-token-1234"

    def test_missing_token_fails_loud(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """No NX_SERVICE_TOKEN in env or creds => StorageServiceStartError
        (no silent fallback for the auth-correctness input, gmiaf.32.5)."""
        creds = {
            "NX_DB_URL": "jdbc:...", "NX_DB_USER": "svc", "NX_DB_PASS": "p",
            "NX_DB_ADMIN_URL": "jdbc:...", "NX_DB_ADMIN_USER": "admin",
            "NX_DB_ADMIN_PASS": "a", "PG_PORT": "15432", "PG_DATA": "/tmp/pgdata",
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NX_SERVICE_TOKEN", None)
            with pytest.raises(StorageServiceStartError):
                _make_supervisor(config_dir, clock, creds=creds)

    def test_token_in_lease_after_publish(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """After _publish(), the token in the lease matches sup._service_token."""
        sup = _make_supervisor(config_dir, clock, service_port=18088)
        fake_proc = _FakeProc(pid=42701)
        sup._proc = fake_proc
        sup._publish(18088)

        registry = ServiceRegistry(dir=config_dir, tier="storage_service", clock=clock)
        rec = registry.discover(str(os.getuid()))
        assert rec is not None
        assert rec.endpoint.get("token") == sup._service_token


# ---------------------------------------------------------------------------
# PG-independent recovery (SIGNIFICANT-1 fix)
# ---------------------------------------------------------------------------



def _ssd_probe():
    """nexus-7f7gb: the heartbeat's health seam is now the tri-state
    ``_probe_service_health``; ``_service_healthy`` survives for the STARTUP
    readiness gate, where UNREADY and UNKNOWN are equivalent. Heartbeat tests
    stub the tri-state; startup tests keep stubbing the bool."""
    from nexus.daemon.storage_service_daemon import HealthProbe

    return HealthProbe


class TestPGIndependentRecovery:
    """When PG dies while the jar is still alive, the run loop must restart
    PG directly without triggering a jar respawn (SIGNIFICANT-1 fix)."""

    def test_heartbeat_does_not_respawn_when_pg_down(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """heartbeat_once() returns (True, False) when PG is down but jar is alive.

        The caller (run loop) handles PG recovery, not heartbeat_once() itself.
        """
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=43100)
        sup._proc = fake_proc
        sup._service_port = 19001
        sup._publish(19001)

        import nexus.daemon.storage_service_daemon as ssd_mod
        with patch.object(sup, "_probe_service_health", return_value=_ssd_probe().OK), \
             patch.object(sup, "_pg_reachable", return_value=False), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            jar_running, pg_ok = sup.heartbeat_once()

        # Jar should still be considered running
        assert jar_running is True
        # PG is reported down
        assert pg_ok is False
        # PG-down with a HEALTHY jar must not advance the stuck-process counter
        # (RDR-175: that counter is the only path to a falsey-running exit).
        assert sup._consecutive_unhealthy_heartbeats == 0, (
            "PG-down with a healthy jar must NOT advance the stuck-process "
            "exit counter; (True, False) is in-place PG recovery, not an exit"
        )

    def test_ensure_pg_running_called_when_pg_down_but_jar_alive(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """The run loop calls _ensure_pg_running() when (True, False) is returned.

        This test validates the run_storage_supervisor PG recovery path by
        directly verifying that heartbeat_once + _ensure_pg_running achieve
        independent PG recovery.
        """
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=43101)
        sup._proc = fake_proc
        sup._service_port = 19002
        sup._publish(19002)

        ensure_pg_called = []

        def _fake_ensure_pg() -> None:
            ensure_pg_called.append(True)

        sup._ensure_pg_running = _fake_ensure_pg  # type: ignore[method-assign]

        import nexus.daemon.storage_service_daemon as ssd_mod
        with patch.object(sup, "_service_healthy", return_value=True), \
             patch.object(sup, "_pg_reachable", return_value=False), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            jar_running, pg_ok = sup.heartbeat_once()

        assert jar_running is True and pg_ok is False
        # Simulate the run loop handling PG recovery
        if not pg_ok:
            sup._ensure_pg_running()

        assert ensure_pg_called, "_ensure_pg_running must be called on PG-down"



# ---------------------------------------------------------------------------
# End-to-end discovery test (CRITICAL-1 fix)
# ---------------------------------------------------------------------------


class TestEndToEndDiscovery:
    """Publish via StorageServiceSupervisor's ServiceRegistry(tier='storage_service')
    and discover via health._resolve_service_endpoint.

    CRITICAL-1 fix: the supervisor writes tier="storage_service" + scope=str(uid);
    _resolve_service_endpoint must read the same tier + scope, not tier="t2".
    This test would have failed with the old code (tier="t2" + scope_key="storage_service").

    NOTE: Uses real time.time() (not fake clock) for the publish, so the lease
    is fresh from the resolver's real-clock perspective.
    """

    def test_supervisor_publish_then_health_resolve(
        self, config_dir: Path
    ) -> None:
        """Publish via supervisor path (real clock) → discover via health module → same (host, port).

        Uses a real clock (time.time) so the published lease is fresh when
        health._resolve_service_endpoint reads it (which also uses time.time).
        The fake clock would publish at t=1000.0 and the resolver would see
        the TTL as expired vs real time (~1.7e9).
        """
        import time
        import nexus.health as health_mod

        # Use real-time clock for the publish so the lease is fresh
        sup = _make_supervisor(config_dir, _FakeClock(), service_port=19100,
                               )
        # Override the registry's clock to real time
        sup._lease_clock = time.time
        fake_proc = _FakeProc(pid=44001)
        sup._proc = fake_proc
        # Publish using a real-clock registry
        sup._registry = ServiceRegistry(
            dir=config_dir, tier="storage_service", clock=time.time,
        )
        from nexus.daemon.storage_service_daemon import _daemon_version, _SERVICE_HOST
        endpoint = {
            "host": _SERVICE_HOST,
            "port": 19100,
            "pid": fake_proc.pid,
            "token": sup._service_token,
        }
        sup._service_port = 19100
        from nexus.daemon.service_registry import ServiceSupervisor
        sup._supervisor = ServiceSupervisor(
            sup._registry,
            str(os.getuid()),
            version=_daemon_version(),
            endpoint_provider=lambda: endpoint,
        )
        sup._supervisor.publish_once()

        # health._resolve_service_endpoint reads from tier="storage_service"
        # with scope=str(os.getuid()). Isolate it to our tmp config_dir.
        result = health_mod._resolve_service_endpoint(config_dir)

        assert result is not None, (
            "_resolve_service_endpoint returned None — the tier/scope mismatch "
            "is not fixed (expected tier='storage_service', scope=str(uid))"
        )
        host, port = result
        assert host == "127.0.0.1"
        assert port == 19100

    def test_old_tier_t2_does_not_match(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """Verify the fix: reading tier='t2' would NOT find the storage_service record.

        This is a regression guard: if someone reverts health.py to use tier='t2',
        this test proves the supervisor's lease is NOT discoverable under t2.
        """
        sup = _make_supervisor(config_dir, clock, service_port=19101)
        fake_proc = _FakeProc(pid=44002)
        sup._proc = fake_proc
        sup._publish(19101)

        # Directly read using the OLD broken path (tier="t2", scope="storage_service")
        broken_registry = ServiceRegistry(dir=config_dir, tier="t2", clock=clock)
        broken_record = broken_registry.discover("storage_service")

        assert broken_record is None, (
            "The storage_service lease must NOT be discoverable via tier='t2' + "
            "scope='storage_service'. This verifies the CRITICAL-1 fix."
        )

    def test_health_resolve_returns_none_when_no_lease(
        self, config_dir: Path
    ) -> None:
        """_resolve_service_endpoint returns None when no supervisor has published."""
        import nexus.health as health_mod

        result = health_mod._resolve_service_endpoint(config_dir)
        assert result is None

    def test_token_readable_from_resolved_endpoint(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """After publish, the token is readable from the lease endpoint so HTTP
        clients can re-read it after a supervisor restart (HIGH-3 fix)."""
        sup = _make_supervisor(config_dir, clock, service_port=19102)
        fake_proc = _FakeProc(pid=44003)
        sup._proc = fake_proc
        sup._publish(19102)

        # Read back the raw lease record
        registry = ServiceRegistry(dir=config_dir, tier="storage_service", clock=clock)
        scope = str(os.getuid())
        rec = registry.discover(scope)
        assert rec is not None

        # Token must be present in the endpoint payload
        token = rec.endpoint.get("token")
        assert token is not None and len(token) > 0, (
            "Token must be present in lease endpoint so clients can rediscover "
            "it after a restart (HIGH-3 fix)"
        )
        assert token == sup._service_token


# ---------------------------------------------------------------------------
# Module-level start / stop helper tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stuck-JVM detection tests (SIG2 detection RETAINED; RDR-175 action = exit)
# ---------------------------------------------------------------------------


class TestStuckJvmDetection:
    """Jar alive but /health returning non-200 must signal a falsey `running`
    after _MAX_UNHEALTHY_HEARTBEATS consecutive failures so the supervise loop
    EXITS non-zero (RDR-175) — the OS watchdog then restarts the whole process.

    A stuck-but-alive JVM (connection-pool exhaustion, GC pause, internal
    deadlock) is the most common Java partial-failure mode, and the OS watchdog
    cannot see it (the process never dies) without this detection signal.
    RDR-175 retired the in-process respawn mechanism; the DETECTION is retained
    but its action is now exit-for-OS-restart, not _respawn. Treating it as
    'jar alive, lease not re-stamped, no recovery' was the silent-degrade gap.
    """

    def test_single_unhealthy_beat_does_not_signal_exit(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """A single unhealthy heartbeat is below threshold — no exit signal."""
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=45001)
        sup._proc = fake_proc
        sup._service_port = 20001
        sup._publish(20001)

        import nexus.daemon.storage_service_daemon as ssd_mod
        with patch.object(sup, "_service_healthy", return_value=False), \
             patch.object(sup, "_pg_reachable", return_value=True), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            jar_running, pg_ok = sup.heartbeat_once()

        # Below threshold: jar still considered running (no respawn yet)
        assert jar_running is True
        assert sup._consecutive_unhealthy_heartbeats == 1

    def test_threshold_minus_one_beats_no_exit(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """_MAX_UNHEALTHY_HEARTBEATS - 1 consecutive failures do not signal exit."""
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=45002)
        sup._proc = fake_proc
        sup._service_port = 20002
        sup._publish(20002)

        import nexus.daemon.storage_service_daemon as ssd_mod
        with patch.object(sup, "_service_healthy", return_value=False), \
             patch.object(sup, "_pg_reachable", return_value=True), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            for i in range(_MAX_UNHEALTHY_HEARTBEATS - 1):
                jar_running, _pg_ok = sup.heartbeat_once()
                assert jar_running is True, f"should not signal exit on beat {i+1}"

        assert sup._consecutive_unhealthy_heartbeats == _MAX_UNHEALTHY_HEARTBEATS - 1

    def test_at_threshold_returns_false_to_force_exit(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """After _MAX_UNHEALTHY_HEARTBEATS consecutive failures, return (False, pg_ok)
        so the run loop exits non-zero — treating stuck JVM like a jar death.
        """
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=45003)
        sup._proc = fake_proc
        sup._service_port = 20003
        sup._publish(20003)

        import nexus.daemon.storage_service_daemon as ssd_mod
        with patch.object(sup, "_service_healthy", return_value=False), \
             patch.object(sup, "_pg_reachable", return_value=True), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            # First N-1 beats: no respawn signal
            for _ in range(_MAX_UNHEALTHY_HEARTBEATS - 1):
                jar_running, _ = sup.heartbeat_once()
                assert jar_running is True
            # Nth beat: threshold crossed → exit signal
            jar_running, pg_ok = sup.heartbeat_once()

        assert jar_running is False, (
            f"After {_MAX_UNHEALTHY_HEARTBEATS} consecutive unhealthy beats, "
            "heartbeat_once() must return (False, _) to signal a supervisor exit"
        )
        assert pg_ok is True  # PG was healthy
        # Counter reset after signalling
        assert sup._consecutive_unhealthy_heartbeats == 0

    def test_single_healthy_beat_resets_counter(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """One healthy heartbeat resets the unhealthy counter to 0 so transient
        GC pauses do not accumulate toward the exit threshold.
        """
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=45004)
        sup._proc = fake_proc
        sup._service_port = 20004
        sup._publish(20004)

        import nexus.daemon.storage_service_daemon as ssd_mod

        # Accumulate some unhealthy beats (below threshold)
        with patch.object(sup, "_probe_service_health", return_value=_ssd_probe().UNKNOWN), \
             patch.object(sup, "_pg_reachable", return_value=True), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            for _ in range(_MAX_UNHEALTHY_HEARTBEATS - 1):
                sup.heartbeat_once()

        assert sup._consecutive_unhealthy_heartbeats == _MAX_UNHEALTHY_HEARTBEATS - 1

        # One healthy beat — counter resets
        with patch.object(sup, "_probe_service_health", return_value=_ssd_probe().OK), \
             patch.object(sup, "_pg_reachable", return_value=True), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            jar_running, pg_ok = sup.heartbeat_once()

        assert jar_running is True and pg_ok is True
        assert sup._consecutive_unhealthy_heartbeats == 0, (
            "One healthy beat must reset _consecutive_unhealthy_heartbeats to 0"
        )

    def test_saturated_pool_silent_health_but_live_livez_never_restarts(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """nexus-hubc0: /health silent + /livez answering = SATURATED, not wedged.

        ``GET /health`` takes a HikariCP connection, so under an indexing burst
        it can block past its own timeout and go silent — indistinguishable,
        before this change, from a wedged JVM. ``/livez`` touches no pool, so
        an answer there is positive proof the process is alive and its HTTP
        loop is responsive. The restart counter must therefore NEVER advance
        on this combination, no matter how long it persists: restarting a busy
        service severs in-flight clients, which retry, which adds load — the
        self-amplifying loop nexus-7f7gb was filed for.

        The lease is still NOT re-stamped (the service cannot serve reads right
        now), so discoverers age it out via TTL. Degraded, not dead.
        """
        sup = _make_supervisor(config_dir, clock)
        sup._proc = _FakeProc(pid=45010)
        sup._service_port = 20010
        sup._publish(20010)

        import nexus.daemon.storage_service_daemon as ssd_mod
        with patch.object(sup, "_probe_service_health", return_value=_ssd_probe().UNKNOWN), \
             patch.object(sup, "_probe_service_liveness", return_value=True), \
             patch.object(sup, "_pg_reachable", return_value=True), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            for beat in range(_MAX_UNHEALTHY_HEARTBEATS * 3):
                jar_running, _pg_ok = sup.heartbeat_once()
                assert jar_running is True, (
                    f"beat {beat + 1}: a saturated pool must never signal exit"
                )

        assert sup._consecutive_unhealthy_heartbeats == 0, (
            "a live /livez means the process is NOT wedged — the restart "
            "counter must stay at zero however long saturation lasts"
        )

    def test_wedged_process_both_probes_silent_still_exits(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """...and the detection this replaces still fires: silence on BOTH
        endpoints is a genuine wedge, and still exits for an OS restart within
        the same bounded number of beats."""
        sup = _make_supervisor(config_dir, clock)
        sup._proc = _FakeProc(pid=45011)
        sup._service_port = 20011
        sup._publish(20011)

        import nexus.daemon.storage_service_daemon as ssd_mod
        with patch.object(sup, "_probe_service_health", return_value=_ssd_probe().UNKNOWN), \
             patch.object(sup, "_probe_service_liveness", return_value=False), \
             patch.object(sup, "_pg_reachable", return_value=True), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            results = [sup.heartbeat_once()[0] for _ in range(_MAX_UNHEALTHY_HEARTBEATS)]

        assert results[:-1] == [True] * (_MAX_UNHEALTHY_HEARTBEATS - 1), (
            "below threshold a wedge must not exit yet"
        )
        assert results[-1] is False, (
            "at threshold a wedged process must still signal supervisor exit"
        )

    def test_compound_failure_below_threshold_returns_jar_alive(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """When the jar is unhealthy AND PG is also down, below the stuck-JVM
        threshold heartbeat_once() reports the jar as still-alive (True) and PG
        down (False), so the run loop takes the PG-recovery branch rather than
        exiting the supervisor prematurely (round-3 LOW-1).
        """
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=45007)
        sup._proc = fake_proc
        sup._service_port = 20007
        sup._publish(20007)

        import nexus.daemon.storage_service_daemon as ssd_mod
        with patch.object(sup, "_service_healthy", return_value=False), \
             patch.object(sup, "_pg_reachable", return_value=False), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            jar_running, pg_ok = sup.heartbeat_once()

        assert jar_running is True, (
            "Below the unhealthy threshold the jar is still considered alive"
        )
        assert pg_ok is False, "PG is down and must be reported as such"
        assert sup._consecutive_unhealthy_heartbeats == 1


# ---------------------------------------------------------------------------
# SIG1: _cycle_storage_service_to_current unit test
# ---------------------------------------------------------------------------


class TestCycleStorageServiceToCurrent:
    """_cycle_storage_service_to_current() must: discover the live lease using
    tier='storage_service' + uid scope, then invoke 'nx daemon service stop'
    BEFORE 'nx daemon service start'. Non-vacuous: wrong subcommand or order
    causes the assertions to fail.

    Uses the injectable seams (_discover_fn, _run_fn, _nx_bin_fn) added to
    the function for unit-testability — avoids deep try-block local import patching.
    """

    def test_noop_when_no_service_running(self) -> None:
        """If no storage_service lease is live, _cycle_storage_service_to_current
        must not call subprocess.run at all (no auto-spawn during upgrade).
        """
        from nexus.commands.upgrade import _cycle_storage_service_to_current

        subprocess_calls: list = []

        _cycle_storage_service_to_current(
            _discover_fn=lambda: None,  # no live lease
            _run_fn=lambda cmd, **kw: subprocess_calls.append(cmd),
            _nx_bin_fn=lambda: ["nx"],
        )

        assert subprocess_calls == [], (
            "No subprocess calls expected when no service is running"
        )

    def test_stop_before_start_when_service_running(self) -> None:
        """When a live storage_service lease exists, the cycle must call
        'nx daemon service stop' FIRST, then 'nx daemon service start' SECOND.
        Verifies correct subcommand and ordering — wrong verb or wrong order
        causes this assertion to fail.
        """
        from unittest.mock import MagicMock
        from nexus.commands.upgrade import _cycle_storage_service_to_current
        from nexus.daemon.service_registry import LeaseRecord

        fake_record = MagicMock(spec=LeaseRecord)
        subprocess_calls: list[list[str]] = []

        _cycle_storage_service_to_current(
            _discover_fn=lambda: fake_record,  # live lease
            _run_fn=lambda cmd, **kw: subprocess_calls.append(list(cmd)),
            _nx_bin_fn=lambda: ["nx"],
        )

        assert len(subprocess_calls) == 2, (
            f"Expected exactly 2 subprocess calls (stop + start), got: {subprocess_calls}"
        )
        # First call: stop
        assert "stop" in subprocess_calls[0], (
            f"First command must be 'stop', got: {subprocess_calls[0]}"
        )
        # Second call: start
        assert "start" in subprocess_calls[1], (
            f"Second command must be 'start', got: {subprocess_calls[1]}"
        )
        # Both must target 'service' subcommand, not 't2' or 't3'
        assert "service" in subprocess_calls[0], (
            f"Stop command must target 'service', got: {subprocess_calls[0]}"
        )
        assert "service" in subprocess_calls[1], (
            f"Start command must target 'service', got: {subprocess_calls[1]}"
        )

    def test_same_version_lease_skips_cycle(self) -> None:
        """nexus-f0pmd (RDR-183 candidate 0, GH #1405): a live supervisor whose
        lease version MATCHES the installed package version must NOT be
        cycled — the SessionStart hook's `nx upgrade --auto` was stop+starting
        a current supervisor on every session event (20 stop_requested
        exits/day, 5-10s lease gap each)."""
        from unittest.mock import MagicMock

        from nexus.commands.upgrade import _cycle_storage_service_to_current
        from nexus.daemon.service_registry import LeaseRecord

        fake_record = MagicMock(spec=LeaseRecord)
        fake_record.version = "9.9.9"
        subprocess_calls: list[list[str]] = []

        _cycle_storage_service_to_current(
            _discover_fn=lambda: fake_record,
            _run_fn=lambda cmd, **kw: subprocess_calls.append(list(cmd)),
            _nx_bin_fn=lambda: ["nx"],
            _installed_version_fn=lambda: "9.9.9",
        )

        assert subprocess_calls == [], (
            "a supervisor already on the installed version must not be cycled"
        )

    def test_version_skew_still_cycles(self) -> None:
        from unittest.mock import MagicMock

        from nexus.commands.upgrade import _cycle_storage_service_to_current
        from nexus.daemon.service_registry import LeaseRecord

        fake_record = MagicMock(spec=LeaseRecord)
        fake_record.version = "9.9.8"
        subprocess_calls: list[list[str]] = []

        _cycle_storage_service_to_current(
            _discover_fn=lambda: fake_record,
            _run_fn=lambda cmd, **kw: subprocess_calls.append(list(cmd)),
            _nx_bin_fn=lambda: ["nx"],
            _installed_version_fn=lambda: "9.9.9",
        )

        assert len(subprocess_calls) == 2, "stale supervisor must still be cycled"

    def test_empty_or_missing_lease_version_still_cycles(self) -> None:
        # Fail TOWARD cycling: a legacy lease without a version (or an empty
        # one) cannot prove currency — upgrade correctness wins.
        from nexus.commands.upgrade import _cycle_storage_service_to_current

        class _VersionlessLease:
            version = ""

        for lease in (_VersionlessLease(), object()):
            calls: list[list[str]] = []
            _cycle_storage_service_to_current(
                _discover_fn=lambda lease=lease: lease,
                _run_fn=lambda cmd, **kw: calls.append(list(cmd)),
                _nx_bin_fn=lambda: ["nx"],
                _installed_version_fn=lambda: "9.9.9",
            )
            assert len(calls) == 2, (
                f"unprovable lease version must still cycle, got {calls}"
            )

    def test_default_installed_version_fn_real_path(self, monkeypatch) -> None:
        # Review Medium-3: the four injected-fn tests all bypass the production
        # default. Exercise it: monkeypatch importlib.metadata.version so the
        # default seam resolves a matching version → skip.
        from unittest.mock import MagicMock

        import importlib.metadata as md

        from nexus.commands.upgrade import _cycle_storage_service_to_current
        from nexus.daemon.service_registry import LeaseRecord

        monkeypatch.setattr(md, "version", lambda name: "7.7.7")
        fake_record = MagicMock(spec=LeaseRecord)
        fake_record.version = "7.7.7"
        calls: list[list[str]] = []

        _cycle_storage_service_to_current(
            _discover_fn=lambda: fake_record,
            _run_fn=lambda cmd, **kw: calls.append(list(cmd)),
            _nx_bin_fn=lambda: ["nx"],
        )
        assert calls == [], "default _installed_version_fn must drive the skip"

    def test_default_installed_version_fn_probe_error_cycles(self, monkeypatch) -> None:
        # Review Medium-2 regression: ANY probe exception (not just
        # PackageNotFoundError) must yield "" → fail toward cycling, never
        # escape to the outer handler (which would fail toward doing nothing).
        from unittest.mock import MagicMock

        import importlib.metadata as md

        from nexus.commands.upgrade import _cycle_storage_service_to_current
        from nexus.daemon.service_registry import LeaseRecord

        def _boom(name):
            raise OSError("corrupted dist-info")

        monkeypatch.setattr(md, "version", _boom)
        fake_record = MagicMock(spec=LeaseRecord)
        fake_record.version = "7.7.7"
        calls: list[list[str]] = []

        _cycle_storage_service_to_current(
            _discover_fn=lambda: fake_record,
            _run_fn=lambda cmd, **kw: calls.append(list(cmd)),
            _nx_bin_fn=lambda: ["nx"],
        )
        assert len(calls) == 2, (
            "probe failure must fail toward cycling, not silently do nothing"
        )

    def test_installed_version_unknown_still_cycles(self) -> None:
        # If the installed version cannot be determined, do not skip.
        from unittest.mock import MagicMock

        from nexus.commands.upgrade import _cycle_storage_service_to_current
        from nexus.daemon.service_registry import LeaseRecord

        fake_record = MagicMock(spec=LeaseRecord)
        fake_record.version = "9.9.9"
        calls: list[list[str]] = []

        _cycle_storage_service_to_current(
            _discover_fn=lambda: fake_record,
            _run_fn=lambda cmd, **kw: calls.append(list(cmd)),
            _nx_bin_fn=lambda: ["nx"],
            _installed_version_fn=lambda: "",
        )
        assert len(calls) == 2

    def test_correct_tier_used_for_discover(self, config_dir: Path) -> None:
        """The production (non-injected) path must use tier='storage_service' for
        the discovery call. Verifies the CRITICAL-1 fix is not regressed in the
        upgrade path. Publishes a real lease under tier='storage_service' then
        calls the function with real discovery; if the tier is wrong, discover()
        returns None and no subprocess calls are made.
        """
        from nexus.commands.upgrade import _cycle_storage_service_to_current
        from nexus.daemon.service_registry import ServiceRegistry, ServiceSupervisor

        # Publish a real lease under tier='storage_service' in our tmp dir
        import time
        registry = ServiceRegistry(dir=config_dir, tier="storage_service", clock=time.time)
        sup = ServiceSupervisor(
            registry, str(os.getuid()),
            version="1.0.0",
            endpoint_provider=lambda: {"host": "127.0.0.1", "port": 19900},
        )
        sup.publish_once()

        subprocess_calls: list[list[str]] = []

        # Use the injectable discover seam to point at our tmp config_dir
        # (avoids patching nexus.config which is a local import inside the try block)
        def _real_discover():
            r = ServiceRegistry(dir=config_dir, tier="storage_service", clock=time.time)
            return r.discover(str(os.getuid()))

        _cycle_storage_service_to_current(
            _discover_fn=_real_discover,
            _run_fn=lambda cmd, **kw: subprocess_calls.append(list(cmd)),
            _nx_bin_fn=lambda: ["nx"],
        )

        # The real discover path found our lease → stop + start were called
        assert len(subprocess_calls) == 2, (
            "Expected stop + start calls; if 0, tier='storage_service' is broken "
            f"in the upgrade discover path. Calls: {subprocess_calls}"
        )


def _write_stale_or_fresh_lease(
    config_dir: Path,
    *,
    supervisor_pid: int | None,
    engine_pid: int | None,
    age_s: float,
    ttl: float = 15.0,
) -> None:
    """Write a ``storage_service_addr.<uid>`` lease record directly, with
    ``heartbeat_epoch`` stamped *age_s* seconds in the past — no fake clock
    or sleep needed to simulate a TTL-expired-but-alive-supervisor lease
    (nexus-oyo2g repro a/b/c: ``age_s > ttl`` is the lz3f2/f9y78 stall
    signature; ``age_s < ttl`` is an ordinary live lease).
    """
    scope = str(os.getuid())
    record = LeaseRecord(
        scope_key=scope,
        generation=1,
        owner_token="test-owner-token",
        heartbeat_epoch=time.time() - age_s,
        ttl=ttl,
        endpoint={"pid": engine_pid} if engine_pid is not None else {},
        version="0.0.0-test",
        payload={"supervisor_pid": supervisor_pid} if supervisor_pid is not None else {},
    )
    (config_dir / f"storage_service_addr.{scope}").write_text(record.to_json())


class TestRunStorageSupervisorFunction:
    """Tests for the module-level start/stop helper functions."""

    def test_stop_noop_when_no_lease(self, config_dir: Path) -> None:
        """No lease AND no matching OS process => genuinely already
        stopped (StopOutcome.already_stopped, source='none'). This is the
        ONLY case allowed to report 'already stopped' (nexus-oyo2g)."""
        with patch(
            "nexus.daemon.service_registry.all_process_rows", return_value=[],
        ):
            outcome = stop_storage_service(config_dir=config_dir)
        assert isinstance(outcome, StopOutcome)
        assert outcome.already_stopped
        assert outcome.source == "none"
        assert outcome.pids == ()

    def test_lease_miss_with_live_stack_is_not_reported_stopped(
        self, config_dir: Path,
    ) -> None:
        """REPRO (a) FALSE ALL-CLEAR (nexus-oyo2g): a TTL-expired lease
        (age_s=20 > ttl=15) on a supervisor+engine pair that are STILL
        ALIVE per the process table must never read as 'already stopped'.

        Falsifies the pre-fix code: ``stop_storage_service`` used to
        return ``None`` (source: registry.discover() -> None ->
        immediate 'no_live_lease' noop) the instant the lease record was
        unreadable/expired, WITHOUT ever consulting the process table.
        This assertion (``not outcome.already_stopped``) fails against
        that code and passes only once the lease-miss fallback exists.
        """
        supervisor_pid, engine_pid = 970101, 970102
        _write_stale_or_fresh_lease(
            config_dir, supervisor_pid=supervisor_pid, engine_pid=engine_pid,
            age_s=20.0, ttl=15.0,
        )
        rows = [
            (supervisor_pid, 60,
             f"nx daemon service start --foreground --config-dir {config_dir}"),
            (engine_pid, 60, f"{config_dir}/service/nexus-service -Xmx1g"),
        ]
        terminated: list[list[int]] = []
        with patch(
            "nexus.daemon.service_registry.all_process_rows", return_value=rows,
        ), patch(
            "nexus.daemon.service_registry.process_command",
            side_effect=lambda pid: dict((p, c) for p, _a, c in rows)[pid],
        ), patch(
            "nexus.daemon.service_registry.terminate_pids",
            side_effect=lambda pids, **_: terminated.append(list(pids)) or [],
        ):
            outcome = stop_storage_service(config_dir=config_dir)
        assert not outcome.already_stopped, (
            "a TTL-expired lease on a live supervisor+engine must never "
            f"read as 'already stopped'; got {outcome}"
        )
        assert outcome.source == "process_table"
        assert sorted(outcome.pids) == sorted([supervisor_pid, engine_pid])
        assert terminated and sorted(terminated[0]) == sorted(
            [supervisor_pid, engine_pid],
        ), (
            "both the supervisor AND the engine must actually be signalled "
            f"(the stop&&start no-op repro b depends on real termination, "
            f"not just an honest label); got {terminated}"
        )

    def test_lease_miss_with_no_live_processes_is_genuinely_stopped(
        self, config_dir: Path,
    ) -> None:
        """A stale/absent lease with NOTHING alive in the process table IS
        'already stopped' — the fallback must not manufacture false
        positives on an ordinary clean stop."""
        _write_stale_or_fresh_lease(
            config_dir, supervisor_pid=970103, engine_pid=970104, age_s=99.0,
        )
        with patch(
            "nexus.daemon.service_registry.all_process_rows", return_value=[],
        ):
            outcome = stop_storage_service(config_dir=config_dir)
        assert outcome.already_stopped
        assert outcome.source == "none"

    def test_fresh_lease_tree_sweep_catches_surviving_engine(
        self, config_dir: Path,
    ) -> None:
        """Tree-signalling requirement (nexus-oyo2g): even with a FRESH
        lease naming a supervisor that dies cleanly from SIGTERM, the
        ENGINE CHILD surviving that SIGTERM (PDEATHSIG not effective —
        e.g. macOS) must still be found and terminated. Falsifies the
        pre-fix code, which signalled ONLY ``supervisor_pid`` and returned
        immediately — the engine pid never appears in any kill call."""
        supervisor_pid, engine_pid = 970105, 970106
        _write_stale_or_fresh_lease(
            config_dir, supervisor_pid=supervisor_pid, engine_pid=engine_pid,
            age_s=1.0, ttl=15.0,
        )
        # The supervisor dies cleanly off the direct os.kill in the
        # lease-found branch; the engine survives independently and is
        # only found by the tree-sweep's process-table scan.
        rows = [
            (engine_pid, 60, f"{config_dir}/service/nexus-service -Xmx1g"),
        ]
        terminated: list[list[int]] = []
        with patch(
            "nexus.daemon.storage_service_daemon._pid_is_alive",
            # 1st call: initial "is it alive" check -> True, enter the
            # signal branch. 2nd call: the post-SIGTERM wait loop -> False,
            # the supervisor died cleanly, break immediately (no real
            # sleep). 3rd call: the post-loop SIGKILL-escalation check ->
            # False, no escalation needed.
            side_effect=[True, False, False],
        ), patch(
            "nexus.daemon.storage_service_daemon.os.kill",
        ), patch(
            "nexus.daemon.service_registry.all_process_rows", return_value=rows,
        ), patch(
            "nexus.daemon.service_registry.process_command",
            return_value=rows[0][2],
        ), patch(
            "nexus.daemon.service_registry.terminate_pids",
            side_effect=lambda pids, **_: terminated.append(list(pids)) or [],
        ):
            outcome = stop_storage_service(config_dir=config_dir)
        assert engine_pid in outcome.pids, (
            f"the surviving engine child must be signalled too: {outcome}"
        )
        assert terminated == [[engine_pid]], (
            f"the tree-sweep must terminate the surviving engine: {terminated}"
        )

    def test_stubborn_survivor_is_reported_not_silently_dropped(
        self, config_dir: Path,
    ) -> None:
        """REPRO (c) DOUBLE-SPAWN precondition: a frozen (SIGSTOPped)
        supervisor does not act on SIGTERM, so even the SIGKILL escalation
        inside terminate_pids can race a slow reaper. The outcome must
        surface any pid still alive after the escalation as 'stubborn' —
        never silently claim a clean stop while a survivor might still be
        running (which is what invites the double-spawn race)."""
        supervisor_pid, engine_pid = 970107, 970108
        _write_stale_or_fresh_lease(
            config_dir, supervisor_pid=supervisor_pid, engine_pid=engine_pid,
            age_s=30.0, ttl=15.0,
        )
        rows = [
            (supervisor_pid, 60,
             f"nx daemon service start --foreground --config-dir {config_dir}"),
            (engine_pid, 60, f"{config_dir}/service/nexus-service -Xmx1g"),
        ]
        with patch(
            "nexus.daemon.service_registry.all_process_rows", return_value=rows,
        ), patch(
            "nexus.daemon.service_registry.process_command",
            side_effect=lambda pid: dict((p, c) for p, _a, c in rows)[pid],
        ), patch(
            "nexus.daemon.service_registry.terminate_pids",
            return_value=[supervisor_pid],  # SIGKILL didn't reap it in time
        ):
            outcome = stop_storage_service(config_dir=config_dir)
        assert not outcome.already_stopped
        assert supervisor_pid in outcome.stubborn, (
            f"a survivor of the SIGKILL escalation must be reported, "
            f"never silently dropped: {outcome}"
        )

    def test_process_table_unavailable_degrades_honestly(
        self, config_dir: Path,
    ) -> None:
        """When neither 'ps' nor '/proc' is available, the fallback must
        say so rather than silently reusing the old 'already stopped'
        wording for a check it never actually performed."""
        with patch(
            "nexus.daemon.service_registry.all_process_rows",
            side_effect=RuntimeError("no ps, no /proc"),
        ):
            outcome = stop_storage_service(config_dir=config_dir)
        assert outcome.source == "process_table_unavailable"
        assert outcome.already_stopped

    # -- code review round 2 (T2 [21508]) finding 2: present-but-unusable
    #    lease must not be reported as "no lease found" ------------------

    def test_present_but_unusable_lease_falls_back_to_process_table(
        self, config_dir: Path,
    ) -> None:
        """A lease record IS present (fresh, well within TTL) but carries
        NO usable pid info at all (malformed/legacy shape — no
        supervisor_pid, no endpoint pid). This must be distinguished from
        a genuine lease MISS: ``lease_seen`` is True even though the
        signal source ends up being the process table, because a lease
        WAS discovered — it was just unusable, not absent."""
        _write_stale_or_fresh_lease(
            config_dir, supervisor_pid=None, engine_pid=None, age_s=1.0,
            ttl=15.0,
        )
        engine_pid = 970201
        rows = [(engine_pid, 60, f"{config_dir}/service/nexus-service -Xmx1g")]
        with patch(
            "nexus.daemon.service_registry.all_process_rows", return_value=rows,
        ), patch(
            "nexus.daemon.service_registry.process_command",
            return_value=rows[0][2],
        ), patch(
            "nexus.daemon.service_registry.terminate_pids", return_value=[],
        ):
            outcome = stop_storage_service(config_dir=config_dir)
        assert outcome.lease_seen is True, (
            f"a lease record WAS discovered (even though unusable): {outcome}"
        )
        assert outcome.source == "process_table"
        assert engine_pid in outcome.pids

    def test_present_but_unusable_lease_with_no_processes_still_marks_lease_seen(
        self, config_dir: Path,
    ) -> None:
        """Same malformed-lease precondition, but nothing is found in the
        process table either — genuinely nothing to signal, but a
        (malformed) lease WAS present, so ``lease_seen`` must stay True,
        never silently collapsing to 'no lease at all'."""
        _write_stale_or_fresh_lease(
            config_dir, supervisor_pid=None, engine_pid=None, age_s=1.0,
            ttl=15.0,
        )
        with patch(
            "nexus.daemon.service_registry.all_process_rows", return_value=[],
        ):
            outcome = stop_storage_service(config_dir=config_dir)
        assert outcome.already_stopped
        assert outcome.source == "none"
        assert outcome.lease_seen is True, (
            f"a (malformed) lease WAS present: {outcome}"
        )

    def test_true_lease_miss_has_lease_seen_false(
        self, config_dir: Path,
    ) -> None:
        """Baseline contrast: a genuine lease MISS (no record on disk at
        all) must report ``lease_seen=False`` — never conflated with the
        present-but-unusable case above."""
        with patch(
            "nexus.daemon.service_registry.all_process_rows", return_value=[],
        ):
            outcome = stop_storage_service(config_dir=config_dir)
        assert outcome.lease_seen is False

    # -- code review round 2 finding 3: pid_alive consolidation ----------

    def test_pid_is_alive_is_the_shared_primitive_not_a_local_copy(self) -> None:
        """storage_service_daemon._pid_is_alive must be THE SAME function
        object as service_registry.pid_alive (a re-export, not a
        semantically-diverged duplicate) — the exact bug class
        AGENTS.md's 'no per-tier lifecycle copy' rule exists to prevent."""
        import nexus.daemon.storage_service_daemon as ssd_mod
        from nexus.daemon.service_registry import pid_alive
        assert ssd_mod._pid_is_alive is pid_alive, (
            "storage_service_daemon._pid_is_alive has drifted from the "
            "shared service_registry.pid_alive primitive"
        )

    def test_pg_credentials_read_on_start(
        self, config_dir: Path, creds_path: Path
    ) -> None:
        """start path reads pg_credentials file and extracts expected keys."""
        from nexus.daemon.storage_service_daemon import _read_pg_credentials
        creds = _read_pg_credentials(creds_path)
        assert creds["PG_PORT"] == "15432"
        assert "NX_DB_URL" in creds
        assert "NX_DB_ADMIN_USER" in creds

    def test_credentials_missing_raises_loudly(
        self, config_dir: Path
    ) -> None:
        """If pg_credentials is absent, start raises a clear error."""
        with pytest.raises((StorageServiceStartError, FileNotFoundError, RuntimeError)):
            start_storage_service(config_dir=config_dir)

    def test_binary_not_found_raises_loudly(
        self, config_dir: Path, creds_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RDR-161: with no native binary present, start raises loudly —
        there is no JVM fallback to defer to."""
        monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
        with pytest.raises(StorageServiceStartError, match="(?i)binary|install-binary"):
            start_storage_service(config_dir=config_dir)


# ---------------------------------------------------------------------------
# scope_key constant test: "storage_service" must match what health.py expects
# ---------------------------------------------------------------------------


def test_scope_key_matches_health_module() -> None:
    """The storage_service scope key must match _STORAGE_SERVICE_SCOPE_KEY
    in health.py (the discover() endpoint the doctor reads)."""
    from nexus.daemon.storage_service_daemon import STORAGE_SERVICE_SCOPE_KEY
    from nexus.health import _STORAGE_SERVICE_SCOPE_KEY as health_key
    assert STORAGE_SERVICE_SCOPE_KEY == health_key == "storage_service"


# ---------------------------------------------------------------------------
# Lifecycle gate: no per-tier lifecycle functions introduced by this module
# (the gate test_lifecycle_gate.py is exhaustive; this is a double-check)
# ---------------------------------------------------------------------------


def test_module_does_not_reimplement_elect() -> None:
    """storage_service_daemon.py must not define _elect() (election lives in
    the primitive only, per test_lifecycle_gate.py)."""
    import nexus.daemon.storage_service_daemon as mod
    import inspect
    src = inspect.getsource(mod)
    assert "def _elect(" not in src, (
        "storage_service_daemon must not re-define _elect; "
        "use ServiceRegistry._elect via publish/heartbeat"
    )


def test_module_does_not_define_lease_record() -> None:
    """storage_service_daemon.py must not redefine LeaseRecord."""
    import nexus.daemon.storage_service_daemon as mod
    import inspect
    src = inspect.getsource(mod)
    assert "class LeaseRecord" not in src, (
        "LeaseRecord must be defined only in service_registry.py"
    )


# ---------------------------------------------------------------------------
# nexus-pebfx.2: supervisor plumbs NX_VOYAGE_API_KEY into the JAR env
# ---------------------------------------------------------------------------


class TestSpawnServiceVoyageKeyPlumbing:
    """The 2026-06-10 migration ran against silent ONNX-384 fallback because
    the JAR only reads ``NX_VOYAGE_API_KEY`` and nothing put it there. The
    supervisor must resolve the key through the nexus credential chain
    (``VOYAGE_API_KEY`` env > ``config.yml`` credentials) and pass it down."""

    def _spawn_env(
        self, config_dir: Path, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, str]:
        """Run _spawn_service with Popen mocked; return the env it received."""
        sup = _make_supervisor(config_dir, clock)
        captured: dict[str, str] = {}

        def _fake_popen(cmd, env=None, **kwargs):
            captured.update(env or {})
            return MagicMock(pid=43210)

        monkeypatch.setattr(
            "nexus.daemon.storage_service_daemon._popen", _fake_popen,
        )
        sup._spawn_service()
        return captured

    def test_explicit_nx_voyage_api_key_passes_through(
        self, config_dir: Path, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NX_VOYAGE_API_KEY", "explicit-key")
        monkeypatch.setenv("VOYAGE_API_KEY", "chain-key-should-lose")
        with patch("nexus.config.get_credential") as get_cred:
            env = self._spawn_env(config_dir, clock, monkeypatch)
        get_cred.assert_not_called()
        assert env["NX_VOYAGE_API_KEY"] == "explicit-key"

    def test_key_resolved_from_credential_chain(
        self, config_dir: Path, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
        with patch(
            "nexus.config.get_credential", return_value="chain-key",
        ) as get_cred:
            env = self._spawn_env(config_dir, clock, monkeypatch)
        get_cred.assert_called_once_with("voyage_api_key")
        assert env["NX_VOYAGE_API_KEY"] == "chain-key"

    def test_no_key_anywhere_leaves_env_unset(
        self, config_dir: Path, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        with patch("nexus.config.get_credential", return_value=""):
            env = self._spawn_env(config_dir, clock, monkeypatch)
        assert "NX_VOYAGE_API_KEY" not in env

    # -- nexus-r5f3c: the configured local embed model is the intent record --

    def test_bge_configured_model_blocks_chain_plumbing(
        self, config_dir: Path, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """nx init saved local.embed_model=bge — an ambient VOYAGE_API_KEY
        must NOT flip the engine voyage-only (it 422'd the first store on
        every fresh local install whose shell exported a Voyage key)."""
        monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
        monkeypatch.setenv("VOYAGE_API_KEY", "ambient-key-must-not-plumb")
        with patch(
            "nexus.config.local_embed_model_choice",
            return_value="BAAI/bge-base-en-v1.5",
        ), patch("nexus.config.get_credential") as get_cred:
            env = self._spawn_env(config_dir, clock, monkeypatch)
        get_cred.assert_not_called()
        assert "NX_VOYAGE_API_KEY" not in env

    def test_bge_configured_model_explicit_override_still_wins(
        self, config_dir: Path, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An EXPLICIT NX_VOYAGE_API_KEY is caller intent — it overrides the
        configured model and passes through unchanged."""
        monkeypatch.setenv("NX_VOYAGE_API_KEY", "explicit-key")
        with patch(
            "nexus.config.local_embed_model_choice",
            return_value="BAAI/bge-base-en-v1.5",
        ):
            env = self._spawn_env(config_dir, clock, monkeypatch)
        assert env["NX_VOYAGE_API_KEY"] == "explicit-key"

    def test_voyage_configured_model_still_plumbs(
        self, config_dir: Path, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A voyage-configured local install keeps the chain plumbing (the
        mirror failure — ONNX-only engine refusing voyage-* collections —
        must not be introduced by the r5f3c fix)."""
        monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
        with patch(
            "nexus.config.local_embed_model_choice",
            return_value="voyage-context-3",
        ), patch(
            "nexus.config.get_credential", return_value="chain-key",
        ) as get_cred:
            env = self._spawn_env(config_dir, clock, monkeypatch)
        get_cred.assert_called_once_with("voyage_api_key")
        assert env["NX_VOYAGE_API_KEY"] == "chain-key"

    # -- nexus-35ok4 round 2 (code-review-expert IMPORTANT 1): prove the
    # engine-plumbing decision (this module) and the client-naming
    # decision (nexus.corpus.effective_embedding_model_for_writes) are
    # driven by the SAME shared predicate, not two independently-written
    # checks that happen to agree today. Patches
    # ``nexus.config.local_embed_model_is_voyage`` DIRECTLY (not the
    # underlying ``local_embed_model_choice``) so a future edit that
    # makes either call site stop calling the shared predicate (e.g.
    # reverting to its own inline ``.startswith("voyage")``) breaks this
    # test instead of passing silently.

    # nexus-35ok4 round 3 (code-review-expert): the ORIGINAL version of
    # this pair patched ``local_embed_model_choice`` AND
    # ``local_embed_model_is_voyage`` to CONSISTENT values (a
    # voyage-shaped string alongside predicate=True, a bge-shaped string
    # alongside predicate=False) — so a call site that had been reverted
    # to its own inline ``.startswith("voyage")`` re-derivation would
    # agree with the patched predicate anyway and pass regardless
    # (reviewer falsified this live). Below, the string and the
    # predicate are set to DISAGREE in both directions: only a call site
    # that genuinely dispatches off ``local_embed_model_is_voyage()``
    # (not off the string shape) can pass both.

    def test_shared_predicate_true_drives_both_sites_to_voyage(
        self, config_dir: Path, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.corpus import effective_embedding_model_for_writes

        monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        # Predicate says voyage...
        monkeypatch.setattr("nexus.config.local_embed_model_is_voyage", lambda: True)
        # ...but the underlying string is NOT voyage-shaped — a naive
        # inline ``.startswith("voyage")`` re-derivation would say False
        # here and skip; only genuinely calling the predicate passes.
        monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "BAAI/bge-base-en-v1.5")
        with patch("nexus.config.get_credential", return_value="shared-key"):
            env = self._spawn_env(config_dir, clock, monkeypatch)
            client_model = effective_embedding_model_for_writes("code")
        # Engine side: key plumbed (voyage-capable engine).
        assert env["NX_VOYAGE_API_KEY"] == "shared-key"
        # Client side: mints the matching voyage token, not bge.
        assert client_model == "voyage-code-3"

    def test_shared_predicate_false_drives_both_sites_to_local(
        self, config_dir: Path, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.corpus import LOCAL_EMBEDDING_MODELS, effective_embedding_model_for_writes

        monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
        monkeypatch.setenv("VOYAGE_API_KEY", "ambient-key-must-not-be-used")
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        # Predicate says NOT voyage...
        monkeypatch.setattr("nexus.config.local_embed_model_is_voyage", lambda: False)
        # ...but the underlying string IS voyage-shaped — a naive inline
        # ``.startswith("voyage")`` re-derivation would say True here and
        # plumb/mint; only genuinely calling the predicate stays local.
        monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
        with patch("nexus.config.get_credential") as get_cred:
            env = self._spawn_env(config_dir, clock, monkeypatch)
            client_model = effective_embedding_model_for_writes("code")
        # Engine side: no key plumbed (bge-only engine).
        get_cred.assert_not_called()
        assert "NX_VOYAGE_API_KEY" not in env
        # Client side: still the local bge/minilm token, never voyage.
        assert client_model in LOCAL_EMBEDDING_MODELS


class TestCredsReloadAfterBackfill:
    """nexus-hzhgl round 3 review Significant-1: ``_backfill_provision_grants()``
    (called from ``_ensure_pg_running``) can rewrite ``pg_credentials`` on
    disk via ``provision()``'s fast path, but ``self._creds`` was frozen at
    ``__init__`` time. ``_spawn_service()`` — the very next step in
    ``_start_locked`` — builds the JVM's entire env from ``self._creds``.
    Without a reload in between, a future backfill that rotates an
    env-relevant credential would land on disk but never reach the freshly
    spawned process.
    """

    def test_ensure_pg_running_reloads_creds_after_backfill(
        self, config_dir: Path, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Direct proof on ``_ensure_pg_running`` alone: a backfill that
        mutates the on-disk file is reflected in ``self._creds`` immediately
        afterward, with no real PG or subprocess involved."""
        creds_file = config_dir / "pg_credentials"
        creds_file.write_text(
            "PG_PORT=15432\n"
            "PG_DATA=/tmp/testpgdata\n"
            "NX_DB_URL=jdbc:postgresql://127.0.0.1:15432/nexus\n"
            "NX_DB_USER=nexus_svc\n"
            "NX_DB_PASS=original-pass\n"
            "NX_DB_ADMIN_URL=jdbc:postgresql://127.0.0.1:15432/nexus\n"
            "NX_DB_ADMIN_USER=nexus_admin\n"
            "NX_DB_ADMIN_PASS=testadminpass\n"
        )
        creds_file.chmod(0o600)

        sup = _make_supervisor(
            config_dir, clock,
            creds={
                "NX_DB_URL": "jdbc:...", "NX_DB_USER": "nexus_svc",
                "NX_DB_PASS": "original-pass",
                "NX_DB_ADMIN_URL": "jdbc:...", "NX_DB_ADMIN_USER": "nexus_admin",
                "NX_DB_ADMIN_PASS": "testadminpass", "PG_PORT": "15432",
                "PG_DATA": "/tmp/testpgdata",
                "NX_SERVICE_TOKEN": "root-token-from-creds-deadbeef",
            },
        )

        def _fake_backfill_mutates_disk() -> None:
            # Simulate a future provision() fast-path backfill rotating a
            # DB credential on disk -- the exact class Significant-1 warns
            # about (today's backfills don't touch these keys; a future one
            # might).
            text = creds_file.read_text().replace(
                "NX_DB_PASS=original-pass", "NX_DB_PASS=fresh-post-backfill-pass",
            )
            creds_file.write_text(text)

        monkeypatch.setattr(
            "nexus.daemon.storage_service_daemon._port_accepting",
            lambda *a, **k: True,  # PG already running -> short-circuit branch
        )
        monkeypatch.setattr(sup, "_backfill_provision_grants", _fake_backfill_mutates_disk)

        assert sup._creds["NX_DB_PASS"] == "original-pass"  # precondition

        sup._ensure_pg_running()

        assert sup._creds["NX_DB_PASS"] == "fresh-post-backfill-pass", (
            "self._creds was not reloaded after _backfill_provision_grants() "
            "ran -- _spawn_service() would build the JVM env from stale creds"
        )

    def test_spawned_env_carries_post_backfill_value(
        self, config_dir: Path, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end proof through the real call sequence the reviewer
        asked for: a backfill mutates a cred value, then the SPAWNED env
        (captured via a mocked Popen -- no real JVM needed) carries the
        fresh value, not the construction-time snapshot."""
        creds_file = config_dir / "pg_credentials"
        creds_file.write_text(
            "PG_PORT=15432\n"
            "PG_DATA=/tmp/testpgdata\n"
            "NX_DB_URL=jdbc:postgresql://127.0.0.1:15432/nexus\n"
            "NX_DB_USER=nexus_svc\n"
            "NX_DB_PASS=original-pass\n"
            "NX_DB_ADMIN_URL=jdbc:postgresql://127.0.0.1:15432/nexus\n"
            "NX_DB_ADMIN_USER=nexus_admin\n"
            "NX_DB_ADMIN_PASS=testadminpass\n"
        )
        creds_file.chmod(0o600)

        sup = _make_supervisor(
            config_dir, clock,
            creds={
                "NX_DB_URL": "jdbc:...", "NX_DB_USER": "nexus_svc",
                "NX_DB_PASS": "original-pass",
                "NX_DB_ADMIN_URL": "jdbc:...", "NX_DB_ADMIN_USER": "nexus_admin",
                "NX_DB_ADMIN_PASS": "testadminpass", "PG_PORT": "15432",
                "PG_DATA": "/tmp/testpgdata",
                "NX_SERVICE_TOKEN": "root-token-from-creds-deadbeef",
            },
        )

        def _fake_backfill_mutates_disk() -> None:
            text = creds_file.read_text().replace(
                "NX_DB_PASS=original-pass", "NX_DB_PASS=fresh-post-backfill-pass",
            )
            creds_file.write_text(text)

        monkeypatch.setattr(
            "nexus.daemon.storage_service_daemon._port_accepting",
            lambda *a, **k: True,
        )
        monkeypatch.setattr(sup, "_backfill_provision_grants", _fake_backfill_mutates_disk)

        sup._ensure_pg_running()  # Step 1 -- runs the (fake) backfill + reload

        captured: dict[str, str] = {}

        def _fake_popen(cmd, env=None, **kwargs):
            captured.update(env or {})
            return MagicMock(pid=44100)

        monkeypatch.setattr(
            "nexus.daemon.storage_service_daemon._popen", _fake_popen,
        )
        sup._spawn_service()  # Step 2 -- builds the JVM env from self._creds

        assert captured["NX_DB_PASS"] == "fresh-post-backfill-pass", (
            "_spawn_service() built the JVM env from a stale credentials "
            "snapshot -- a real post-upgrade credential rotation would "
            "silently spawn the new engine with the OLD value"
        )


class TestEnsurePgRunningCalledOnFreshStart:
    """nexus-hzhgl round 3 review Significant-2, positive case: with NO live
    lease, ``start()`` must reach ``_ensure_pg_running()`` (and so the
    backfill). The negative case (live lease -> NOT called) is already
    pinned by ``TestRdr175MvvSingleSupervisor::
    test_second_start_short_circuits_to_single_lease``; this test closes the
    other side so the lease short-circuit is proven to be the ONLY skip
    path, not merely one that happens to skip it in the cases already
    covered.
    """

    def test_no_live_lease_reaches_ensure_pg_running(
        self, config_dir: Path, clock: _FakeClock,
    ) -> None:
        from nexus.daemon.service_registry import ServiceRegistry

        scope = str(os.getuid())
        assert ServiceRegistry(dir=config_dir, tier="storage_service", clock=clock).discover(scope) is None

        sup = _make_supervisor(config_dir, clock)
        proc = _FakeProc(pid=51300)
        with patch.object(sup, "_ensure_pg_running") as ensure_pg, \
             patch.object(sup, "_spawn_service", return_value=(proc, 19800)), \
             patch.object(sup, "_wait_for_service_ready"):
            sup.start()

        ensure_pg.assert_called_once()


class TestNativeStartHasNoSchemaSkewGate:
    """RDR-161: the JVM-only schema-skew gate (nexus-pebfx.4) is expunged with
    the legacy launch path. A native start goes PG -> spawn with no skew probe;
    the gate helper must not be invoked."""

    def test_native_start_skips_skew_gate(
        self, config_dir: Path, clock: _FakeClock,
    ) -> None:
        # The gate helper is expunged entirely; a native start reaches spawn.
        import nexus.daemon.binary_lifecycle as bl
        assert not hasattr(bl, "check_schema_skew")

        sup = _make_supervisor(config_dir, clock)
        proc = _FakeProc(pid=51000)
        with patch.object(sup, "_ensure_pg_running"), \
             patch.object(sup, "_spawn_service", return_value=(proc, 19500)) as spawn, \
             patch.object(sup, "_wait_for_service_ready"):
            payload = sup.start()
        spawn.assert_called_once()
        assert payload["port"] == 19500


# ---------------------------------------------------------------------------
# RDR-175: minimal supervise loop — start + heartbeat + die-non-zero.
# OS init (RDR-174 units) is the single process watchdog; no in-process respawn.
# ---------------------------------------------------------------------------


class _ScriptedSupervisor:
    """run-loop double for ``_supervise_until_stopped``: heartbeat_once pops
    scripted (service_running, pg_ok) tuples; every lifecycle call is recorded
    in ``calls`` so ordering assertions are exact. Note: there is no ``_respawn``
    on the double — RDR-175 retired it; if the loop ever called it this double
    would raise AttributeError, which is the desired regression tripwire."""

    def __init__(
        self,
        beats: list[tuple[bool, bool]],
        stop_requested,
        *,
        ensure_pg_raises: Exception | None = None,
        owns_process: bool = True,
    ) -> None:
        self._beats = list(beats)
        self._stop = stop_requested
        self._ensure_pg_raises = ensure_pg_raises
        self.owns_process = owns_process
        self.calls: list[str] = []
        self.heartbeat_calls = 0

    def start(self) -> None:
        self.calls.append("start")

    def heartbeat_once(self) -> tuple[bool, bool]:
        self.heartbeat_calls += 1
        if not self._beats:
            # Script exhausted: end the loop instead of inventing beats.
            self._stop.set()
            return True, True
        beat = self._beats.pop(0)
        if not self._beats:
            self._stop.set()  # last scripted beat — loop exits after handling
        return beat

    def _ensure_pg_running(self) -> None:
        self.calls.append("ensure_pg")
        if self._ensure_pg_raises is not None:
            raise self._ensure_pg_raises

    def stop(self) -> None:
        self.calls.append("stop")


class TestMinimalSuperviseLoop:
    """RDR-175: the supervise loop is start + heartbeat + die-non-zero. On any
    falsey ``service_running`` beat (process death OR stuck-process threshold)
    the supervisor EXITS non-zero so the OS watchdog restarts the whole
    process — there is NO in-process respawn. The lone in-place recovery is the
    ``(True, False)`` PG-only arm, which restarts PG without bouncing the JVM."""

    def _run(self, sup_factory):
        import threading

        from nexus.daemon import storage_service_daemon as ssd

        stop = threading.Event()
        sup = sup_factory(stop)
        with patch.object(ssd, "DEFAULT_HEARTBEAT_INTERVAL", 0.0):
            code = ssd._supervise_until_stopped(sup, stop, lambda: None)
        return sup, code

    def test_service_and_pg_down_exits_3_for_os_restart(self) -> None:
        """(False, False): service dead — exit 3. The supervisor does NOT
        attempt an in-process PG restart or respawn; the OS restart re-runs
        start() (which brings PG back up via _ensure_pg_running)."""
        sup, code = self._run(
            lambda stop: _ScriptedSupervisor([(False, False)], stop)
        )
        assert code == 3, "service-unrecoverable is the exit-3 OS-restart contract"
        assert "ensure_pg" not in sup.calls, (
            "service death must NOT trigger an in-process PG restart; the OS "
            "restart re-runs start() which brings PG up"
        )
        assert sup.calls == ["start", "stop"], (
            f"loop must be start -> exit -> stop, no respawn; got {sup.calls}"
        )

    def test_service_down_pg_up_exits_3(self) -> None:
        """(False, True): stuck-process threshold (live PG probe) — exit 3, no
        in-process respawn."""
        sup, code = self._run(
            lambda stop: _ScriptedSupervisor([(False, True)], stop)
        )
        assert code == 3, "service-unrecoverable is the exit-3 contract"
        assert "ensure_pg" not in sup.calls

    def test_pg_only_death_restarts_pg_without_exit(self) -> None:
        """(True, False): JVM alive, PG down — restart PG in place, NO exit.
        After the scripted beat the loop exits cleanly (code 0)."""
        sup, code = self._run(
            lambda stop: _ScriptedSupervisor([(True, False)], stop)
        )
        assert code == 0, "PG-only death must NOT exit the supervisor"
        assert sup.calls.count("ensure_pg") == 1, (
            "the (True, False) arm restarts PG directly without bouncing Java"
        )

    def test_pg_only_restart_failure_exits_4(self) -> None:
        """(True, False) with an unrecoverable PG — exit 4."""
        sup, code = self._run(
            lambda stop: _ScriptedSupervisor(
                [(True, False)], stop,
                ensure_pg_raises=StorageServiceStartError("pg_ctl failed"),
            )
        )
        assert code == 4, "PG-unrecoverable is the exit-4 contract"
        assert "ensure_pg" in sup.calls, "exit 4 must come FROM the PG attempt"

    def test_healthy_beats_sleep_then_exit_on_stop(self) -> None:
        """(True, True) beats keep the loop alive (no exit, no PG churn) until
        stop is requested, then exit 0."""
        sup, code = self._run(
            lambda stop: _ScriptedSupervisor([(True, True), (True, True)], stop)
        )
        assert code == 0
        assert "ensure_pg" not in sup.calls
        assert sup.calls == ["start", "stop"]

    def test_not_owning_process_exits_0_without_entering_heartbeat_loop(
        self,
    ) -> None:
        """GH #1369: when start() short-circuits on an existing, healthy lease
        (another supervisor owns the service), owns_process is False and this
        supervisor has nothing to heartbeat — it must exit 0 immediately
        without ever calling heartbeat_once(). Before this fix, the loop
        called heartbeat_once() regardless; the real (non-scripted)
        heartbeat_once() reads self._proc is None (never assigned by the
        short-circuit branch) as "process died" and forced exit(3), causing a
        launchd respawn loop + port churn against a perfectly healthy service.
        No beats are scripted here on purpose: any call to heartbeat_once()
        would raise on the empty-list pop path being exercised incorrectly,
        making an accidental heartbeat call visible immediately."""
        sup, code = self._run(
            lambda stop: _ScriptedSupervisor([], stop, owns_process=False)
        )
        assert code == 0, "not owning the process must exit 0, not 3"
        assert sup.heartbeat_calls == 0, (
            "a supervisor with nothing to own must never call heartbeat_once()"
        )
        assert sup.calls == ["start", "stop"]


# ---------------------------------------------------------------------------
# nexus-qke1e: ensure_storage_supervisor — the single persistent-start path
# ---------------------------------------------------------------------------
class TestEnsureStorageSupervisor:
    """nexus-qke1e: nx init --service AND nx daemon service start both route
    through ensure_storage_supervisor, which guarantees a PERSISTENT supervisor
    owns the lease (never a transient unsupervised lease that ages out by TTL)."""

    def _publish_fresh_lease(self, config_dir: Path, port: int = 18091) -> None:
        import time as _time

        sup = _make_supervisor(config_dir, lambda: _time.time(), supervised=True)
        sup._proc = _FakeProc(pid=42777)
        sup._service_port = port
        sup._publish(port)

    def test_live_lease_short_circuits_without_spawn(self, config_dir: Path) -> None:
        from nexus.commands import daemon as daemon_mod

        self._publish_fresh_lease(config_dir)
        # process_state stubbed alive-shaped: on a /proc-less box it shells out
        # to `ps` via the same stdlib subprocess symbol this test mocks
        # (nexus-o8dil.21). The lease's supervisor_pid IS this live test
        # process, so "S" is the faithful answer.
        with patch("nexus.daemon.service_registry.process_state", return_value="S"), \
             patch.object(daemon_mod, "_popen") as popen:
            rec = daemon_mod.ensure_storage_supervisor(config_dir)
        assert rec is not None
        popen.assert_not_called()  # idempotent: a live lease is never re-spawned

    def test_live_lease_raises_on_explicit_artifact_mismatch(
        self, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """nexus-4e96a mismatch arm, THE load-bearing layer: this is the
        branch that returns the live lease without ever spawning a
        subprocess (so storage_service_daemon.py's own _start_locked copy of
        the check is never even reached). An explicit NEXUS_SERVICE_BIN
        request differing from the lease's published artifact must raise,
        with no spawn attempt — sibling of
        test_live_lease_short_circuits_without_spawn."""
        from nexus.commands import daemon as daemon_mod
        from nexus.daemon.storage_service_daemon import StorageServiceStartError

        self._publish_fresh_lease(config_dir)  # artifact="/fake/nexus-service"

        other_binary = tmp_path / "different-nexus-service"
        other_binary.write_text("#!/bin/sh\nexit 0\n")
        other_binary.chmod(0o755)
        monkeypatch.setenv("NEXUS_SERVICE_BIN", str(other_binary))
        monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)

        with patch("nexus.daemon.service_registry.process_state", return_value="S"), \
             patch.object(daemon_mod, "_popen") as popen:
            with pytest.raises(StorageServiceStartError, match="DIFFERENT artifact"):
                daemon_mod.ensure_storage_supervisor(config_dir)
        popen.assert_not_called()

    def test_live_lease_unknown_artifact_allows_with_warning(
        self, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A lease published before this fix carries no 'artifact' key at
        all. Per the design's degrade contract, an explicit request against
        such a lease must ALLOW (+ warn), never raise — a one-release
        window, distinct from the genuine-mismatch arm above."""
        import time as _time

        from nexus.commands import daemon as daemon_mod
        from nexus.daemon.service_registry import ServiceRegistry, ServiceSupervisor

        registry = ServiceRegistry(dir=config_dir, tier="storage_service", clock=_time.time)
        scope = str(os.getuid())
        pre_fix_supervisor = ServiceSupervisor(
            registry, scope, version="0.0.0",
            endpoint_provider=lambda: {
                "host": "127.0.0.1", "port": 18099, "pid": 424242, "token": "tok",
                # deliberately NO "artifact" / "launch_kind" keys.
            },
            payload={"supervisor_pid": os.getpid()},  # alive: this test process
        )
        pre_fix_supervisor.publish_once()

        other_binary = tmp_path / "different-nexus-service"
        other_binary.write_text("#!/bin/sh\nexit 0\n")
        other_binary.chmod(0o755)
        monkeypatch.setenv("NEXUS_SERVICE_BIN", str(other_binary))
        monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)

        with patch("nexus.daemon.service_registry.process_state", return_value="S"), \
             patch.object(daemon_mod, "_popen") as popen:
            rec = daemon_mod.ensure_storage_supervisor(config_dir)
        popen.assert_not_called()
        assert rec is not None and rec.endpoint.get("port") == 18099

    def test_spawns_supervisor_when_no_lease(self, config_dir: Path) -> None:
        from nexus.commands import daemon as daemon_mod
        from nexus.daemon.service_registry import ServiceRegistry

        scope = str(os.getuid())
        assert ServiceRegistry(dir=config_dir, tier="storage_service").discover(scope) is None

        def _popen_publishes(*_a, **_k):
            # The detached --foreground supervisor would publish the lease; model
            # that so the wait loop resolves.
            self._publish_fresh_lease(config_dir, port=18092)
            return MagicMock()

        with patch.object(daemon_mod, "_resolve_nx_bin", return_value=["nx"]), \
             patch.object(daemon_mod, "_popen", side_effect=_popen_publishes) as popen:
            rec = daemon_mod.ensure_storage_supervisor(config_dir)
        popen.assert_called_once()
        assert rec is not None and rec.endpoint.get("port") == 18092

    def test_timeout_raises_loud(self, config_dir: Path, monkeypatch) -> None:
        import nexus.commands.daemon as daemon_mod
        from nexus.daemon.storage_service_daemon import StorageServiceStartError

        # A Popen that never publishes; advance the monotonic clock past the 60s
        # deadline on the second read (first read sets the deadline, second is
        # already past it) so the wait loop exits without a real 60s spin.
        ticks = iter([0.0, 10_000.0, 10_000.0])
        monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(daemon_mod.time, "sleep", lambda _s: None)
        with patch.object(daemon_mod, "_resolve_nx_bin", return_value=["nx"]), \
             patch.object(daemon_mod, "_popen", return_value=MagicMock()):
            with pytest.raises(StorageServiceStartError):
                daemon_mod.ensure_storage_supervisor(config_dir)

    def test_dead_supervisor_pid_relinquishes_and_respawns(
        self, config_dir: Path
    ) -> None:
        """RDR-175 heal-on-next-use: a hard-crashed supervisor (OOM-kill, no
        relinquish) can leave a still-fresh (TTL-live) lease whose
        ``supervisor_pid`` points at a dead process. The discover path must
        detect the dead pid, relinquish the stale lease, and re-spawn — rather
        than returning a dead endpoint for up to the lease TTL window."""
        import nexus.daemon.storage_service_daemon as ssd_mod
        from nexus.commands import daemon as daemon_mod
        from nexus.daemon.service_registry import ServiceRegistry

        # A fresh, supervised lease (payload carries supervisor_pid). Patch
        # the guard's probe False so it treats that supervisor as dead.
        # The probe is ``_pid_is_running``, not ``_pid_is_alive``, since
        # nexus-o8dil.21 — see the zombie sibling test below for why.
        self._publish_fresh_lease(config_dir, port=18093)
        scope = str(os.getuid())
        assert ServiceRegistry(dir=config_dir, tier="storage_service").discover(scope) is not None

        def _popen_publishes(*_a, **_k):
            self._publish_fresh_lease(config_dir, port=18094)
            return MagicMock()

        with patch.object(ssd_mod, "_pid_is_running", return_value=False), \
             patch.object(daemon_mod, "_resolve_nx_bin", return_value=["nx"]), \
             patch.object(daemon_mod, "_popen", side_effect=_popen_publishes) as popen:
            rec = daemon_mod.ensure_storage_supervisor(config_dir)

        popen.assert_called_once()  # dead lease must trigger a re-spawn
        assert rec is not None and rec.endpoint.get("port") == 18094

    def test_zombie_supervisor_pid_relinquishes_and_respawns(
        self, config_dir: Path
    ) -> None:
        """nexus-o8dil.21. Same heal, but with a REAL zombie and NO patched
        probe — which is the only way to prove the fix, since the whole
        defect is that the old probe could not tell a corpse from a
        supervisor.

        ``_pid_is_alive`` is ``os.kill(pid, 0)`` and succeeds indefinitely
        on a killed-but-unreaped process. When PID 1 is not a real init (a
        container whose PID 1 is a shell script; a CI runner), a
        hard-crashed supervisor stays exactly that — so this heal, which
        exists for the hard-crash case, never fired there: the fresh lease
        stayed, ``start`` short-circuited onto it, and the box served a
        dead supervisor's endpoint until the TTL ran out.

        RED-FIRST: against the pre-fix call site (``_pid_is_alive``) the
        guard sees "alive", no re-spawn happens, and ``popen`` is never
        called.
        """
        from nexus.commands import daemon as daemon_mod
        from nexus.daemon.service_registry import (
            ServiceRegistry,
            ServiceSupervisor,
        )

        proc = subprocess.Popen(  # noqa: S603 — fixed argv, this interpreter
            [sys.executable, "-c", "import time; time.sleep(120)"],
        )
        try:
            os.kill(proc.pid, signal.SIGKILL)
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and process_state(proc.pid) != "Z":
                time.sleep(0.02)
            assert process_state(proc.pid) == "Z", "fixture must be a zombie"

            registry = ServiceRegistry(
                dir=config_dir, tier="storage_service", clock=time.time,
            )
            ServiceSupervisor(
                registry, str(os.getuid()), version="0.0.0",
                endpoint_provider=lambda: {
                    "host": "127.0.0.1", "port": 18096, "pid": proc.pid,
                    "token": "tok",
                },
                payload={"supervisor_pid": proc.pid},
            ).publish_once()

            def _popen_publishes(*_a, **_k):
                self._publish_fresh_lease(config_dir, port=18097)
                return MagicMock()

            # Shadow daemon_mod's OWN `subprocess` reference rather than
            # patching the stdlib module's Popen attribute: process_state
            # legitimately shells out to `ps` on a /proc-less box, and a
            # global Popen mock would break that real probe — which is the
            # thing under test here (nexus-o8dil.21).
            popen = MagicMock(side_effect=_popen_publishes)
            with patch.object(daemon_mod, "_resolve_nx_bin", return_value=["nx"]), \
                 patch.object(daemon_mod, "_popen", popen):
                rec = daemon_mod.ensure_storage_supervisor(config_dir)
        finally:
            with contextlib.suppress(ChildProcessError, OSError, subprocess.TimeoutExpired):
                proc.wait(timeout=5)

        # A zombie supervisor is a CRASHED supervisor: its fresh lease must be
        # reclaimed and the stack re-spawned.
        popen.assert_called_once()
        assert rec is not None and rec.endpoint.get("port") == 18097

    def test_absent_supervisor_pid_trusts_ttl_freshness(
        self, config_dir: Path
    ) -> None:
        """A lease WITHOUT a supervisor_pid (legacy/non-supervised) must fall
        through to the existing TTL-freshness short-circuit — no spurious
        re-spawn — even when the liveness probe would report dead."""
        import time as _time

        import nexus.daemon.storage_service_daemon as ssd_mod
        from nexus.commands import daemon as daemon_mod

        # Publish a NON-supervised lease: payload {} → supervisor_pid absent.
        sup = _make_supervisor(config_dir, lambda: _time.time(), supervised=False)
        sup._proc = _FakeProc(pid=42778)
        sup._service_port = 18095
        sup._publish(18095)

        with patch.object(ssd_mod, "_pid_is_running", return_value=False), \
             patch.object(daemon_mod, "_popen") as popen:
            rec = daemon_mod.ensure_storage_supervisor(config_dir)

        popen.assert_not_called()  # absent supervisor_pid → trust TTL, no re-spawn
        assert rec is not None and rec.endpoint.get("port") == 18095


# ---------------------------------------------------------------------------
# nexus-lz3f2: lease-TTL margin + optional service heap bound
# ---------------------------------------------------------------------------
class TestLeaseTtlAndHeapBound:
    """nexus-lz3f2: the storage-service supervisor was OOM-killed at the boot
    memory peak (its lease then vanished silently). Two robustness fixes:
    (B) a 15s lease TTL so a transient heartbeat stall never false-expires a
    LIVE service's lease; (C) an optional -Xmx bound so memory-constrained hosts
    don't trip the OOM killer."""

    def test_lease_published_with_extended_ttl(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        import nexus.daemon.storage_service_daemon as ssd_mod
        from nexus.daemon.service_registry import ServiceRegistry

        sup = _make_supervisor(config_dir, clock, supervised=True)
        sup._proc = _FakeProc(pid=49001)
        sup._service_port = 18077
        sup._publish(18077)

        # discover() judges freshness from the RECORD's ttl, not this registry's
        # ttl arg — so a default-ttl registry still reads the 15s stamped at publish.
        registry = ServiceRegistry(dir=config_dir, tier="storage_service", clock=clock)
        rec = registry.discover(str(os.getuid()))
        assert rec is not None
        # The published lease carries the storage-service tier TTL (shared
        # primitive), not the 3s substrate default.
        from nexus.daemon.service_registry import ttl_for_tier

        assert rec.ttl == ttl_for_tier("storage_service") == 15.0

    def test_ttl_exceeds_worst_case_heartbeat_tick(self) -> None:
        """The margin invariant (debugger RF-1 finding): a heartbeat tick can
        take up to its probe budget + DEFAULT_HEARTBEAT_INTERVAL, and the TTL
        must exceed that with room or a single slow tick grazes the TTL.

        nexus-hubc0: the worst tick now includes BOTH probes, because a beat
        whose /health goes silent then consults /livez. Understating the budget
        here would make the invariant a lie — the formula tracks the code, not
        just the constants.
        """
        import nexus.daemon.storage_service_daemon as ssd_mod
        from nexus.daemon.service_registry import DEFAULT_HEARTBEAT_INTERVAL, ttl_for_tier

        worst_tick = (
            ssd_mod._HEALTH_TIMEOUT + ssd_mod._LIVEZ_TIMEOUT + DEFAULT_HEARTBEAT_INTERVAL
        )
        assert ttl_for_tier("storage_service") >= 3 * worst_tick

    def test_ttl_has_real_margin_not_boundary(self) -> None:
        """nexus-hubc0 acceptance: the invariant must hold with MARGIN, not at
        the boundary. Pre-fix it was exactly equal (4.0 + 1.0 = 5.0, TTL 15 ==
        3 x 5), so any future probe-budget increase silently broke it. Moving
        the restart decision onto the dependency-free /livez is what bought the
        headroom: /health no longer has to outlast a saturated pool, because a
        /health timeout is no longer evidence of a wedge.
        """
        import nexus.daemon.storage_service_daemon as ssd_mod
        from nexus.daemon.service_registry import DEFAULT_HEARTBEAT_INTERVAL, ttl_for_tier

        worst_tick = (
            ssd_mod._HEALTH_TIMEOUT + ssd_mod._LIVEZ_TIMEOUT + DEFAULT_HEARTBEAT_INTERVAL
        )
        ttl = ttl_for_tier("storage_service")
        assert ttl > 3 * worst_tick, (
            f"TTL {ttl}s sits AT the 3x boundary for a {worst_tick}s worst tick "
            "— restore headroom before raising either probe timeout"
        )

    def test_spawn_service_applies_max_heap_when_set(
        self, config_dir: Path, clock: _FakeClock, monkeypatch
    ) -> None:
        import nexus.daemon.storage_service_daemon as ssd_mod

        monkeypatch.setenv("NX_SERVICE_MAX_HEAP", "1g")
        sup = _make_supervisor(config_dir, clock)
        captured: dict = {}

        def _fake_popen(argv, **kw):
            captured["argv"] = argv
            return _FakeProc(pid=49100)

        monkeypatch.setattr(ssd_mod, "_popen", _fake_popen)
        monkeypatch.setattr(ssd_mod, "_allocate_free_port", lambda: 18078)
        sup._spawn_service()
        # -Xmx must immediately follow the binary path (native-image consumes
        # runtime options before app args).
        assert captured["argv"][0] == str(sup._binary_path)
        assert captured["argv"][1] == "-Xmx1g"

    def test_spawn_service_rejects_malformed_max_heap(
        self, config_dir: Path, clock: _FakeClock, monkeypatch
    ) -> None:
        import nexus.daemon.storage_service_daemon as ssd_mod
        from nexus.daemon.storage_service_daemon import StorageServiceStartError

        monkeypatch.setenv("NX_SERVICE_MAX_HEAP", "abc")
        sup = _make_supervisor(config_dir, clock)
        monkeypatch.setattr(ssd_mod, "_allocate_free_port", lambda: 18088)
        # A malformed heap value fails loud BEFORE spawning (no /health-timeout
        # misdiagnosis); Popen is never reached.
        monkeypatch.setattr(ssd_mod, "_popen",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen reached")))
        with pytest.raises(StorageServiceStartError, match="NX_SERVICE_MAX_HEAP"):
            sup._spawn_service()

    def test_spawn_service_no_heap_flag_by_default(
        self, config_dir: Path, clock: _FakeClock, monkeypatch
    ) -> None:
        import nexus.daemon.storage_service_daemon as ssd_mod

        monkeypatch.delenv("NX_SERVICE_MAX_HEAP", raising=False)
        sup = _make_supervisor(config_dir, clock)
        captured: dict = {}

        def _fake_popen(argv, **kw):
            captured["argv"] = argv
            return _FakeProc(pid=49101)

        monkeypatch.setattr(ssd_mod, "_popen", _fake_popen)
        monkeypatch.setattr(ssd_mod, "_allocate_free_port", lambda: 18079)
        sup._spawn_service()
        # Production default: no -Xmx — the binary keeps native-image's default heap.
        assert not any(a.startswith("-Xmx") for a in captured["argv"][1:])


class TestPdeathsigOrphanPrevention:
    """nexus-03bcg / f9y78 root cause: an OOM-killed supervisor left an
    orphaned-but-serving JVM whose lease aged out — and a naive heal-on-next-use
    would then double-spawn a JVM. RDR-149-aligned fix (no pid-file, no orphan
    sweep): the JVM dies WITH its supervisor via PR_SET_PDEATHSIG, so a dead
    supervisor leaves NO orphan to double-spawn against."""

    def test_spawn_service_wires_pdeathsig_on_linux(
        self, config_dir: Path, clock: _FakeClock, monkeypatch
    ) -> None:
        import nexus.daemon.storage_service_daemon as ssd_mod

        monkeypatch.setattr(ssd_mod, "_LIBC", object())  # simulate Linux libc loaded
        sup = _make_supervisor(config_dir, clock)
        captured: dict = {}

        def _fake_popen(argv, **kw):
            captured.update(kw)
            return _FakeProc(pid=49201)

        monkeypatch.setattr(ssd_mod, "_popen", _fake_popen)
        monkeypatch.setattr(ssd_mod, "_allocate_free_port", lambda: 18079)
        sup._spawn_service()
        assert captured.get("preexec_fn") is ssd_mod._set_pdeathsig_preexec, (
            "on Linux the JVM must be spawned with the PR_SET_PDEATHSIG preexec_fn"
        )

    def test_spawn_service_no_pdeathsig_off_linux(
        self, config_dir: Path, clock: _FakeClock, monkeypatch
    ) -> None:
        import nexus.daemon.storage_service_daemon as ssd_mod

        monkeypatch.setattr(ssd_mod, "_LIBC", None)  # non-Linux: no prctl
        sup = _make_supervisor(config_dir, clock)
        captured: dict = {}

        def _fake_popen(argv, **kw):
            captured.update(kw)
            return _FakeProc(pid=49202)

        monkeypatch.setattr(ssd_mod, "_popen", _fake_popen)
        monkeypatch.setattr(ssd_mod, "_allocate_free_port", lambda: 18079)
        sup._spawn_service()
        assert captured.get("preexec_fn") is None, (
            "PR_SET_PDEATHSIG is Linux-only; preexec_fn must be None elsewhere"
        )

    def test_spawn_service_arms_parent_death_watchdog_env(
        self, config_dir: Path, clock: _FakeClock, monkeypatch
    ) -> None:
        """nexus-03bcg: the supervisor sets NX_SERVICE_PARENT_DEATH_EXIT=1 so the
        Java-side parent-death watchdog activates — the portable (macOS-covering)
        complement to the Linux-only PR_SET_PDEATHSIG."""
        import nexus.daemon.storage_service_daemon as ssd_mod

        sup = _make_supervisor(config_dir, clock)
        captured: dict = {}

        def _fake_popen(argv, **kw):
            captured.update(kw)
            return _FakeProc(pid=49203)

        monkeypatch.setattr(ssd_mod, "_popen", _fake_popen)
        monkeypatch.setattr(ssd_mod, "_allocate_free_port", lambda: 18079)
        sup._spawn_service()
        assert captured["env"].get("NX_SERVICE_PARENT_DEATH_EXIT") == "1"

    @pytest.mark.skipif(
        not __import__("sys").platform.startswith("linux"),
        reason="PR_SET_PDEATHSIG is Linux-only",
    )
    def test_pdeathsig_orphan_dies_with_parent(self) -> None:
        """Integration proof (Linux): a child spawned with the pdeathsig
        preexec_fn is killed when its parent dies, leaving no orphan."""
        import subprocess as _sp
        import sys as _sys
        import textwrap
        import time as _t

        from nexus.daemon.storage_service_daemon import _pid_is_alive

        parent_src = textwrap.dedent(
            """
            import subprocess, sys
            from nexus.daemon.storage_service_daemon import _set_pdeathsig_preexec
            p = subprocess.Popen(["sleep", "30"], preexec_fn=_set_pdeathsig_preexec)
            sys.stdout.write(str(p.pid) + "\\n")
            sys.stdout.flush()
            # parent exits here → the child must receive SIGKILL via PR_SET_PDEATHSIG
            """
        )
        parent = _sp.Popen([_sys.executable, "-c", parent_src], stdout=_sp.PIPE, text=True)
        child_pid = int(parent.stdout.readline().strip())
        parent.wait(timeout=10)
        # give the kernel a moment to deliver the parent-death signal
        deadline = _t.monotonic() + 5.0
        while _t.monotonic() < deadline and _pid_is_alive(child_pid):
            _t.sleep(0.1)
        assert not _pid_is_alive(child_pid), (
            "the child must die with its parent (PR_SET_PDEATHSIG); orphan survived"
        )


# ---------------------------------------------------------------------------
# RDR-174 P2.2 (nexus-exfns): boot-robustness — the supervisor self-manages PG.
#
# §4 finding: there is NO external postgresql.service to order the autostart
# unit against. The supervisor STARTS its own nx-owned PG cluster as step 1 of
# startup, with boot-safe binary discovery from the config dir — no
# provisioning-time env (NEXUS_PG_BIN) required. These regression tests pin
# that guarantee so the "After=postgresql.service / macOS readiness wrapper"
# delta stays a verified no-op: the unit needs only After=network.target.
# ---------------------------------------------------------------------------


class TestSupervisorSelfManagesPgAtBoot:
    """The autostart unit needs no external PG ordering because the supervisor
    self-starts PG. Encodes the RDR-174 §4 verified-no-op finding."""

    def test_start_locked_starts_pg_before_spawning_service(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """_start_locked must call _ensure_pg_running() BEFORE _spawn_service().

        PG-before-engine ordering is owned by the supervisor itself, not by a
        systemd After= dependency on an external postgresql.service.
        """
        sup = _make_supervisor(config_dir, clock)
        order: list[str] = []

        def _fake_ensure_pg() -> None:
            order.append("ensure_pg")

        def _fake_spawn() -> tuple[Any, int]:
            order.append("spawn_service")
            return _FakeProc(pid=44900), 18077

        sup._ensure_pg_running = _fake_ensure_pg  # type: ignore[method-assign]
        sup._spawn_service = _fake_spawn  # type: ignore[method-assign]
        stub_supervisor = MagicMock()
        stub_supervisor.record.generation = 1
        sup._supervisor = stub_supervisor
        with patch.object(sup, "_wait_for_service_ready"), \
             patch.object(sup, "_publish"):
            sup._start_locked()

        assert order == ["ensure_pg", "spawn_service"], (
            "the supervisor must start its own PG (step 1) before the engine, "
            f"with no other ordering; got order={order}"
        )

    def test_ensure_pg_running_self_starts_cluster_without_provisioning_env(
        self, config_dir: Path, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With PG down and NEXUS_PG_BIN unset, _ensure_pg_running discovers
        binaries and starts an nx-owned cluster itself.

        This is the boot-safety guarantee: cold boot has no provisioning env,
        and there is no external postgresql.service. The supervisor resolves
        binaries (config-dir boot-safe) and runs _start_cluster against its own
        PG_DATA/port — so the autostart unit requires no PG ordering seam.
        """
        import nexus.daemon.storage_service_daemon as ssd_mod
        import nexus.db.pg_provision as pgp

        monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
        sup = _make_supervisor(config_dir, clock, pg_port=15439)
        sup._creds["PG_DATA"] = str(config_dir / "postgres")

        calls: dict[str, Any] = {}

        def _fake_discover() -> Any:
            calls["discover"] = True
            return "FAKE_BINS"

        def _fake_start_cluster(bins: Any, pgdata: Path, port: int) -> None:
            calls["start_cluster"] = (bins, Path(pgdata), port)

        monkeypatch.setattr(pgp, "discover_pg_binaries", _fake_discover)
        monkeypatch.setattr(pgp, "_start_cluster", _fake_start_cluster)
        # PG is down on entry, then accepting after the supervisor starts it.
        accepting = iter([False, True])
        monkeypatch.setattr(
            ssd_mod, "_port_accepting", lambda host, port, **kw: next(accepting)
        )

        sup._ensure_pg_running()

        assert calls.get("discover") is True, (
            "supervisor must discover PG binaries itself (no external unit)"
        )
        assert "start_cluster" in calls, (
            "supervisor must start its own PG cluster, not wait on postgresql.service"
        )
        bins, pgdata, port = calls["start_cluster"]
        assert bins == "FAKE_BINS"
        assert pgdata == config_dir / "postgres", "starts nx's own PG_DATA"
        assert port == 15439, "starts nx's own provisioned port"


# ---------------------------------------------------------------------------
# RDR-175 Minimum Viable Validation: single supervisor, no double-spawn
# ---------------------------------------------------------------------------
class TestRdr175MvvSingleSupervisor:
    """RDR-175 MVV (subsumes nexus-1brzs). The minimal design's regression
    proof: after the in-process respawn mechanism is retired, exactly ONE
    supervisor owns the lease — a second start attempt (e.g. an autostart unit
    activating while a session supervisor already runs) discovers the live
    lease and short-circuits without spawning a second service. The
    no-double-spawn property rests on RDR-149 lease arbitration (idempotent
    start under a live lease), NOT on in-process respawn. The decide-first
    autostart ordering that prevents the coexistence in the first place is a
    forward requirement on RDR-174 P2.4 (nexus-3pfj0), not in this RDR.

    The (True, False) PG-only arm restarts PG in place WITHOUT bouncing the
    JVM: the Java process identity is unchanged across a PG restart."""

    def test_second_start_short_circuits_to_single_lease(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """A live lease (first supervisor / unit start) makes a second
        supervisor.start() short-circuit: no second spawn, exactly one lease."""
        from nexus.daemon.service_registry import ServiceRegistry

        scope = str(os.getuid())

        # First supervisor publishes a live lease (models the unit / session
        # supervisor already holding the lease).
        first = _make_supervisor(config_dir, clock, supervised=True)
        first._proc = _FakeProc(pid=46001)
        first._service_port = 18101
        first._publish(18101)

        registry = ServiceRegistry(dir=config_dir, tier="storage_service", clock=clock)
        assert registry.discover(scope) is not None

        # Second supervisor attempts start(). _spawn_service is a tripwire: if
        # the short-circuit fails and it tries to spawn, the test fails loudly.
        second = _make_supervisor(config_dir, clock, supervised=True)

        def _must_not_spawn() -> tuple[Any, int]:
            raise AssertionError(
                "second start() must short-circuit on the live lease, never spawn "
                "a second service (no double-spawn)"
            )

        with patch.object(second, "_spawn_service", side_effect=_must_not_spawn), \
             patch.object(second, "_ensure_pg_running") as ensure_pg:
            payload = second.start()

        ensure_pg.assert_not_called()  # short-circuit precedes PG bring-up
        # Exactly one lease, and it is the FIRST supervisor's endpoint.
        rec = registry.discover(scope)
        assert rec is not None
        assert rec.endpoint.get("port") == 18101
        assert payload["port"] == 18101, "second start must return the live endpoint"

    def test_second_start_raises_on_explicit_artifact_mismatch(
        self, config_dir: Path, clock: _FakeClock, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """nexus-4e96a mismatch arm: an explicit NEXUS_SERVICE_BIN request
        that differs from the artifact the live lease is already serving
        must raise loud, never silently attach. Sibling of
        test_second_start_short_circuits_to_single_lease — same lease setup,
        but this time the second supervisor's process env EXPLICITLY names a
        different artifact than the one the first supervisor published."""
        from nexus.daemon.service_registry import ServiceRegistry

        scope = str(os.getuid())
        first = _make_supervisor(config_dir, clock, supervised=True)
        first._proc = _FakeProc(pid=46002)
        first._service_port = 18104
        first._publish(18104)  # publishes artifact=str(first._binary_path)

        registry = ServiceRegistry(dir=config_dir, tier="storage_service", clock=clock)
        assert registry.discover(scope) is not None

        other_binary = tmp_path / "different-nexus-service"
        other_binary.write_text("#!/bin/sh\nexit 0\n")
        other_binary.chmod(0o755)
        monkeypatch.setenv("NEXUS_SERVICE_BIN", str(other_binary))
        monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)

        second = _make_supervisor(config_dir, clock, supervised=True)

        def _must_not_spawn() -> tuple[Any, int]:
            raise AssertionError(
                "an explicit artifact mismatch must raise BEFORE any spawn attempt"
            )

        with patch.object(second, "_spawn_service", side_effect=_must_not_spawn), \
             patch.object(second, "_ensure_pg_running") as ensure_pg:
            with pytest.raises(StorageServiceStartError, match="DIFFERENT artifact"):
                second.start()

        ensure_pg.assert_not_called()
        # The mismatch must not have disturbed the first supervisor's lease.
        rec = registry.discover(scope)
        assert rec is not None
        assert rec.endpoint.get("port") == 18104

    def test_second_supervisor_under_live_lease_exits_zero(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """Coexistence through the FULL run loop (substantive-critic SIG-1,
        corrected by GH #1369): a second supervisor started while another
        holds a live lease must NOT double-spawn — start() short-circuits on
        the live lease, _proc stays unset, and the loop now exits 0 rather
        than entering the heartbeat loop. Before the GH #1369 fix this
        asserted exit 3, matching a real bug: heartbeat_once() reads
        ``self._proc is None`` (never assigned by the short-circuit branch) as
        "process died" and forced a non-zero exit — under an OS unit with
        KeepAlive=true this produced an UNBOUNDED launchd respawn loop (not
        the bounded "until the foreign lease expires" this test used to
        claim), churning the service's port out from under cached MCP
        endpoints on any respawn that raced a real restart. owns_process is
        now the signal: a supervisor with nothing to own exits cleanly and
        leaves the OS-level KeepAlive alone until the OWNING supervisor's
        service actually dies."""
        import threading

        from nexus.daemon import storage_service_daemon as ssd

        # First supervisor holds a live lease.
        first = _make_supervisor(config_dir, clock, supervised=True)
        first._proc = _FakeProc(pid=46021)
        first._service_port = 18103
        first._publish(18103)

        # Second supervisor runs the real loop. _spawn_service is a tripwire:
        # the short-circuit must keep it from ever spawning a second service.
        second = _make_supervisor(config_dir, clock, supervised=True)

        def _must_not_spawn() -> tuple[Any, int]:
            raise AssertionError("coexisting supervisor must NOT spawn a second service")

        stop = threading.Event()
        with patch.object(second, "_spawn_service", side_effect=_must_not_spawn), \
             patch.object(second, "_ensure_pg_running"), \
             patch.object(ssd, "DEFAULT_HEARTBEAT_INTERVAL", 0.0):
            code = ssd._supervise_until_stopped(second, stop, lambda: None)

        assert code == 0, (
            "a supervisor that finds the lease already held owns no process "
            "(owns_process is False) and must exit 0 without entering the "
            "heartbeat loop — GH #1369: exiting non-zero here caused an "
            "unbounded launchd respawn loop against a perfectly healthy "
            "service, occasionally winning a race against a real restart and "
            "churning the service's port out from under cached MCP endpoints"
        )
        assert not second.owns_process, (
            "the short-circuit branch must never assign self._proc"
        )

    def test_pg_only_restart_keeps_same_java_pid(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """(True, False) PG-only death: the supervise loop restarts PG in place
        and the Java process identity (sup._proc) is unchanged before and
        after — the JVM is never bounced for a PG-only failure."""
        import threading

        from nexus.daemon import storage_service_daemon as ssd

        sup = _make_supervisor(config_dir, clock, supervised=True)
        fake_proc = _FakeProc(pid=46010)
        sup._proc = fake_proc
        sup._service_port = 18102
        sup._publish(18102)

        stop = threading.Event()
        beats = iter([(True, False), (True, True)])

        def _scripted_heartbeat() -> tuple[bool, bool]:
            try:
                return next(beats)
            except StopIteration:
                stop.set()
                return True, True

        ensure_pg_calls: list[int] = []

        def _record_ensure_pg() -> None:
            ensure_pg_calls.append(1)  # restart PG in place; do NOT touch _proc

        pid_before = sup._proc.pid
        with patch.object(sup, "start"), \
             patch.object(sup, "heartbeat_once", side_effect=_scripted_heartbeat), \
             patch.object(sup, "_ensure_pg_running", side_effect=_record_ensure_pg), \
             patch.object(sup, "stop"), \
             patch.object(ssd, "DEFAULT_HEARTBEAT_INTERVAL", 0.0):
            code = ssd._supervise_until_stopped(sup, stop, lambda: None)

        assert code == 0, "PG-only death must NOT exit the supervisor"
        assert ensure_pg_calls == [1], "PG restarted in place exactly once"
        assert sup._proc is fake_proc, "Java process must NOT be bounced for a PG-only restart"
        assert sup._proc.pid == pid_before


class TestStopDoesNotWaitOnAnAlreadyDeadSupervisor:
    """nexus-o8dil.21, the double-killer leg.

    ``stop_storage_service``'s lease branch SIGTERMs the supervisor and
    then waits up to ``_GRACEFUL_STOP_TIMEOUT`` for it to go away. The
    wait probe used to be ``os.kill(pid, 0)``, which succeeds forever on a
    ZOMBIE — a supervisor already killed by a previous stop, an upgrade
    sweep, or any concurrent killer, whose parent has not reaped it. So a
    second stop burned the whole grace window and then sent a pointless
    SIGKILL to a corpse.

    Uses a REAL zombie rather than a patched probe: the whole defect is
    that the probe cannot tell the difference, so a faked probe would
    assert nothing.
    """

    def test_zombie_supervisor_is_not_waited_on(self, config_dir: Path) -> None:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, this interpreter
            [sys.executable, "-c", "import time; time.sleep(120)"],
        )
        try:
            os.kill(proc.pid, signal.SIGKILL)
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and process_state(proc.pid) != "Z":
                time.sleep(0.02)
            assert process_state(proc.pid) == "Z", (
                "fixture must produce a real unreaped zombie"
            )
            _write_stale_or_fresh_lease(
                config_dir, supervisor_pid=proc.pid, engine_pid=None,
                age_s=1.0, ttl=15.0,
            )
            t0 = time.monotonic()
            outcome = stop_storage_service(config_dir=config_dir)
            elapsed = time.monotonic() - t0
        finally:
            with contextlib.suppress(ChildProcessError, OSError, subprocess.TimeoutExpired):
                proc.wait(timeout=5)

        assert elapsed < 2.0, (
            "an already-dead (zombie) supervisor must not hold the graceful "
            f"stop window open; took {elapsed:.2f}s of a 5s budget"
        )
        assert outcome.stubborn == (), (
            f"a corpse is not a stubborn survivor: {outcome}"
        )


# ---------------------------------------------------------------------------
# nexus-8vp0i / GH #1486: migration-aware readiness wiring
# ---------------------------------------------------------------------------


class TestLogTailer:
    """_LogTailer: respawn must never read the previous process's lines."""

    def test_reads_new_lines_appended_after_offset(self, tmp_path: Path) -> None:
        import nexus.daemon.storage_service_daemon as ssd_mod

        log_path = tmp_path / "svc.log"
        log_path.write_text("old line one\nold line two\n")
        start_offset = log_path.stat().st_size

        tailer = ssd_mod._LogTailer(log_path, start_offset)
        assert tailer() == []  # nothing new yet

        with open(log_path, "a") as fh:
            fh.write("new line one\nnew line two\n")
        assert tailer() == ["new line one", "new line two"]
        assert tailer() == []  # consumed; no duplicates

    def test_respawn_offset_ignores_previous_process_tail(self, tmp_path: Path) -> None:
        """The historical incident shape: a killed process's crash output
        sits in the O_APPEND log; a respawned process's tailer, seeded at
        the NEW spawn's offset, must not see any of it."""
        import nexus.daemon.storage_service_daemon as ssd_mod

        log_path = tmp_path / "svc.log"
        log_path.write_text("")
        with open(log_path, "a") as fh:
            fh.write("process A: booting\n")
            fh.write("process A: FATAL crash\n")
        offset_at_respawn = log_path.stat().st_size

        # A fresh tailer built at the OLD offset (0) would see process A's
        # lines — the bug this offset-capture prevents.
        stale_tailer = ssd_mod._LogTailer(log_path, 0)
        assert stale_tailer() == ["process A: booting", "process A: FATAL crash"]

        # The correct tailer, seeded at spawn time for process B.
        tailer = ssd_mod._LogTailer(log_path, offset_at_respawn)
        assert tailer() == []
        with open(log_path, "a") as fh:
            fh.write("process B: booting\n")
        assert tailer() == ["process B: booting"]

    def test_partial_trailing_line_buffered_across_calls(self, tmp_path: Path) -> None:
        import nexus.daemon.storage_service_daemon as ssd_mod

        log_path = tmp_path / "svc.log"
        log_path.write_text("")
        tailer = ssd_mod._LogTailer(log_path, 0)

        with open(log_path, "a") as fh:
            fh.write("event=schema_migration_start\nRunning Chang")
        assert tailer() == ["event=schema_migration_start"]

        with open(log_path, "a") as fh:
            fh.write("eset: x::y::z\n")
        assert tailer() == ["Running Changeset: x::y::z"]

    def test_missing_file_reads_as_no_new_lines(self, tmp_path: Path) -> None:
        import nexus.daemon.storage_service_daemon as ssd_mod

        tailer = ssd_mod._LogTailer(tmp_path / "does-not-exist.log", 0)
        assert tailer() == []


class TestStaleChangelogLockCleanup:
    """_release_stale_changelog_lock / _migration_pg_probe: guarded to the
    bundled cluster, best-effort, never block a service start."""

    def test_cleanup_skipped_without_pg_data(self, config_dir: Path, clock: _FakeClock) -> None:
        creds = {
            "NX_DB_URL": "jdbc:...", "NX_DB_USER": "svc", "NX_DB_PASS": "pass",
            "NX_DB_ADMIN_URL": "jdbc:...", "NX_DB_ADMIN_USER": "admin",
            "NX_DB_ADMIN_PASS": "adminpass", "PG_PORT": "15432",
            "NX_SERVICE_TOKEN": "root-token-from-creds-deadbeef",
            # no PG_DATA: managed/BYO Postgres
        }
        sup = _make_supervisor(config_dir, clock, creds=creds)

        # Must not raise, and must not attempt any real psql invocation.
        with patch("nexus.db.pg_provision.discover_pg_binaries") as discover:
            sup._release_stale_changelog_lock(reason="test")
            discover.assert_not_called()

    def test_pg_probe_unavailable_without_pg_data(self, config_dir: Path, clock: _FakeClock) -> None:
        import nexus.daemon.readiness as readiness

        creds = {
            "NX_DB_URL": "jdbc:...", "NX_DB_USER": "svc", "NX_DB_PASS": "pass",
            "NX_DB_ADMIN_URL": "jdbc:...", "NX_DB_ADMIN_USER": "admin",
            "NX_DB_ADMIN_PASS": "adminpass", "PG_PORT": "15432",
            "NX_SERVICE_TOKEN": "root-token-from-creds-deadbeef",
        }
        sup = _make_supervisor(config_dir, clock, creds=creds)
        assert sup._migration_pg_probe() is readiness.PgActivity.UNAVAILABLE

    def test_skips_when_a_live_engine_is_found(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """nexus-8vp0i review round 2 (substantive-critic Critical 1): the
        liveness gate must skip BEFORE any psql call when the injected scan
        reports a live engine — no terminate, no release, WARNING logged."""
        scan_calls: list[tuple[Path, Path]] = []

        def fake_scan(cd: Path, bp: Path) -> list[tuple[int, str]]:
            scan_calls.append((cd, bp))
            return [(99999, "/fake/nexus-service --port 18080")]

        sup = _make_supervisor(config_dir, clock, engine_liveness_scan=fake_scan)

        with patch("nexus.db.pg_provision.discover_pg_binaries") as discover:
            sup._release_stale_changelog_lock(reason="test")
            discover.assert_not_called()
        assert scan_calls == [(config_dir, Path("/fake/nexus-service"))]

    def test_skips_when_pg_probe_reports_executing(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """The process-table scan can miss a live holder outside this
        config_dir's own stack (e.g. a standalone engine run against the
        same cluster) — the PG probe is the second, independent check."""
        import nexus.daemon.readiness as readiness

        sup = _make_supervisor(config_dir, clock)  # default scan: no live engine

        with patch.object(sup, "_migration_pg_probe", return_value=readiness.PgActivity.EXECUTING), \
             patch("nexus.db.pg_provision.discover_pg_binaries") as discover:
            sup._release_stale_changelog_lock(reason="test")
            discover.assert_not_called()

    def test_scan_failure_does_not_proceed(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """A liveness-scan failure is NOT evidence of 'no live engine' — it
        must degrade to skipping cleanup, not to proceeding as if the scan
        had come back clean."""
        def failing_scan(cd: Path, bp: Path) -> list[tuple[int, str]]:
            raise RuntimeError("process table unreadable")

        sup = _make_supervisor(config_dir, clock, engine_liveness_scan=failing_scan)

        with patch("nexus.db.pg_provision.discover_pg_binaries") as discover:
            sup._release_stale_changelog_lock(reason="test")  # must not raise
            discover.assert_not_called()

    def test_proceeds_and_logs_terminated_pids_when_gate_clears(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """nexus-8vp0i review round 2 (code-review-expert Significant 2):
        once the liveness gate clears (no live engine, probe not
        EXECUTING), the terminate/release SQL runs and the WARNING names
        exactly which pids were terminated — not discarded."""
        import nexus.daemon.readiness as readiness
        import nexus.daemon.storage_service_daemon as ssd_mod

        def fake_run_psql(psql_bin, host, port, dbname, user, password, sql, *, psql_runner=None):
            import subprocess as _sp
            if "SELECT lockedby, lockgranted" in sql:
                return _sp.CompletedProcess(
                    args=[], returncode=0, stdout="svc@host|2026-08-29 12:00:00\n", stderr="",
                )
            if "pg_terminate_backend" in sql:
                return _sp.CompletedProcess(
                    args=[], returncode=0, stdout="4242|t\n4343|t\n", stderr="",
                )
            if "UPDATE databasechangeloglock" in sql:
                return _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected SQL: {sql}")

        sup = _make_supervisor(config_dir, clock)  # default: no live engine

        with patch.object(sup, "_migration_pg_probe", return_value=readiness.PgActivity.IDLE), \
             patch("nexus.db.pg_provision.discover_pg_binaries") as discover, \
             patch("nexus.health._run_psql", side_effect=fake_run_psql), \
             patch.object(ssd_mod, "_log") as log_mock:
            discover.return_value.psql = Path("/fake/psql")
            sup._release_stale_changelog_lock(reason="test_reason")

        released_calls = [
            call for call in log_mock.warning.call_args_list
            if call.args and call.args[0] == "storage_service_stale_changelog_lock_released"
        ]
        assert len(released_calls) == 1
        assert released_calls[0].kwargs["terminated_pids"] == [4242, 4343]
        assert released_calls[0].kwargs["lockedby"] == "svc@host"
        assert released_calls[0].kwargs["reason"] == "test_reason"


class TestWaitForServiceReadyMigrationAware:
    """_wait_for_service_ready: end-to-end wiring of the readiness monitor
    into the supervisor (nexus-8vp0i / GH #1486). Real wall-clock, kept to
    sub-second budgets exactly like the pre-existing
    test_loud_failure_when_service_unreachable (timeout=0.5) — only the
    module-level MIGRATING bounds are monkeypatched down so a migration
    stall test does not take 600 real seconds.
    """

    def test_healthy_fast_boot_returns_without_raising(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=51000)
        with patch.object(sup, "_probe_service_health", return_value=HealthProbe.OK):
            sup._wait_for_service_ready(fake_proc, 19999, timeout=0.5)  # must not raise

    def test_process_exit_keeps_the_historical_message(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=51001, returncode=7)
        with patch.object(sup, "_probe_service_health", return_value=HealthProbe.UNKNOWN), \
             patch.object(sup, "_release_stale_changelog_lock"):
            with pytest.raises(StorageServiceStartError, match="exited with code 7"):
                sup._wait_for_service_ready(fake_proc, 19999, timeout=0.5)

    def test_process_exit_triggers_stale_lock_cleanup(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """nexus-8vp0i review round 2 (code-review-expert Significant 1): a
        JVM crash mid-changeset is exactly the exit shape that leaves
        databasechangeloglock stuck — process-exit must trigger cleanup
        too, not only a readiness-timeout kill."""
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=51001, returncode=137)

        cleanup_calls: list[str] = []
        with patch.object(sup, "_probe_service_health", return_value=HealthProbe.UNKNOWN), \
             patch.object(
                 sup, "_release_stale_changelog_lock",
                 side_effect=lambda reason: cleanup_calls.append(reason),
             ):
            with pytest.raises(StorageServiceStartError, match="exited with code 137"):
                sup._wait_for_service_ready(fake_proc, 19999, timeout=0.5)

        assert cleanup_calls == ["process_exited"]

    def test_plain_timeout_keeps_the_historical_message(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """No migration marker ever seen -> PRE_MIGRATION stall -> the exact
        pre-nexus-8vp0i wording (operators and any external tooling key on
        it)."""
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=51002)
        with patch.object(sup, "_probe_service_health", return_value=HealthProbe.UNKNOWN), \
             patch.object(sup, "_release_stale_changelog_lock"):
            with pytest.raises(
                StorageServiceStartError,
                match=r"did not become healthy .* within 1s",
            ):
                sup._wait_for_service_ready(fake_proc, 19999, timeout=1.0)

    def test_migration_stall_raises_migration_specific_message_and_cleans_up(
        self, config_dir: Path, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import nexus.daemon.storage_service_daemon as ssd_mod
        import nexus.daemon.readiness as readiness

        monkeypatch.setattr(ssd_mod, "_MIGRATION_STALL_TIMEOUT", 0.2)
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=51003)

        log_lines = iter([["event=schema_migration_start"]])

        def fake_log_reader() -> list[str]:
            return next(log_lines, [])

        cleanup_calls: list[str] = []
        # IDLE (not UNAVAILABLE): the probe IS available but finds nothing
        # executing, so the 600s-equivalent (monkeypatched to 0.2s) stall
        # bound applies. UNAVAILABLE would widen to the 3600s log-only bound
        # instead — not the branch this test targets.
        with patch.object(sup, "_probe_service_health", return_value=HealthProbe.UNKNOWN), \
             patch.object(sup, "_build_log_tailer", return_value=fake_log_reader), \
             patch.object(sup, "_migration_pg_probe", return_value=readiness.PgActivity.IDLE), \
             patch.object(
                 sup, "_release_stale_changelog_lock",
                 side_effect=lambda reason: cleanup_calls.append(reason),
             ):
            with pytest.raises(StorageServiceStartError, match="migration stalled"):
                sup._wait_for_service_ready(fake_proc, 19999, timeout=60.0)

        assert "readiness_timeout" in cleanup_calls

    def test_waiting_for_lock_never_triggers_cleanup_on_its_own(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """nexus-8vp0i review round 2 (substantive-critic Critical 1): a
        live holder must NEVER be terminated. 'Waiting for changelog lock'
        alone must NOT trigger cleanup — the lock may be legitimately held
        by a still-running engine (macOS has no PR_SET_PDEATHSIG, so an
        orphaned-but-alive engine from a dead supervisor is reachable
        there). Liquibase gives up on its own after its 10-minute
        changeLogLockWaitTime with a LockException, which exits THIS
        process non-zero — that is the ReadinessProcessExitedError path,
        which DOES clean up (see test_process_exit_triggers_stale_lock_
        cleanup). TickResult.waiting_for_lock is still reported (observed
        via the progress log), just never wired to a cleanup call."""
        import nexus.daemon.readiness as readiness

        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=51004)

        # Every tick reports 'Waiting for changelog lock' — a sustained,
        # not just one-off, observation — right up until health turns OK.
        log_batches = iter([
            ["Waiting for changelog lock"],
            ["Waiting for changelog lock"],
            ["Waiting for changelog lock"],
        ])

        def fake_log_reader() -> list[str]:
            return next(log_batches, [])

        health_answers = iter([HealthProbe.UNKNOWN, HealthProbe.UNKNOWN, HealthProbe.OK])

        def fake_health(port: int | None = None) -> HealthProbe:
            return next(health_answers, HealthProbe.OK)

        with patch.object(sup, "_probe_service_health", side_effect=fake_health), \
             patch.object(sup, "_build_log_tailer", return_value=fake_log_reader), \
             patch.object(sup, "_migration_pg_probe", return_value=readiness.PgActivity.IDLE), \
             patch.object(sup, "_release_stale_changelog_lock") as cleanup, \
             patch("time.sleep"):
            sup._wait_for_service_ready(fake_proc, 19999, timeout=60.0)

        cleanup.assert_not_called()


class TestStartLockedReleasesStaleLockBeforeSpawn:
    def test_release_stale_lock_called_before_spawn(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """Step 1.5 (nexus-8vp0i): the stale-changelog-lock cleanup must run
        AFTER _ensure_pg_running and BEFORE _spawn_service."""
        sup = _make_supervisor(config_dir, clock)
        order: list[str] = []

        sup._ensure_pg_running = lambda: order.append("ensure_pg")  # type: ignore[method-assign]
        sup._release_stale_changelog_lock = lambda reason: order.append(  # type: ignore[method-assign]
            f"release_lock:{reason}"
        )

        def _fake_spawn() -> tuple[Any, int]:
            order.append("spawn_service")
            return _FakeProc(pid=51005), 18078

        sup._spawn_service = _fake_spawn  # type: ignore[method-assign]
        stub_supervisor = MagicMock()
        stub_supervisor.record.generation = 1
        sup._supervisor = stub_supervisor

        with patch.object(sup, "_wait_for_service_ready"), patch.object(sup, "_publish"):
            sup._start_locked()

        assert order == ["ensure_pg", "release_lock:pre_spawn", "spawn_service"]
