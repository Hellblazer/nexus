# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-126 §2 (nexus-dyqu7): TDD contract for the lifted ``nexus.daemon.installer``.

The T2 autostart install/uninstall logic is lifted out of the Click
command bodies in ``src/nexus/commands/daemon.py`` into pure library
functions so it can be called in-process by:

- ``nexus.mcp._first_run.ensure_installed_and_running`` (first-run on
  MCP startup, which needs a STRUCTURED status to drive the banner), and
- the ``daemon_uninstall`` MCP tool (RDR-126 §4), and
- the existing ``nx daemon t2 install/uninstall`` CLI (now thin wrappers).

These tests pin the structured contract. Library functions NEVER call
``click.echo`` / ``sys.exit``; they return ``InstallResult`` /
``UninstallResult`` or raise typed ``InstallerError`` subclasses.

The generic autostart helpers stay in ``daemon.py`` (shared with the T3
paths); ``installer`` delegates to them, so tests stub the same
``daemon_cmd._autostart_*`` indirection points used by the T3 install
tests. ``launchctl`` / ``systemctl`` shell-out is mocked; template
substitution + file placement are exercised for real.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.commands import daemon as daemon_cmd
from nexus.daemon import installer


def _set_platform(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: platform)


def _stub_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        daemon_cmd, "_autostart_install_dir", lambda: tmp_path / "units"
    )
    monkeypatch.setattr(
        daemon_cmd, "_autostart_log_dir", lambda: tmp_path / "logs"
    )
    monkeypatch.setattr(daemon_cmd, "_resolve_nx_bin", lambda: ["/opt/conexus/bin/nx"])


class TestPublicSurface:
    def test_status_enum_values(self) -> None:
        # Values are serialized into the daemon_uninstall MCP tool's text
        # response (report.unit_status.value), so lock them exactly.
        assert installer.InstallStatus.NEWLY_INSTALLED.value == "newly_installed"
        assert installer.InstallStatus.ALREADY_PRESENT.value == "already_present"
        assert installer.InstallStatus.FAILED.value == "failed"
        assert installer.UninstallStatus.REMOVED.value == "removed"
        assert installer.UninstallStatus.NOT_INSTALLED.value == "not_installed"

    def test_error_hierarchy(self) -> None:
        assert issubclass(installer.SymlinkRefusedError, installer.InstallerError)
        assert issubclass(installer.ContentDiffersError, installer.InstallerError)
        assert issubclass(installer.ActivationError, installer.InstallerError)


class TestInstallFreshMacOS:
    def test_install_writes_plist_activates_and_reports_newly_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)

        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            result = installer.install_autostart()

        dest = tmp_path / "units" / "com.nexus.t2.plist"
        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert result.dest == dest
        assert dest.exists()
        body = dest.read_text()
        assert "<string>/opt/conexus/bin/nx</string>" in body
        assert "<string>t2</string>" in body
        assert "<string>__NX_BIN__</string>" not in body
        # Activation command captured for the caller / banner.
        assert result.activated_cmd is not None
        assert result.activated_cmd[0] == "launchctl"
        assert result.activated_cmd[1] == "bootstrap"
        assert result.activated_cmd[-1] == str(dest)

    def test_installed_plist_is_mode_0644(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            result = installer.install_autostart()
        assert (result.dest.stat().st_mode & 0o777) == 0o644


class TestInstallFreshLinux:
    def test_install_writes_unit_and_calls_systemctl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "linux")
        _stub_paths(tmp_path, monkeypatch)

        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            result = installer.install_autostart()

        dest = tmp_path / "units" / "nexus-t2.service"
        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert dest.exists()
        body = dest.read_text()
        assert "/opt/conexus/bin/nx daemon t2 start" in body
        assert "ExecStart=__NX_BIN__" not in body
        assert result.activated_cmd == [
            "systemctl", "--user", "enable", "--now", "nexus-t2.service",
        ]


class TestInstallIdempotent:
    def test_reinstall_identical_content_reports_already_present_no_activation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            installer.install_autostart()

        # Second call: content matches the freshly rendered template, so
        # no write and no activation shell-out happens.
        with patch.object(daemon_cmd.subprocess, "run") as mock_run2:
            result = installer.install_autostart()
        assert result.status is installer.InstallStatus.ALREADY_PRESENT
        assert mock_run2.call_count == 0
        assert result.activated_cmd is None


class TestSymlinkGuard:
    def test_install_raises_on_symlink_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        (tmp_path / "units").mkdir()
        real = tmp_path / "real-file"
        real.write_text("<!-- real -->\n")
        link = tmp_path / "units" / "com.nexus.t2.plist"
        link.symlink_to(real)

        with pytest.raises(installer.SymlinkRefusedError):
            installer.install_autostart()
        # The symlink target is left untouched.
        assert real.read_text() == "<!-- real -->\n"


class TestContentDiffGuard:
    def test_install_raises_when_content_differs_without_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        (tmp_path / "units").mkdir()
        dest = tmp_path / "units" / "com.nexus.t2.plist"
        dest.write_text("<!-- operator customisation -->\n")

        with pytest.raises(installer.ContentDiffersError):
            installer.install_autostart()
        assert dest.read_text() == "<!-- operator customisation -->\n"

    def test_force_overwrites_differing_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        (tmp_path / "units").mkdir()
        dest = tmp_path / "units" / "com.nexus.t2.plist"
        dest.write_text("<!-- old -->\n")

        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            result = installer.install_autostart(force=True)

        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert "<!-- old -->" not in dest.read_text()
        assert "<string>/opt/conexus/bin/nx</string>" in dest.read_text()


class TestActivationFailure:
    def test_activation_failure_without_force_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)

        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "boom"
            mock_run.return_value.stdout = ""
            with pytest.raises(installer.ActivationError):
                installer.install_autostart()
        # The file was written before activation was attempted.
        assert (tmp_path / "units" / "com.nexus.t2.plist").exists()

    def test_activation_failure_with_force_returns_newly_installed_with_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)

        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "boom"
            mock_run.return_value.stdout = ""
            result = installer.install_autostart(force=True)

        # Under --force, activation failure is downgraded to a warning:
        # the file is installed, the result reports it, no raise.
        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert result.warnings
        assert any("boom" in w for w in result.warnings)


class TestUninstall:
    def test_uninstall_removes_unit_and_calls_bootout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            installer.install_autostart()
        dest = tmp_path / "units" / "com.nexus.t2.plist"
        assert dest.exists()

        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            result = installer.uninstall_autostart()

        assert result.status is installer.UninstallStatus.REMOVED
        assert result.dest == dest
        assert not dest.exists()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "launchctl" and cmd[1] == "bootout"
        assert "com.nexus.t2" in cmd[2]
        assert "com.nexus.t3" not in cmd[2]

    def test_uninstall_when_missing_reports_not_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        result = installer.uninstall_autostart()
        assert result.status is installer.UninstallStatus.NOT_INSTALLED
        assert not result.dest.exists()

    def test_uninstall_proceeds_with_warning_when_bootout_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            installer.install_autostart()
        dest = tmp_path / "units" / "com.nexus.t2.plist"

        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "bootout failed"
            mock_run.return_value.stdout = ""
            result = installer.uninstall_autostart()

        # bootout failure must NOT block file removal (mirrors the CLI).
        assert result.status is installer.UninstallStatus.REMOVED
        assert not dest.exists()
        assert result.warnings


class TestLinuxUninstall:
    def test_uninstall_calls_systemctl_disable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "linux")
        _stub_paths(tmp_path, monkeypatch)
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            installer.install_autostart()

        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            result = installer.uninstall_autostart()

        assert result.status is installer.UninstallStatus.REMOVED
        cmd = mock_run.call_args[0][0]
        assert cmd == ["systemctl", "--user", "disable", "--now", "nexus-t2.service"]
