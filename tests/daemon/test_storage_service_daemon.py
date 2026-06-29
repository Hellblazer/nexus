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

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus.daemon.service_registry import (
    ServiceRegistry,
    ServiceSupervisor,
    StaleOwnerError,
)
from nexus.daemon.storage_service_daemon import (
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
) -> StorageServiceSupervisor:
    """Build a supervisor with injected clock and no real pg/service spawn."""
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
    return StorageServiceSupervisor(
        config_dir=config_dir,
        binary_path=binary_path,
        pg_port=pg_port,
        service_port=service_port,
        creds=creds,
        lease_clock=clock,
        supervised=supervised,
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
        with patch.object(sup, "_service_healthy", return_value=True), \
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
        with patch.object(sup, "_service_healthy", return_value=False), \
             patch.object(sup, "_pg_reachable", return_value=True), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            for _ in range(_MAX_UNHEALTHY_HEARTBEATS - 1):
                sup.heartbeat_once()

        assert sup._consecutive_unhealthy_heartbeats == _MAX_UNHEALTHY_HEARTBEATS - 1

        # One healthy beat — counter resets
        with patch.object(sup, "_service_healthy", return_value=True), \
             patch.object(sup, "_pg_reachable", return_value=True), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            jar_running, pg_ok = sup.heartbeat_once()

        assert jar_running is True and pg_ok is True
        assert sup._consecutive_unhealthy_heartbeats == 0, (
            "One healthy beat must reset _consecutive_unhealthy_heartbeats to 0"
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


class TestRunStorageSupervisorFunction:
    """Tests for the module-level start/stop helper functions."""

    def test_stop_noop_when_no_lease(self, config_dir: Path) -> None:
        """stop_storage_service returns None if no lease is present."""
        result = stop_storage_service(config_dir=config_dir)
        assert result is None

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
            "nexus.daemon.storage_service_daemon.subprocess.Popen", _fake_popen,
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
    ) -> None:
        self._beats = list(beats)
        self._stop = stop_requested
        self._ensure_pg_raises = ensure_pg_raises
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def heartbeat_once(self) -> tuple[bool, bool]:
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
        with patch.object(daemon_mod.subprocess, "Popen") as popen:
            rec = daemon_mod.ensure_storage_supervisor(config_dir)
        assert rec is not None
        popen.assert_not_called()  # idempotent: a live lease is never re-spawned

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
             patch.object(daemon_mod.subprocess, "Popen", side_effect=_popen_publishes) as popen:
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
             patch.object(daemon_mod.subprocess, "Popen", return_value=MagicMock()):
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
        # _pid_is_alive False so the guard treats that supervisor as dead.
        self._publish_fresh_lease(config_dir, port=18093)
        scope = str(os.getuid())
        assert ServiceRegistry(dir=config_dir, tier="storage_service").discover(scope) is not None

        def _popen_publishes(*_a, **_k):
            self._publish_fresh_lease(config_dir, port=18094)
            return MagicMock()

        with patch.object(ssd_mod, "_pid_is_alive", return_value=False), \
             patch.object(daemon_mod, "_resolve_nx_bin", return_value=["nx"]), \
             patch.object(daemon_mod.subprocess, "Popen", side_effect=_popen_publishes) as popen:
            rec = daemon_mod.ensure_storage_supervisor(config_dir)

        popen.assert_called_once()  # dead lease must trigger a re-spawn
        assert rec is not None and rec.endpoint.get("port") == 18094

    def test_absent_supervisor_pid_trusts_ttl_freshness(
        self, config_dir: Path
    ) -> None:
        """A lease WITHOUT a supervisor_pid (legacy/non-supervised) must fall
        through to the existing TTL-freshness short-circuit — no spurious
        re-spawn — even when _pid_is_alive would report dead."""
        import time as _time

        import nexus.daemon.storage_service_daemon as ssd_mod
        from nexus.commands import daemon as daemon_mod

        # Publish a NON-supervised lease: payload {} → supervisor_pid absent.
        sup = _make_supervisor(config_dir, lambda: _time.time(), supervised=False)
        sup._proc = _FakeProc(pid=42778)
        sup._service_port = 18095
        sup._publish(18095)

        with patch.object(ssd_mod, "_pid_is_alive", return_value=False), \
             patch.object(daemon_mod.subprocess, "Popen") as popen:
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
        # The margin invariant (debugger RF-1 finding): a heartbeat tick can take
        # up to _HEALTH_TIMEOUT + DEFAULT_HEARTBEAT_INTERVAL; the TTL must exceed
        # that with room, or a single slow tick grazes the TTL.
        import nexus.daemon.storage_service_daemon as ssd_mod
        from nexus.daemon.service_registry import DEFAULT_HEARTBEAT_INTERVAL, ttl_for_tier

        worst_tick = ssd_mod._HEALTH_TIMEOUT + DEFAULT_HEARTBEAT_INTERVAL
        assert ttl_for_tier("storage_service") >= 3 * worst_tick

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

        monkeypatch.setattr(ssd_mod.subprocess, "Popen", _fake_popen)
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
        monkeypatch.setattr(ssd_mod.subprocess, "Popen",
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

        monkeypatch.setattr(ssd_mod.subprocess, "Popen", _fake_popen)
        monkeypatch.setattr(ssd_mod, "_allocate_free_port", lambda: 18079)
        sup._spawn_service()
        # Production default: no -Xmx — the binary keeps native-image's default heap.
        assert not any(a.startswith("-Xmx") for a in captured["argv"][1:])


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

    def test_second_supervisor_under_live_lease_exits_nonzero(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """Coexistence through the FULL run loop (substantive-critic SIG-1): a
        second supervisor started while another holds a live lease must NOT
        double-spawn — start() short-circuits on the live lease, _proc stays
        unset, and the loop exits non-zero (the OS, not an in-process respawn,
        owns restart decisions). Under the OS unit this re-runs every RestartSec
        until the foreign lease expires — a bounded crash-loop (RDR-175
        §Consequences) that RDR-174 P2.4's decide-first ordering (nexus-3pfj0)
        prevents by never starting a session supervisor under a unit. This PINS
        the behavior so the requirement on nexus-3pfj0 stays visible."""
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

        assert code == 3, (
            "a supervisor that finds the lease already held exits 3 (no "
            "double-spawn); the OS unit then crash-loops until the foreign "
            "lease expires — decide-first ordering on nexus-3pfj0 prevents the "
            "coexistence; see RDR-175 §Consequences"
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
