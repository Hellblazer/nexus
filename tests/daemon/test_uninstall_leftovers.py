# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-224 (nexus-f9bgu): what ``nx uninstall --yes --remove-data`` leaves behind,
and whether its summary tells the truth about the stop.

Measured on a clean Windows 11 guest with conexus 7.72.1 (T2
``nexus_rdr/224-windows-py312-and-uninstall-walk``):

1. ``%LOCALAPPDATA%\\nexus\\autostart`` survived the uninstall as an empty
   directory. The Windows autostart install creates it (the kept copy of the
   Task Scheduler XML lives there) and nothing removed it.
2. The summary said "daemon stop not confirmed" with zero nexus processes left.
   That line is the RETIRED T2 daemon's: no current install has a T2 unit, so
   ``uninstall_autostart(tier="t2")`` returns NOT_INSTALLED and the report read
   "nothing to stop" as "could not confirm the stop", on every platform.

Platform is injected through ``_autostart_platform``; the unit manager is a fake.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

#: The darwin arms these tests run read os.getuid(), absent on Windows (see conftest).
pytestmark = pytest.mark.usefixtures("launchd_uid")

from nexus.commands import daemon as daemon_cmd
from nexus.daemon import installer


def _ok_manager(argv: list[str], *, timeout: float, **_kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, "", "")


def _layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str) -> tuple[Path, Path]:
    """The real per-platform autostart and log dirs, rooted at a fake home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: platform)
    monkeypatch.setattr(daemon_cmd, "_resolve_nx_bin", lambda: ["/opt/conexus/bin/nx"])
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(installer, "run_bounded", _ok_manager)
    monkeypatch.setattr(installer, "_windows_task_registered", lambda: False)
    monkeypatch.setattr(installer, "_probe_survivors", lambda *, tier: ())
    install_dir = daemon_cmd._autostart_install_dir()
    install_dir.mkdir(parents=True)
    (install_dir / daemon_cmd._autostart_filename_service()).write_text("<unit/>")
    return install_dir, daemon_cmd._autostart_log_dir()


class TestWindowsAutostartDirIsRemoved:
    def test_uninstall_removes_the_autostart_dir_and_its_empty_nexus_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_dir, _ = _layout(monkeypatch, tmp_path, "win32")
        root = install_dir.parent
        assert root == tmp_path / "home" / "AppData" / "Local" / "nexus"

        result = installer.uninstall_autostart(tier="service")

        assert result.status is installer.UninstallStatus.REMOVED
        assert not install_dir.exists(), "the empty autostart dir the install created must go"
        assert not root.exists(), "an empty %LOCALAPPDATA%\\nexus must go too"
        assert (tmp_path / "home" / "AppData" / "Local").is_dir(), "never above the nexus dir"

    def test_a_nexus_parent_holding_other_files_is_left_and_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_dir, _ = _layout(monkeypatch, tmp_path, "win32")
        root = install_dir.parent
        keep = root / "someone-elses.txt"
        keep.write_text("not ours")
        monkeypatch.setattr(installer, "_stop_service_stack_best_effort", lambda: (True, None))

        report = installer.uninstall_daemon(confirm=True, remove_data=True)

        assert not install_dir.exists()
        assert keep.read_text() == "not ours", "a file uninstall did not create is never touched"
        assert any(str(root) in w and "someone-elses.txt" in w for w in report.warnings), report.warnings

    def test_dirs_not_named_for_nexus_are_never_pruned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ownership rule is the path, not emptiness: an install dir that a
        test or a caller pointed somewhere else is not ours to remove."""
        monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: "win32")
        elsewhere = tmp_path / "autostart"
        elsewhere.mkdir()
        monkeypatch.setattr(daemon_cmd, "_autostart_install_dir", lambda: elsewhere)
        monkeypatch.setattr(daemon_cmd, "_autostart_log_dir", lambda: tmp_path / "logs")

        assert installer._prune_autostart_dirs() == []
        assert elsewhere.is_dir()


class TestPosixSharedUnitDirsAreKept:
    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_launchagents_and_systemd_user_dirs_survive_even_when_empty(
        self, platform: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """~/Library/LaunchAgents and ~/.config/systemd/user hold every other
        program's units too, so emptiness does not make them nexus's."""
        install_dir, _ = _layout(monkeypatch, tmp_path, platform)

        installer.uninstall_autostart(tier="service")

        assert install_dir.is_dir() and not any(install_dir.iterdir())

    def test_linux_log_dir_and_service_log_go_with_remove_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """~/.local/state/nexus is created by the systemd install for the unit's
        log; with --remove-data it goes with everything else nexus wrote."""
        _, log_dir = _layout(monkeypatch, tmp_path, "linux")
        assert log_dir == tmp_path / "home" / ".local" / "state" / "nexus"
        log_dir.mkdir(parents=True)
        (log_dir / installer.AUTOSTART_SERVICE_LOG_NAME).write_text("log\n")
        monkeypatch.setattr(installer, "_stop_service_stack_best_effort", lambda: (True, None))

        installer.uninstall_daemon(confirm=True, remove_data=True)

        assert not log_dir.exists()
        assert (tmp_path / "home" / ".local" / "state").is_dir()

    def test_the_service_log_is_kept_without_remove_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, log_dir = _layout(monkeypatch, tmp_path, "linux")
        log_dir.mkdir(parents=True)
        log = log_dir / installer.AUTOSTART_SERVICE_LOG_NAME
        log.write_text("log\n")
        monkeypatch.setattr(installer, "_stop_service_stack_best_effort", lambda: (True, None))

        installer.uninstall_daemon(confirm=True, remove_data=False)

        assert log.exists()

    def test_macos_shared_logs_dir_loses_only_the_nexus_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, log_dir = _layout(monkeypatch, tmp_path, "darwin")
        log_dir.mkdir(parents=True)
        (log_dir / installer.AUTOSTART_SERVICE_LOG_NAME).write_text("log\n")
        monkeypatch.setattr(installer, "_stop_service_stack_best_effort", lambda: (True, None))

        installer.uninstall_daemon(confirm=True, remove_data=True)

        assert log_dir.is_dir(), "~/Library/Logs is shared"
        assert not (log_dir / installer.AUTOSTART_SERVICE_LOG_NAME).exists()

    def test_the_log_name_is_the_one_the_unit_templates_write(self) -> None:
        for name in ("com.nexus.service.plist", "nexus-service.service"):
            body = daemon_cmd._read_template(name)
            assert f"__LOG_DIR__/{installer.AUTOSTART_SERVICE_LOG_NAME}" in body, name


class TestStopSummaryTellsTheTruth:
    @pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
    def test_no_t2_unit_is_not_an_unconfirmed_stop(
        self, platform: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _layout(monkeypatch, tmp_path, platform)
        monkeypatch.setattr(installer, "_stop_service_stack_best_effort", lambda: (True, None))

        report = installer.uninstall_daemon(confirm=True, remove_data=True)

        assert report.unit_status is installer.UninstallStatus.NOT_INSTALLED
        assert "not confirmed" not in report.message, report.message
        assert report.daemon_stopped is True
        assert "service stack stopped" in report.message

    def test_a_failed_stop_command_with_nothing_running_is_stopped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stop verb exits non-zero when nothing was running; the processes
        being gone is what the summary reports, and the exit stays a warning."""
        _layout(monkeypatch, tmp_path, "win32")
        monkeypatch.setattr(
            installer, "_stop_service_stack_best_effort",
            lambda: (False, "service stop exited 1: not running"),
        )

        report = installer.uninstall_daemon(confirm=True)

        assert report.service_stopped is True
        assert "service stack stopped" in report.message
        assert any("exited 1" in w for w in report.warnings)

    def test_a_survivor_after_the_stop_is_not_confirmed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _layout(monkeypatch, tmp_path, "win32")
        monkeypatch.setattr(installer, "_stop_service_stack_best_effort", lambda: (True, None))
        calls: list[str] = []

        def survivors(*, tier: str) -> tuple[str, ...]:
            calls.append(tier)
            return ("Postgres is still accepting connections on 127.0.0.1:5433",)

        monkeypatch.setattr(installer, "_probe_survivors", survivors)

        report = installer.uninstall_daemon(confirm=True)

        assert report.service_stopped is False
        assert "service stop not confirmed" in report.message
        assert any("127.0.0.1:5433" in w for w in report.warnings)

    def test_an_unusable_probe_falls_back_to_the_stop_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _layout(monkeypatch, tmp_path, "linux")
        monkeypatch.setattr(installer, "_stop_service_stack_best_effort", lambda: (False, "boom"))

        def broken(*, tier: str) -> tuple[str, ...]:
            raise RuntimeError("probe broke")

        monkeypatch.setattr(installer, "_probe_survivors", broken)

        report = installer.uninstall_daemon(confirm=True)

        assert report.service_stopped is False
        assert "service stop not confirmed" in report.message
