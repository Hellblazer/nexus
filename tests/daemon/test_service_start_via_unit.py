# SPDX-License-Identifier: AGPL-3.0-or-later
"""Starting the storage service goes through an installed, enabled autostart unit
(nexus-crwqm).

The unit-first logic lives in ``ensure_storage_supervisor``, so these tests cover
every caller: ``nx daemon service start``, ``nx init --service`` and the
replace-quiesce restart.

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
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.commands import daemon as daemon_mod
from nexus.commands import init as init_mod
from nexus.daemon import installer
from nexus.daemon import storage_service_daemon as ssd
from nexus.daemon import service_registry as sr
from nexus.daemon.replace_quiesce import default_ops
from nexus.daemon.service_registry import ServiceRegistry, ServiceSupervisor, service_identity
from nexus.daemon.windows_autostart import TASK_FILENAME, task_xml
from tests._fence_home import _link_through
from tests._module_seam import setattr_in

pytestmark = pytest.mark.usefixtures("launchd_uid", "no_explicit_artifact")

ACTIVE = installer.ActivationProbe(installer.ActivationState.ACTIVE)


def _probe(state: installer.ActivationState, detail: str = "") -> installer.ActivationProbe:
    return installer.ActivationProbe(state, detail)


@pytest.fixture
def no_explicit_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient launch-artifact request: the tests that want one set it."""
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)


def _write_unit(path: Path, platform: str, config_dir: Path | None) -> None:
    """Write the unit body the installer renders for *platform*, baking
    *config_dir* (resolved, as the installer does). ``None`` writes a legacy
    flagless systemd unit."""
    if config_dir is None:
        path.write_text(
            "[Service]\nExecStart=/usr/local/bin/nx daemon service start --foreground\n", encoding="utf-8"
        )
        return
    _write_unit_literal(path, platform, str(config_dir.resolve()))


def _write_unit_literal(path: Path, platform: str, baked: str) -> None:
    """Write the rendered unit for *platform* with *baked* exactly as given
    (the installer resolves; a hand-edited or older unit may not)."""
    if platform == "win32":
        body = task_xml(sid="S-1-5-21-1-2-3-1001", pythonw=r"C:\Py\pythonw.exe", config_dir=baked)
    else:
        body = daemon_mod._render_template(
            path.name,
            nx_bin=["/usr/local/bin/nx"],
            log_dir="/tmp/nexus-logs",
            path_env="/usr/bin:/bin",
            config_dir=baked,
        )
    path.write_text(body, encoding="utf-8")


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
    monkeypatch.setattr(daemon_mod, "_autostart_log_dir", lambda: tmp_path / "unit-logs")
    path = units / daemon_mod._SERVICE_SERVICE_NAME
    _write_unit(path, "linux", tmp_path / "cfg")
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

    def __init__(
        self,
        config_dir: Path,
        *,
        start_rc: int = 0,
        publish: bool = True,
        events: list[str] | None = None,
    ) -> None:
        self.config_dir = config_dir
        self.events = events
        self.start_rc = start_rc
        self.publish = publish
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, argv, *, timeout, **_kw):
        # The argv[0] is the resolved binary (an absolute path on Windows).
        self.calls.append([installer._manager_key(argv[0]), *argv[1:]])
        self.timeouts.append(timeout)
        if self.events is not None:
            self.events.append("manager")
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
def today(config_dir: Path):
    """Today's path: the detached spawn, observed at ``_popen``. The fake child
    publishes the lease the real supervisor would, and stays running."""

    def _spawn(argv, **_kw):
        _publish_lease(config_dir, port=18093)
        return SimpleNamespace(poll=lambda: None)

    with patch("nexus.daemon.service_registry.process_state", return_value="S"), patch.object(
        daemon_mod, "_popen", side_effect=_spawn
    ) as popen:
        yield popen


class TestUnitActive:
    def test_unit_active_starts_through_the_manager_and_does_not_spawn(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        with patch.object(daemon_mod, "_popen") as popen:
            result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        assert manager.calls == [["systemctl", "--user", "start", "nexus-service.service"]]
        popen.assert_not_called()
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
            # {uid} is filled in from launchd_uid: no os.getuid at collection
            # on a Windows host (RDR-224, nexus-f9bgu.44).
            ("darwin", ["launchctl", "kickstart", "gui/{uid}/com.nexus.service"]),
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
        launchd_uid: int,
    ) -> None:
        expected = [part.replace("{uid}", str(launchd_uid)) for part in expected]
        units = tmp_path / "units"
        units.mkdir()
        monkeypatch.setattr(daemon_mod, "_autostart_platform", lambda: platform)
        monkeypatch.setattr(daemon_mod, "_autostart_install_dir", lambda: units)
        _write_unit(units / daemon_mod._autostart_filename_service(), platform, config_dir)
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
        with patch.object(daemon_mod, "_popen") as popen:
            result = _run_start(config_dir)
        assert result.exit_code == 2
        assert "nexus-service.service was started" in result.output
        assert "did not become ready" in result.output
        popen.assert_not_called()

    def test_the_lease_may_appear_only_after_several_poll_ticks(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:
        """The unit's supervisor takes a while to boot: the poll sleeps, looks
        again, and returns the lease once it appears, before the deadline."""
        manager = FakeManager(config_dir, publish=False)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        sleeps: list[float] = []

        def tick(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 3:
                _publish_lease(config_dir, port=18095)

        setattr_in(monkeypatch, daemon_mod, "time.sleep", tick)
        with patch.object(daemon_mod, "_popen") as popen:
            result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        assert len(sleeps) == 3
        assert "18095" in result.output
        popen.assert_not_called()


class TestUnitStartTimeoutMessage:
    """The unit's supervisor writes to the unit's own log, never to the
    detached spawn's crash log, so the timeout names that log."""

    def _never_publishes(
        self,
        config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        err_log: Path | None = None,
        new_error: str | None = None,
    ) -> None:
        """The start verb succeeds and no lease appears. It appends *new_error*
        to *err_log* (what the supervisor would write), or writes nothing."""

        def start(argv, *, timeout, **_kw):
            if err_log is not None and new_error is not None:
                err_log.parent.mkdir(parents=True, exist_ok=True)
                with err_log.open("a", encoding="utf-8") as fh:
                    fh.write(f"Error: {new_error}\n")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(installer, "run_bounded", start)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        monkeypatch.setattr(daemon_mod, "_UNIT_START_WAIT_S", 0.05)

    def test_linux_names_the_units_err_log_and_its_last_error(
        self, config_dir: Path, unit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:
        err_log = tmp_path / "unit-logs" / "nexus-service.err"
        err_log.parent.mkdir(parents=True)
        err_log.write_text("Error: an earlier boot's failure\n", encoding="utf-8")
        self._never_publishes(
            config_dir, monkeypatch, err_log=err_log, new_error="pg_credentials missing; run nx init"
        )
        result = _run_start(config_dir)
        assert result.exit_code == 2
        assert str(err_log) in result.output
        assert "storage_service.crash.log" not in result.output
        assert "pg_credentials missing" in result.output
        assert "an earlier boot" not in result.output
        assert "keeps restarting" in result.output

    def test_an_error_from_before_this_start_is_not_reported(
        self, config_dir: Path, unit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:
        """The supervisor writes no new ``Error:`` line during this start, so the
        log's older one is not its reason: the mark taken before the start verb
        excludes it."""
        err_log = tmp_path / "unit-logs" / "nexus-service.err"
        err_log.parent.mkdir(parents=True)
        err_log.write_text("Error: an earlier boot's failure\n", encoding="utf-8")
        self._never_publishes(config_dir, monkeypatch, err_log=err_log)
        result = _run_start(config_dir)
        assert result.exit_code == 2
        assert str(err_log) in result.output
        assert "an earlier boot" not in result.output
        assert "The supervisor reported" not in result.output

    def test_windows_names_the_task_and_the_launchers_logs(
        self, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:
        units = tmp_path / "units"
        units.mkdir()
        monkeypatch.setattr(daemon_mod, "_autostart_platform", lambda: "win32")
        monkeypatch.setattr(daemon_mod, "_autostart_install_dir", lambda: units)
        _write_unit(units / daemon_mod._autostart_filename_service(), "win32", config_dir)
        crash_log = config_dir / "logs" / "storage_service.crash.log"
        crash_log.parent.mkdir(parents=True)
        crash_log.write_text("Error: an earlier boot's failure\n", encoding="utf-8")
        self._never_publishes(
            config_dir, monkeypatch, err_log=crash_log, new_error="engine exited during start"
        )
        result = _run_start(config_dir)
        assert result.exit_code == 2
        assert "NexusStorageService" in result.output
        assert "nexus-service-task.xml" not in result.output
        assert "windows_autostart.crash.log" in result.output
        assert "The supervisor reported: engine exited during start" in result.output
        assert "an earlier boot" not in result.output


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


class TestUnitConfigDirMustMatch:
    """The unit runs the supervisor for the dir baked into it; a start for a
    different dir must not be answered by that unit's service."""

    def test_a_unit_baked_for_another_dir_takes_todays_path_with_one_line(
        self, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, today
    ) -> None:
        units = tmp_path / "units"
        units.mkdir()
        monkeypatch.setattr(daemon_mod, "_autostart_platform", lambda: "linux")
        monkeypatch.setattr(daemon_mod, "_autostart_install_dir", lambda: units)
        other = tmp_path / "other-profile"
        other.mkdir()
        _write_unit(units / daemon_mod._SERVICE_SERVICE_NAME, "linux", other)
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        today.assert_called_once()
        assert manager.calls == []
        lines = [l for l in result.output.splitlines() if "autostart unit" in l]
        assert len(lines) == 1, result.output
        assert str(other.resolve()) in lines[0] and str(config_dir.resolve()) in lines[0]

    def test_a_flagless_legacy_unit_means_the_default_dir(
        self, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, today
    ) -> None:
        units = tmp_path / "units"
        units.mkdir()
        monkeypatch.setattr(daemon_mod, "_autostart_platform", lambda: "linux")
        monkeypatch.setattr(daemon_mod, "_autostart_install_dir", lambda: units)
        _write_unit(units / daemon_mod._SERVICE_SERVICE_NAME, "linux", None)
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        today.assert_called_once()
        assert manager.calls == []
        assert str((Path.home() / ".config" / "nexus").resolve()) in result.output

    def _platform_units(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> Path:
        units = tmp_path / "units"
        units.mkdir()
        monkeypatch.setattr(daemon_mod, "_autostart_platform", lambda: platform)
        monkeypatch.setattr(daemon_mod, "_autostart_install_dir", lambda: units)
        return units / daemon_mod._autostart_filename_service()

    def _active(self, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> FakeManager:
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        return manager

    def test_windows_compares_the_config_dir_ignoring_case(
        self, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:
        """Windows paths are case-insensitive: a task baked with another
        spelling of the same dir is the right unit."""
        _write_unit_literal(
            self._platform_units(tmp_path, monkeypatch, "win32"),
            "win32",
            str(config_dir.resolve()).upper(),
        )
        manager = self._active(config_dir, monkeypatch)
        with patch.object(daemon_mod, "_popen") as popen:
            result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        assert manager.calls == [["schtasks", "/Run", "/TN", "NexusStorageService"]]
        popen.assert_not_called()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="a Windows host's Path.resolve() returns an existing dir's real case, "
        "so the other spelling resolves to the same path before the comparison",
    )
    def test_posix_compares_the_config_dir_case_sensitively(
        self, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, today
    ) -> None:
        _write_unit_literal(
            self._platform_units(tmp_path, monkeypatch, "linux"),
            "linux",
            str(config_dir.resolve()).upper(),
        )
        manager = self._active(config_dir, monkeypatch)
        result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        today.assert_called_once()
        assert manager.calls == []
        lines = [l for l in result.output.splitlines() if "autostart unit" in l]
        assert len(lines) == 1, result.output

    def test_windows_a_task_baked_for_another_dir_takes_todays_path_with_one_line(
        self, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, today
    ) -> None:
        other = tmp_path / "other-profile"
        other.mkdir()
        _write_unit(self._platform_units(tmp_path, monkeypatch, "win32"), "win32", other)
        manager = self._active(config_dir, monkeypatch)
        result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        today.assert_called_once()
        assert manager.calls == []
        lines = [l for l in result.output.splitlines() if "autostart unit" in l]
        assert len(lines) == 1, result.output
        assert str(other.resolve()) in lines[0] and str(config_dir.resolve()) in lines[0]

    @pytest.mark.parametrize("unit_names_the_link", [True, False])
    def test_a_symlinked_config_dir_is_the_same_dir(
        self,
        unit_names_the_link: bool,
        config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        alive: None,
    ) -> None:
        """The comparison resolves both sides: a unit baked with the symlink and
        a start given the real dir (or the reverse) is the same config dir."""
        link = tmp_path / "cfg-link"
        # A junction on a Windows account without the symlink privilege
        # (the win-release runner, WinError 1314).
        _link_through(link, config_dir)
        baked, given = (link, config_dir) if unit_names_the_link else (config_dir, link)
        _write_unit_literal(
            self._platform_units(tmp_path, monkeypatch, "linux"), "linux", str(baked)
        )
        manager = self._active(config_dir, monkeypatch)
        with patch.object(daemon_mod, "_popen") as popen:
            result = _run_start(given)
        assert result.exit_code == 0, result.output
        assert manager.calls == [["systemctl", "--user", "start", "nexus-service.service"]]
        popen.assert_not_called()

    def test_a_unit_whose_config_dir_cannot_be_read_takes_todays_path_with_one_line(
        self, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, today
    ) -> None:
        self._platform_units(tmp_path, monkeypatch, "linux").write_text("[Service]\nType=simple\n", encoding="utf-8")
        manager = self._active(config_dir, monkeypatch)
        result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        today.assert_called_once()
        assert manager.calls == []
        lines = [l for l in result.output.splitlines() if "autostart unit" in l]
        assert len(lines) == 1, result.output
        assert "could not be read" in lines[0]

    @pytest.mark.parametrize("error", [OSError("Permission denied"), RuntimeError("Symlink loop")])
    def test_a_config_dir_that_cannot_be_resolved_takes_todays_path_with_one_line(
        self,
        error: Exception,
        config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        today,
    ) -> None:
        unresolvable = tmp_path / "unresolvable"
        _write_unit_literal(
            self._platform_units(tmp_path, monkeypatch, "linux"), "linux", str(unresolvable)
        )
        real_resolve = Path.resolve

        def resolve(self: Path, *args, **kwargs):
            if self == unresolvable:
                raise error
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
        manager = self._active(config_dir, monkeypatch)
        result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        today.assert_called_once()
        assert manager.calls == []
        lines = [l for l in result.output.splitlines() if "autostart unit" in l]
        assert len(lines) == 1, result.output
        assert "could not be resolved" in lines[0] and str(error) in lines[0]


class TestExplicitArtifactBypassesTheUnit:
    """A NEXUS_SERVICE_BIN / NEXUS_SERVICE_JAR in THIS process never reaches the
    unit's supervisor, which runs with the unit's environment."""

    @pytest.mark.parametrize("var", ["NEXUS_SERVICE_BIN", "NEXUS_SERVICE_JAR"])
    def test_an_explicit_artifact_takes_todays_path_with_one_line(
        self,
        var: str,
        config_dir: Path,
        unit: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        today,
    ) -> None:
        artifact = tmp_path / "nexus-service-artifact"
        artifact.write_text("#!/bin/sh\n", encoding="utf-8")
        artifact.chmod(0o755)
        monkeypatch.setenv(var, str(artifact))
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        result = _run_start(config_dir)
        assert result.exit_code == 0, result.output
        today.assert_called_once()
        assert manager.calls == []
        lines = [l for l in result.output.splitlines() if "autostart unit" in l]
        assert len(lines) == 1, result.output
        assert var in lines[0]
        assert str(artifact) not in lines[0]

    @pytest.mark.parametrize("var", ["NEXUS_SERVICE_BIN", "NEXUS_SERVICE_JAR"])
    def test_a_set_but_unresolvable_artifact_still_bypasses_the_unit(
        self,
        var: str,
        config_dir: Path,
        unit: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The direct spawn (whose child reports the unresolvable path loudly)
        takes over; the unit is not asked to start in its place."""
        monkeypatch.setenv(var, str(tmp_path / "no-such-artifact"))
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        with patch.object(daemon_mod, "_popen") as popen:
            result = _run_start(config_dir)
        assert manager.calls == []
        popen.assert_called_once()
        lines = [l for l in result.output.splitlines() if "autostart unit" in l]
        assert len(lines) == 1, result.output
        assert var in lines[0]



class TestAutostartUnitConfigDir:
    """``installer.autostart_unit_config_dir`` reads back what the renderers bake."""

    @pytest.mark.parametrize(
        "name", [daemon_mod._SERVICE_SERVICE_NAME, daemon_mod._SERVICE_PLIST_NAME]
    )
    def test_posix_units_round_trip_a_dir_with_a_space(self, name: str, tmp_path: Path) -> None:
        baked = tmp_path / "my profile" / "nexus"
        baked.mkdir(parents=True)
        path = tmp_path / name
        _write_unit(path, "linux", baked)
        assert installer.autostart_unit_config_dir(path) == baked.resolve()

    @pytest.mark.parametrize(
        "baked",
        [r"C:\Users\Sam Q\AppData\nexus", r"C:\cfg\trailing\\", r'C:\odd"quote\dir'],
    )
    def test_the_windows_task_round_trips(self, baked: str, tmp_path: Path) -> None:
        path = tmp_path / TASK_FILENAME
        path.write_text(task_xml(sid="S-1-5-21-1", pythonw=r"C:\Py\pythonw.exe", config_dir=baked), encoding="utf-8")
        assert str(installer.autostart_unit_config_dir(path)) == str(Path(baked))

    def test_an_equals_spelling_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / daemon_mod._SERVICE_SERVICE_NAME
        path.write_text("[Service]\nExecStart=/bin/nx daemon service start --foreground --config-dir=/srv/nx\n", encoding="utf-8")
        assert installer.autostart_unit_config_dir(path) == Path("/srv/nx")

    def test_no_flag_is_the_default_dir(self, tmp_path: Path) -> None:
        path = tmp_path / daemon_mod._SERVICE_SERVICE_NAME
        _write_unit(path, "linux", None)
        assert installer.autostart_unit_config_dir(path) == Path.home() / ".config" / "nexus"

    @pytest.mark.parametrize(
        ("name", "body"),
        [
            (daemon_mod._SERVICE_SERVICE_NAME, "[Service]\nType=simple\n"),
            (daemon_mod._SERVICE_SERVICE_NAME, "[Service]\nExecStart=/bin/nx --config-dir\n"),
            (daemon_mod._SERVICE_PLIST_NAME, "not a plist"),
            ("nexus-service-task.xml", "<Task></Task>"),
        ],
    )
    def test_an_unreadable_unit_is_a_value_error(self, name: str, body: str, tmp_path: Path) -> None:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ValueError):
            installer.autostart_unit_config_dir(path)

    def test_a_missing_file_is_a_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            installer.autostart_unit_config_dir(tmp_path / daemon_mod._SERVICE_SERVICE_NAME)


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


class TestOtherCallersOfEnsure:
    """``nx init --service`` and the replace-quiesce restart call
    ``ensure_storage_supervisor`` directly; they get the unit-first start too."""

    def _active(self, config_dir: Path, monkeypatch: pytest.MonkeyPatch, **kw) -> FakeManager:
        manager = FakeManager(config_dir, **kw)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(installer, "autostart_activation_state", lambda dest, *, tier: ACTIVE)
        return manager

    def test_init_start_step_goes_through_the_unit(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        manager = self._active(config_dir, monkeypatch)
        with patch.object(daemon_mod, "_popen") as popen, patch.object(
            init_mod, "_report_stray_t2_launchagent_cleanup"
        ):
            lease = init_mod._start_service_step()
        assert manager.calls == [["systemctl", "--user", "start", "nexus-service.service"]]
        popen.assert_not_called()
        assert lease.endpoint["port"] == 18091

    def test_init_after_the_unit_is_already_running_does_not_start_twice(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:
        """``enable --now`` already started the unit and its supervisor published:
        the ensure that follows finds that lease and neither asks the manager
        again nor spawns."""

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        _publish_lease(config_dir, port=18094)
        manager = self._active(config_dir, monkeypatch)
        with patch.object(daemon_mod, "_popen") as popen, patch.object(
            init_mod, "_report_stray_t2_launchagent_cleanup"
        ):
            lease = init_mod._start_service_step()
        assert manager.calls == []
        popen.assert_not_called()
        assert lease.endpoint["port"] == 18094

    def test_init_with_a_disabled_unit_still_spawns(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, today
    ) -> None:

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        monkeypatch.setattr(
            installer, "autostart_activation_state",
            lambda dest, *, tier: _probe(installer.ActivationState.NOT_ACTIVE, "disabled"),
        )
        with patch.object(init_mod, "_report_stray_t2_launchagent_cleanup"):
            lease = init_mod._start_service_step()
        today.assert_called_once()
        assert manager.calls == []
        assert lease.endpoint["port"] == 18093

    def test_replace_quiesce_restart_goes_through_the_unit(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:

        manager = self._active(config_dir, monkeypatch)
        with patch.object(daemon_mod, "_popen") as popen:
            default_ops(config_dir).start_service()
        assert manager.calls == [["systemctl", "--user", "start", "nexus-service.service"]]
        popen.assert_not_called()

    def test_replace_quiesce_restart_without_a_unit_spawns_as_before(
        self, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, today
    ) -> None:

        monkeypatch.setattr(daemon_mod, "_autostart_platform", lambda: "linux")
        monkeypatch.setattr(daemon_mod, "_autostart_install_dir", lambda: tmp_path / "no-units")
        manager = FakeManager(config_dir)
        monkeypatch.setattr(installer, "run_bounded", manager)
        default_ops(config_dir).start_service()
        today.assert_called_once()
        assert manager.calls == []

    def test_replace_quiesce_restart_clears_the_stop_request_before_starting_the_unit(
        self, config_dir: Path, unit: Path, monkeypatch: pytest.MonkeyPatch, alive: None
    ) -> None:
        """The quiesce stop wrote a stop marker (Windows); the restart is a start,
        which ends it before the manager is asked, or the launcher would not spawn."""
        seen: list[str] = []
        real_clear = sr.clear_stop_marker
        monkeypatch.setattr(
            sr, "clear_stop_marker", lambda *a, **k: (seen.append("clear"), real_clear(*a, **k))[1]
        )
        manager = self._active(config_dir, monkeypatch, events=seen)
        default_ops(config_dir).start_service()
        assert seen == ["clear", "manager"]
