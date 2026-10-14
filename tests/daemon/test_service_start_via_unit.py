# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx daemon service start`` goes through an installed, enabled autostart unit
(nexus-crwqm).

Measured in a systemd container (T2 ``nexus_rdr/228-research-30``): with the unit
installed and enabled and the service stopped, ``service start`` spawned a
detached supervisor itself. The unit stayed inactive, so the manager neither
supervised nor restarted the service the user asked to autostart.

The manager is faked at ``installer.run_bounded`` (the module's documented seam),
so the real argv builder, the mutating-verb fence and the bounded-call helper all
run, and no ``launchctl`` / ``systemctl`` / ``schtasks`` verb reaches this box.
The lease the unit's supervisor would publish is a real lease file in a temp
config dir.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.commands import daemon as daemon_mod
from nexus.daemon import installer
from nexus.daemon.service_registry import ServiceRegistry, ServiceSupervisor, service_identity

pytestmark = pytest.mark.usefixtures("launchd_uid")

ACTIVE = installer.ActivationProbe(installer.ActivationState.ACTIVE)


def _probe(state: installer.ActivationState, detail: str = "") -> installer.ActivationProbe:
    return installer.ActivationProbe(state, detail)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "cfg"
    cd.mkdir(parents=True, exist_ok=True, mode=0o700)
    return cd


@pytest.fixture
def unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Linux service unit file in a temp unit dir, with the platform stubbed."""
    units = tmp_path / "units"
    units.mkdir()
    monkeypatch.setattr(daemon_mod, "_autostart_platform", lambda: "linux")
    monkeypatch.setattr(daemon_mod, "_autostart_install_dir", lambda: units)
    path = units / daemon_mod._SERVICE_SERVICE_NAME
    path.write_text("[Unit]\n")
    return path


def _publish_lease(config_dir: Path, port: int = 18091) -> None:
    """What the unit's supervisor does: publish a fresh lease whose supervisor
    is alive (this test process)."""
    registry = ServiceRegistry(dir=config_dir, tier="storage_service")
    ServiceSupervisor(
        registry,
        service_identity(),
        version="0.0.0",
        endpoint_provider=lambda: {"host": "127.0.0.1", "port": port, "pid": 4242, "token": "t"},
        payload={"supervisor_pid": os.getpid()},
    ).publish_once()


class FakeManager:
    """Stands in for ``installer.run_bounded``: records each manager argv and
    answers the start verb with *start_rc* (publishing the unit's lease when it
    succeeds and *publish* is set)."""

    def __init__(self, config_dir: Path, *, start_rc: int = 0, publish: bool = True) -> None:
        self.config_dir = config_dir
        self.start_rc = start_rc
        self.publish = publish
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, argv, *, timeout, **_kw):
        # The argv[0] is the resolved binary (an absolute path on Windows).
        self.calls.append([installer._manager_key(argv[0]), *argv[1:]])
        self.timeouts.append(timeout)
        if self.start_rc == 0 and self.publish:
            _publish_lease(self.config_dir)
        return subprocess.CompletedProcess(
            argv, self.start_rc, stdout="", stderr="" if self.start_rc == 0 else "Unit not found.\n"
        )


def _run_start(config_dir: Path, *extra: str):
    return CliRunner().invoke(
        daemon_mod.service_group, ["start", "--config-dir", str(config_dir), *extra]
    )


@pytest.fixture
def alive(monkeypatch: pytest.MonkeyPatch):
    """The lease's supervisor pid is this live process; on a /proc-less box
    ``process_state`` shells out to ``ps``, so answer for it."""
    with patch("nexus.daemon.service_registry.process_state", return_value="S"):
        yield


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch):
    """Today's path: ``ensure_storage_supervisor`` (the detached spawn), observed."""
    lease = SimpleNamespace(endpoint={"host": "h", "port": 1, "pid": 2}, generation=3)
    with patch.object(daemon_mod, "ensure_storage_supervisor", return_value=lease) as ensure:
        yield ensure


class TestUnitActive:
    def test_unit_active_starts_through_the_manager_and_does_not_spawn(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        with patch.object(daemon_mod, "_popen") as popen, patch.object(
            daemon_mod, "ensure_storage_supervisor"
        ) as ensure:
            result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        assert manager.calls == [["systemctl", "--user", "start", "nexus-service.service"]]
        popen.assert_not_called()
        ensure.assert_not_called()
        assert "Storage service running on 127.0.0.1:18091" in result.output

    def test_the_start_verb_is_bounded_by_the_manager_action_timeout(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        assert _run_start(config_dir).exit_code == 0
        assert manager.timeouts == [installer._MANAGER_ACTION_TIMEOUT_S]

    @pytest.mark.parametrize(
        ("platform", "expected"),
        [
            ("linux", ["systemctl", "--user", "start", "nexus-service.service"]),
            ("darwin", ["launchctl", "kickstart", f"gui/{os.getuid()}/com.nexus.service"]),
            ("win32", ["schtasks", "/Run", "/TN", "NexusStorageService"]),
        ],
    )
    def test_the_start_command_per_platform(
        self,
        platform: str,
        expected: list[str],
        config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        alive: None,
    ) -> None:
        units = tmp_path / "units"
        units.mkdir()
        monkeypatch.setattr(daemon_mod, "_autostart_platform", lambda: platform)
        monkeypatch.setattr(daemon_mod, "_autostart_install_dir", lambda: units)
        (units / daemon_mod._autostart_filename_service()).write_text("x")
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        with patch.object(daemon_mod, "_popen") as popen:
            result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        assert manager.calls == [expected]
        popen.assert_not_called()

    def test_a_live_lease_is_reported_and_the_manager_is_not_asked(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:
        """A second ``start`` while the service runs is a no-op report, as before."""
        _publish_lease(config_dir, port=18092)
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        with patch.object(daemon_mod, "_popen") as popen:
            result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        assert "18092" in result.output
        assert manager.calls == []
        popen.assert_not_called()

    def test_a_unit_that_never_publishes_is_a_clear_error_not_a_second_spawn(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:
        manager = FakeManager(config_dir, publish=False)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        monkeypatch.setattr(daemon_mod, "_UNIT_START_WAIT_S", 0.05)
        with patch.object(daemon_mod, "_popen") as popen, patch.object(
            daemon_mod, "ensure_storage_supervisor"
        ) as ensure:
            result = _run_start(config_dir)
        assert result.exit_code == 2
        assert "nexus-service.service was started" in result.output
        assert "did not become ready" in result.output
        popen.assert_not_called()
        ensure.assert_not_called()


class TestUnitNotUsed:
    def test_no_unit_file_takes_todays_path_silently(
        self, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, today
    ) -> None:
        monkeypatch.setattr(daemon_mod, "_autostart_platform", lambda: "linux")
        monkeypatch.setattr(daemon_mod, "_autostart_install_dir", lambda: tmp_path / "no-units")
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        today.assert_called_once()
        assert manager.calls == []
        assert "autostart unit" not in result.output

    @pytest.mark.parametrize(
        "state",
        [
            installer.ActivationState.NOT_ACTIVE,
            installer.ActivationState.UNREACHABLE,
            installer.ActivationState.NO_MANAGER,
        ],
    )
    def test_a_unit_the_manager_does_not_report_active_takes_todays_path_with_one_line(
        self,
        state: installer.ActivationState,
        config_dir: Path,
        unit: Path,
        monkeypatch: pytest.MonkeyPatch,
        today,
    ) -> None:
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(
            installer,
            "autostart_activation_state",
            lambda dest, *, tier: _probe(state, "the manager said so"),
        )
        result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        today.assert_called_once()
        assert manager.calls == []
        lines = [l for l in result.output.splitlines() if "autostart unit" in l]
        assert len(lines) == 1, result.output
        assert state.value in lines[0] and "the manager said so" in lines[0]

    def test_a_failing_manager_start_takes_todays_path_with_one_line(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, today
    ) -> None:
        manager = FakeManager(config_dir, start_rc=5)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        assert manager.calls == [["systemctl", "--user", "start", "nexus-service.service"]]
        today.assert_called_once()
        lines = [l for l in result.output.splitlines() if "autostart unit" in l]
        assert len(lines) == 1, result.output
        assert "exited 5" in lines[0] and "Unit not found." in lines[0]

    def test_a_manager_start_that_times_out_takes_todays_path_with_one_line(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, today
    ) -> None:
        def hung(argv, *, timeout, **_kw):
            raise subprocess.TimeoutExpired(argv, timeout)

        monkeypatch.setattr(installer, "run_bounded", hung)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        today.assert_called_once()
        assert "did not answer within" in result.output

    def test_a_manager_that_vanishes_takes_todays_path_with_one_line(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, today
    ) -> None:
        def gone(argv, *, timeout, **_kw):
            raise FileNotFoundError(2, "No such file", argv[0])

        monkeypatch.setattr(installer, "run_bounded", gone)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        today.assert_called_once()
        assert "could not run" in result.output


class TestForegroundUnchanged:
    def test_foreground_never_consults_the_unit(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--foreground`` is what the unit itself runs: routing it back through
        the manager would loop."""
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        probe_calls: list[object] = []
        monkeypatch.setattr(
            installer,
            "autostart_activation_state",
            lambda dest, *, tier: probe_calls.append(dest) or ACTIVE,
        )
        with patch(
            "nexus.daemon.storage_service_daemon.run_storage_supervisor", return_value=0
        ) as run, patch.object(daemon_mod, "ensure_storage_supervisor") as ensure:
            result = _run_start(config_dir, "--foreground")
        assert result.exit_code == 0, result.output
        run.assert_called_once()
        ensure.assert_not_called()
        assert manager.calls == [] and probe_calls == []
