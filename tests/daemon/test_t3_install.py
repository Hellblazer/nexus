# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P1.5.4 (nexus-v5hb): ``nx daemon t3 install/uninstall --autostart`` tests.

Mirrors the T2 autostart tests at ``tests/commands/test_daemon_autostart.py``.
Templates ship under ``src/nexus/_resources/daemon/``; the CLI
substitutes ``__NX_BIN__`` / ``__LOG_DIR__`` / ``__PATH_ENV__`` at
install time and drops the rendered file into the per-OS autostart
location.

Shell-out to ``launchctl`` / ``systemctl`` is mocked; the template
substitution + file placement are exercised for real.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.commands import daemon as daemon_cmd


def _set_platform(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: platform)


# ---------------------------------------------------------------------------
# Templates ship in resources
# ---------------------------------------------------------------------------


class TestTemplatesShipped:
    def test_t3_plist_template_present_with_placeholders(self) -> None:
        body = daemon_cmd._read_template("com.nexus.t3.plist")
        assert "__NX_BIN__" in body
        assert "com.nexus.t3" in body
        assert "RunAtLoad" in body
        # T3 unit invokes the t3 subcommand, not t2.
        assert "<string>t3</string>" in body

    def test_t3_service_template_present_with_placeholders(self) -> None:
        body = daemon_cmd._read_template("nexus-t3.service")
        assert "__NX_BIN__" in body
        assert "WantedBy=default.target" in body
        assert "Restart=on-failure" in body
        # T3 unit invokes the t3 subcommand.
        assert "daemon t3 start" in body


# ---------------------------------------------------------------------------
# Filename helper
# ---------------------------------------------------------------------------


class TestAutostartFilenameT3:
    def test_macos_filename(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_platform(monkeypatch, "darwin")
        assert daemon_cmd._autostart_filename_t3() == "com.nexus.t3.plist"

    def test_linux_filename(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_platform(monkeypatch, "linux")
        assert daemon_cmd._autostart_filename_t3() == "nexus-t3.service"


# ---------------------------------------------------------------------------
# install + uninstall flow (macOS)
# ---------------------------------------------------------------------------


class TestInstallMacOS:
    def test_install_writes_plist_and_calls_launchctl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        monkeypatch.setattr(
            daemon_cmd, "_autostart_install_dir",
            lambda: tmp_path / "LaunchAgents",
        )
        monkeypatch.setattr(
            daemon_cmd, "_autostart_log_dir", lambda: tmp_path / "Logs",
        )
        monkeypatch.setattr(daemon_cmd, "_resolve_nx_bin", lambda: ["/opt/nx/bin/nx"])

        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            runner = CliRunner()
            result = runner.invoke(
                daemon_cmd.daemon_group,
                ["t3", "install", "--autostart"],
            )

        assert result.exit_code == 0, result.output
        dest = tmp_path / "LaunchAgents" / "com.nexus.t3.plist"
        assert dest.exists()
        body = dest.read_text()
        assert "<string>/opt/nx/bin/nx</string>" in body
        assert "<string>t3</string>" in body
        # ProgramArguments slot substituted (the comment may legitimately
        # mention __NX_BIN__ in prose; test the substantive slot).
        assert "<string>__NX_BIN__</string>" not in body
        # launchctl bootstrap was invoked
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "launchctl" and cmd[1] == "bootstrap"
        assert cmd[-1] == str(dest)

    def test_uninstall_removes_plist_and_calls_launchctl_bootout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        monkeypatch.setattr(
            daemon_cmd, "_autostart_install_dir",
            lambda: tmp_path / "LaunchAgents",
        )
        monkeypatch.setattr(
            daemon_cmd, "_autostart_log_dir", lambda: tmp_path / "Logs",
        )
        monkeypatch.setattr(daemon_cmd, "_resolve_nx_bin", lambda: ["/opt/nx/bin/nx"])

        # First install so there is something to uninstall.
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            runner = CliRunner()
            runner.invoke(daemon_cmd.daemon_group, ["t3", "install", "--autostart"])
        dest = tmp_path / "LaunchAgents" / "com.nexus.t3.plist"
        assert dest.exists()

        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            result = runner.invoke(
                daemon_cmd.daemon_group, ["t3", "uninstall", "--autostart"]
            )
        assert result.exit_code == 0, result.output
        assert not dest.exists()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "launchctl" and cmd[1] == "bootout"
        # Label MUST be the T3 label, not the T2 one.
        assert "com.nexus.t3" in cmd[2]
        assert "com.nexus.t2" not in cmd[2]

    def test_uninstall_when_missing_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        monkeypatch.setattr(
            daemon_cmd, "_autostart_install_dir",
            lambda: tmp_path / "LaunchAgents",
        )

        runner = CliRunner()
        result = runner.invoke(
            daemon_cmd.daemon_group, ["t3", "uninstall", "--autostart"]
        )
        assert result.exit_code == 0
        assert "not installed" in result.output


# ---------------------------------------------------------------------------
# install flow (Linux)
# ---------------------------------------------------------------------------


class TestInstallLinux:
    def test_install_writes_unit_and_calls_systemctl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "linux")
        monkeypatch.setattr(
            daemon_cmd, "_autostart_install_dir",
            lambda: tmp_path / "systemd-user",
        )
        monkeypatch.setattr(
            daemon_cmd, "_autostart_log_dir",
            lambda: tmp_path / "state",
        )
        monkeypatch.setattr(daemon_cmd, "_resolve_nx_bin", lambda: ["/opt/nx/bin/nx"])

        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            runner = CliRunner()
            result = runner.invoke(
                daemon_cmd.daemon_group, ["t3", "install", "--autostart"]
            )

        assert result.exit_code == 0, result.output
        dest = tmp_path / "systemd-user" / "nexus-t3.service"
        assert dest.exists()
        body = dest.read_text()
        # ExecStart points at the t3 subcommand
        assert "/opt/nx/bin/nx daemon t3 start" in body
        # Substantive ExecStart slot substituted (comment lines may
        # legitimately mention __NX_BIN__ in prose).
        assert "ExecStart=__NX_BIN__" not in body
        # systemctl --user enable --now nexus-t3.service was invoked
        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "systemctl", "--user", "enable", "--now", "nexus-t3.service",
        ]


# ---------------------------------------------------------------------------
# Overwrite guard + --force semantics
# ---------------------------------------------------------------------------


class TestOverwriteGuard:
    def test_install_refuses_when_file_differs_without_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        monkeypatch.setattr(
            daemon_cmd, "_autostart_install_dir",
            lambda: tmp_path / "LaunchAgents",
        )
        monkeypatch.setattr(
            daemon_cmd, "_autostart_log_dir", lambda: tmp_path / "Logs",
        )
        (tmp_path / "LaunchAgents").mkdir()
        # Plant a customised file.
        dest = tmp_path / "LaunchAgents" / "com.nexus.t3.plist"
        dest.write_text("<!-- operator customisation -->\n")

        runner = CliRunner()
        result = runner.invoke(
            daemon_cmd.daemon_group, ["t3", "install", "--autostart"]
        )
        assert result.exit_code == 1
        assert "refusing to overwrite" in result.output
        # File preserved as the operator left it.
        assert dest.read_text() == "<!-- operator customisation -->\n"

    def test_install_overwrites_with_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        monkeypatch.setattr(
            daemon_cmd, "_autostart_install_dir",
            lambda: tmp_path / "LaunchAgents",
        )
        monkeypatch.setattr(
            daemon_cmd, "_autostart_log_dir", lambda: tmp_path / "Logs",
        )
        monkeypatch.setattr(daemon_cmd, "_resolve_nx_bin", lambda: ["/opt/nx/bin/nx"])
        (tmp_path / "LaunchAgents").mkdir()
        dest = tmp_path / "LaunchAgents" / "com.nexus.t3.plist"
        dest.write_text("<!-- old customisation -->\n")

        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            runner = CliRunner()
            result = runner.invoke(
                daemon_cmd.daemon_group,
                ["t3", "install", "--autostart", "--force"],
            )

        assert result.exit_code == 0, result.output
        # File replaced with the rendered template.
        assert "<string>/opt/nx/bin/nx</string>" in dest.read_text()
        assert "<!-- old customisation -->" not in dest.read_text()


# ---------------------------------------------------------------------------
# Group registration
# ---------------------------------------------------------------------------


class TestGroupRegistration:
    def test_t3_group_exposes_install_and_uninstall(self) -> None:
        t3 = daemon_cmd.daemon_group.commands["t3"]
        assert "install" in t3.commands
        assert "uninstall" in t3.commands
