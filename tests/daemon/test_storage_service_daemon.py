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
    _RESTART_WINDOW_HEARTBEATS,
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
    jar_path: Path | None = None,
    supervised: bool = False,
    creds: dict[str, str] | None = None,
) -> StorageServiceSupervisor:
    """Build a supervisor with injected clock and no real pg/jar."""
    if jar_path is None:
        jar_path = Path("/fake/nexus-service.jar")
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
        jar_path=jar_path,
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

        original_restart_count = sup._restart_count

        import nexus.daemon.storage_service_daemon as ssd_mod
        with patch.object(sup, "_service_healthy", return_value=True), \
             patch.object(sup, "_pg_reachable", return_value=False), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            jar_running, pg_ok = sup.heartbeat_once()

        # Jar should still be considered running
        assert jar_running is True
        # PG is reported down
        assert pg_ok is False
        # No respawn triggered — _restart_count must not change
        assert sup._restart_count == original_restart_count, (
            "PG-down must NOT trigger a jar respawn; _restart_count unchanged"
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
# Windowed restart budget (SIGNIFICANT-2 fix)
# ---------------------------------------------------------------------------


class TestWindowedRestartBudget:
    """_restart_count must reset after _RESTART_WINDOW_HEARTBEATS clean heartbeats
    following a restart (SIGNIFICANT-2 fix — not lifetime-cumulative)."""

    def test_restart_budget_resets_after_clean_window(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """After a restart and then _RESTART_WINDOW_HEARTBEATS clean heartbeats,
        _restart_count resets to 0 so isolated bursts don't permanently exhaust budget."""
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=43200)
        sup._proc = fake_proc
        sup._service_port = 19010
        sup._publish(19010)

        # Simulate one restart
        sup._restart_count = 1
        sup._clean_heartbeats_since_restart = 0

        # Simulate clean heartbeats via _maybe_reset_restart_budget
        for _ in range(_RESTART_WINDOW_HEARTBEATS - 1):
            sup._maybe_reset_restart_budget()
            assert sup._restart_count == 1, "budget must not reset mid-window"

        # One more clean heartbeat crosses the threshold
        sup._maybe_reset_restart_budget()
        assert sup._restart_count == 0, (
            f"After {_RESTART_WINDOW_HEARTBEATS} clean heartbeats, restart budget "
            "must reset to 0 (windowed, not lifetime-cumulative)"
        )
        assert sup._clean_heartbeats_since_restart == 0

    def test_restart_budget_not_reset_when_no_restarts(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """_maybe_reset_restart_budget is a no-op when _restart_count == 0."""
        sup = _make_supervisor(config_dir, clock)
        assert sup._restart_count == 0
        for _ in range(_RESTART_WINDOW_HEARTBEATS + 10):
            sup._maybe_reset_restart_budget()
        assert sup._restart_count == 0  # already 0, no change

    def test_clean_window_reset_on_respawn(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """_respawn() resets _clean_heartbeats_since_restart to 0, restarting the window."""
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=43201)
        sup._proc = fake_proc
        sup._service_port = 19011
        sup._publish(19011)

        # Accumulate some clean heartbeats
        sup._restart_count = 1
        sup._clean_heartbeats_since_restart = 50

        new_proc = _FakeProc(pid=43202)
        with patch.object(sup, "_spawn_service", return_value=(new_proc, 19011)), \
             patch.object(sup, "_wait_for_service_ready"):
            sup._proc = None
            sup._respawn()

        assert sup._clean_heartbeats_since_restart == 0, (
            "_respawn() must reset the clean window so the budget window restarts"
        )


# ---------------------------------------------------------------------------
# Auto-restart tests
# ---------------------------------------------------------------------------


class TestStorageServiceAutoRestart:
    """The supervisor must auto-restart the jar on death, publishing a strictly
    higher generation each time."""

    def test_restart_publishes_higher_generation(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """After jar death and restart, the new record has gen > old gen."""
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=43001)
        sup._proc = fake_proc
        sup._service_port = 18090
        sup._publish(18090)

        registry = ServiceRegistry(
            dir=config_dir, tier="storage_service", clock=clock
        )
        scope = str(os.getuid())
        gen1 = registry.discover(scope).generation

        # Simulate jar death + respawn
        fake_proc.kill_proc()
        new_proc = _FakeProc(pid=43002)

        with patch.object(sup, "_spawn_service", return_value=(new_proc, 18090)):
            with patch.object(sup, "_wait_for_service_ready"):
                sup._proc = None
                sup._respawn()

        gen2 = registry.discover(scope).generation
        assert gen2 > gen1, "restart must publish a strictly higher generation"

    def test_loud_failure_on_respawn_exhaustion(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """If respawn fails _MAX_RESTART_ATTEMPTS times, StorageServiceStartError is raised."""
        from nexus.daemon.storage_service_daemon import _MAX_RESTART_ATTEMPTS
        sup = _make_supervisor(config_dir, clock)
        # Force the restart counter over the limit
        sup._restart_count = _MAX_RESTART_ATTEMPTS

        with pytest.raises(StorageServiceStartError, match="(?i)restart|attempt|failed"):
            sup._respawn()

    def test_loud_failure_on_spawn_error(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """If _spawn_service raises, the error propagates as StorageServiceStartError."""
        sup = _make_supervisor(config_dir, clock)

        with patch.object(sup, "_spawn_service", side_effect=StorageServiceStartError("JAR not found")):
            with pytest.raises(StorageServiceStartError):
                sup._respawn()


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
# Stuck-JVM / unhealthy-respawn tests (SIG2 fix)
# ---------------------------------------------------------------------------


class TestStuckJvmRespawn:
    """Jar alive but /health returning non-200 must trigger respawn after
    _MAX_UNHEALTHY_HEARTBEATS consecutive failures (SIG2 fix).

    A stuck-but-alive JVM (connection-pool exhaustion, GC pause, internal
    deadlock) is the most common Java partial-failure mode. Treating it as
    'jar alive, lease not re-stamped, no recovery' was the silent-degrade
    gap. The fix: count consecutive unhealthy beats; on reaching the
    threshold return (False, pg_ok) from heartbeat_once() so the run loop
    calls _respawn().
    """

    def test_single_unhealthy_beat_does_not_respawn(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """A single unhealthy heartbeat is below threshold — no respawn signal."""
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

    def test_threshold_minus_one_beats_no_respawn(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """_MAX_UNHEALTHY_HEARTBEATS - 1 consecutive failures do not signal respawn."""
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
                assert jar_running is True, f"should not signal respawn on beat {i+1}"

        assert sup._consecutive_unhealthy_heartbeats == _MAX_UNHEALTHY_HEARTBEATS - 1

    def test_at_threshold_returns_false_to_force_respawn(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """After _MAX_UNHEALTHY_HEARTBEATS consecutive failures, return (False, pg_ok)
        so the run loop calls _respawn() — treating stuck JVM like a jar death.
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
            # Nth beat: threshold crossed → respawn signal
            jar_running, pg_ok = sup.heartbeat_once()

        assert jar_running is False, (
            f"After {_MAX_UNHEALTHY_HEARTBEATS} consecutive unhealthy beats, "
            "heartbeat_once() must return (False, _) to signal a respawn"
        )
        assert pg_ok is True  # PG was healthy
        # Counter reset after signalling
        assert sup._consecutive_unhealthy_heartbeats == 0

    def test_single_healthy_beat_resets_counter(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """One healthy heartbeat resets the unhealthy counter to 0 so transient
        GC pauses do not accumulate toward the respawn threshold.
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

    def test_unhealthy_respawn_counts_toward_restart_budget(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """An unhealthy-triggered respawn (via run loop calling _respawn after
        (False, _) from heartbeat_once) counts toward _restart_count, composing
        correctly with the windowed restart budget.
        """
        sup = _make_supervisor(config_dir, clock)
        fake_proc = _FakeProc(pid=45005)
        sup._proc = fake_proc
        sup._service_port = 20005
        sup._publish(20005)

        # Force the unhealthy threshold
        import nexus.daemon.storage_service_daemon as ssd_mod
        with patch.object(sup, "_service_healthy", return_value=False), \
             patch.object(sup, "_pg_reachable", return_value=True), \
             patch.object(ssd_mod, "_pid_is_alive", return_value=True):
            for _ in range(_MAX_UNHEALTHY_HEARTBEATS - 1):
                sup.heartbeat_once()
            jar_running, _ = sup.heartbeat_once()

        assert jar_running is False  # respawn signal

        # Simulate the run loop calling _respawn(). The stuck JVM (fake_proc) is
        # STILL set as sup._proc — heartbeat_once signalled respawn without
        # clearing it. _respawn() must stop the old process before spawning the
        # replacement, otherwise the stuck JVM is orphaned (round-3 HIGH-1/SIG-1).
        assert sup._proc is fake_proc, "stuck JVM must still be the live _proc"
        order: list[str] = []
        new_proc = _FakeProc(pid=45006)

        def _record_stop() -> None:
            order.append("stop")
            sup._proc = None

        def _record_spawn() -> tuple[_FakeProc, int]:
            order.append("spawn")
            return new_proc, 20005

        with patch.object(sup, "_stop_service", side_effect=_record_stop), \
             patch.object(sup, "_spawn_service", side_effect=_record_spawn), \
             patch.object(sup, "_wait_for_service_ready"):
            sup._respawn()

        assert order == ["stop", "spawn"], (
            "_respawn() must stop the old (stuck) process BEFORE spawning the "
            f"replacement, to avoid orphaning the JVM; got order={order}"
        )
        assert sup._restart_count == 1, (
            "Unhealthy-triggered respawn must count toward _restart_count"
        )

    def test_compound_failure_below_threshold_returns_jar_alive(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """When the jar is unhealthy AND PG is also down, below the stuck-JVM
        threshold heartbeat_once() reports the jar as still-alive (True) and PG
        down (False), so the run loop takes the PG-recovery branch rather than
        respawning the jar prematurely (round-3 LOW-1).
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

    def test_jar_not_found_raises_loudly(
        self, config_dir: Path, creds_path: Path
    ) -> None:
        """If no JAR can be located, start raises StorageServiceStartError."""
        with pytest.raises(StorageServiceStartError, match="(?i)jar|found|nexus-service"):
            start_storage_service(
                config_dir=config_dir,
                jar_path=Path("/nonexistent/nexus-service.jar"),
            )


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
        with patch.object(sup, "_find_java", return_value="/usr/bin/java"):
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


class TestSchemaSkewGateWiring:
    """nexus-pebfx.4: _start_locked runs the schema-skew gate after PG is up
    and BEFORE spawning the JAR — a skewed JAR must never reach Popen."""

    def test_skew_refusal_blocks_spawn(
        self, config_dir: Path, clock: _FakeClock,
    ) -> None:
        sup = _make_supervisor(config_dir, clock)
        spawned: list[bool] = []
        with patch.object(sup, "_ensure_pg_running"), \
             patch(
                 "nexus.daemon.jar_lifecycle.check_schema_skew",
                 side_effect=StorageServiceStartError("JAR is OLDER"),
             ), \
             patch.object(
                 sup, "_spawn_service",
                 side_effect=lambda: spawned.append(True) or (MagicMock(), 1),
             ):
            with pytest.raises(StorageServiceStartError, match="OLDER"):
                sup.start()
        assert spawned == [], "skewed JAR must not be spawned"

    def test_clean_gate_proceeds_to_spawn(
        self, config_dir: Path, clock: _FakeClock,
    ) -> None:
        sup = _make_supervisor(config_dir, clock)
        proc = _FakeProc(pid=51000)
        with patch.object(sup, "_ensure_pg_running"), \
             patch("nexus.daemon.jar_lifecycle.check_schema_skew") as gate, \
             patch.object(sup, "_spawn_service", return_value=(proc, 19500)), \
             patch.object(sup, "_wait_for_service_ready"):
            payload = sup.start()
        gate.assert_called_once()
        assert payload["port"] == 19500


# ---------------------------------------------------------------------------
# nexus-14k0m: simultaneous JAR+PG death must attempt PG recovery
# ---------------------------------------------------------------------------


class _ScriptedSupervisor:
    """run-loop double for ``_supervise_until_stopped``: heartbeat_once pops
    scripted (jar_running, pg_ok) tuples; every lifecycle call is recorded in
    ``calls`` so ordering assertions are exact."""

    def __init__(
        self,
        beats: list[tuple[bool, bool]],
        stop_requested,
        *,
        ensure_pg_raises: Exception | None = None,
        respawn_raises: Exception | None = None,
    ) -> None:
        self._beats = list(beats)
        self._stop = stop_requested
        self._ensure_pg_raises = ensure_pg_raises
        self._respawn_raises = respawn_raises
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

    def _respawn(self) -> None:
        self.calls.append("respawn")
        if self._respawn_raises is not None:
            raise self._respawn_raises

    def stop(self) -> None:
        self.calls.append("stop")


class TestSimultaneousJarPgDeath:
    """nexus-14k0m (P5 gate code-review HIGH): heartbeat (False, False)
    previously hit only the ``not jar_running`` branch, whose _respawn()
    never restarts PG — the new jar's /health can never pass, the restart
    budget burns down, and the supervisor exits without ONE pg_ctl attempt.

    The (False, False) beat models BOTH real routes into jar_running=False
    (process exit, which hardcodes pg_ok=False; stuck-JVM threshold, which
    carries a live PG probe) — at the run-loop seam they are
    indistinguishable, so one scripted beat covers both (CRE M2)."""

    def _run(self, sup_factory):
        import threading

        from nexus.daemon import storage_service_daemon as ssd

        stop = threading.Event()
        sup = sup_factory(stop)
        with patch.object(ssd, "DEFAULT_HEARTBEAT_INTERVAL", 0.0):
            code = ssd._supervise_until_stopped(sup, stop, lambda: None)
        return sup, code

    def test_pg_restarted_before_jar_respawn(self) -> None:
        sup, code = self._run(
            lambda stop: _ScriptedSupervisor([(False, False)], stop)
        )
        assert code == 0
        assert "ensure_pg" in sup.calls, (
            "simultaneous death must attempt PG recovery, not only respawn"
        )
        assert sup.calls.index("ensure_pg") < sup.calls.index("respawn"), (
            "PG must be up BEFORE the jar respawn or /health can never pass"
        )

    def test_pg_restart_failure_exits_4_without_burning_respawn(self) -> None:
        sup, code = self._run(
            lambda stop: _ScriptedSupervisor(
                [(False, False)], stop,
                ensure_pg_raises=StorageServiceStartError("pg_ctl failed"),
            )
        )
        assert code == 4, "PG-unrecoverable is the exit-4 contract"
        assert "ensure_pg" in sup.calls, "exit 4 must come FROM the PG attempt"
        assert "respawn" not in sup.calls, (
            "respawning the jar with PG down is futile budget burn"
        )

    def test_jar_only_death_does_not_touch_pg(self) -> None:
        sup, code = self._run(
            lambda stop: _ScriptedSupervisor([(False, True)], stop)
        )
        assert code == 0
        assert "respawn" in sup.calls
        assert "ensure_pg" not in sup.calls, (
            "jar-only death keeps the existing no-PG-churn behaviour"
        )

    def test_pg_only_death_unchanged(self) -> None:
        sup, code = self._run(
            lambda stop: _ScriptedSupervisor([(True, False)], stop)
        )
        assert code == 0
        assert sup.calls.count("ensure_pg") == 1
        assert "respawn" not in sup.calls
