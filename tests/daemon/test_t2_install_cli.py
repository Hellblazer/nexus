# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-126 §2 (nexus-2yajx review follow-up): CLI parity for the lifted
``nx daemon t2 install/uninstall --autostart`` thin wrappers.

The install/uninstall logic was lifted into ``nexus.daemon.installer``
(see ``test_installer_lift.py`` for the library contract). The decision
memo requires the CLI to stay byte-identical to the pre-lift behavior;
these CliRunner tests lock the exit codes and stdout/stderr strings the
thin wrappers now produce by translating ``InstallResult`` /
``UninstallResult``. Mirrors ``test_t3_install.py``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.commands import daemon as daemon_cmd


def _set_platform(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: platform)


def _stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_platform(monkeypatch, "darwin")
    monkeypatch.setattr(daemon_cmd, "_autostart_install_dir", lambda: tmp_path / "units")
    monkeypatch.setattr(daemon_cmd, "_autostart_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(daemon_cmd, "_resolve_nx_bin", lambda: ["/opt/conexus/bin/nx"])


class TestInstallCli:
    def test_fresh_install_exits_zero_and_reports_wrote_and_activated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(tmp_path, monkeypatch)
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            result = CliRunner().invoke(
                daemon_cmd.daemon_group, ["t2", "install", "--autostart"]
            )
        assert result.exit_code == 0, result.output
        dest = tmp_path / "units" / "com.nexus.t2.plist"
        assert f"Wrote {dest}" in result.output
        assert "Activated via:" in result.output
        assert dest.exists()

    def test_idempotent_reinstall_reports_already_up_to_date(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(tmp_path, monkeypatch)
        runner = CliRunner()
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            runner.invoke(daemon_cmd.daemon_group, ["t2", "install", "--autostart"])
            result = runner.invoke(
                daemon_cmd.daemon_group, ["t2", "install", "--autostart"]
            )
        assert result.exit_code == 0, result.output
        assert "already up to date; no changes" in result.output

    def test_content_differs_without_force_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(tmp_path, monkeypatch)
        (tmp_path / "units").mkdir()
        dest = tmp_path / "units" / "com.nexus.t2.plist"
        dest.write_text("<!-- operator customisation -->\n")
        result = CliRunner().invoke(
            daemon_cmd.daemon_group, ["t2", "install", "--autostart"]
        )
        assert result.exit_code == 1
        assert "refusing to overwrite" in result.output
        assert dest.read_text() == "<!-- operator customisation -->\n"

    def test_symlink_target_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(tmp_path, monkeypatch)
        (tmp_path / "units").mkdir()
        real = tmp_path / "real"
        real.write_text("x\n")
        link = tmp_path / "units" / "com.nexus.t2.plist"
        link.symlink_to(real)
        result = CliRunner().invoke(
            daemon_cmd.daemon_group, ["t2", "install", "--autostart"]
        )
        assert result.exit_code == 1
        assert "symlink" in result.output.lower()

    def test_force_overwrites_and_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(tmp_path, monkeypatch)
        (tmp_path / "units").mkdir()
        dest = tmp_path / "units" / "com.nexus.t2.plist"
        dest.write_text("<!-- old -->\n")
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            result = CliRunner().invoke(
                daemon_cmd.daemon_group, ["t2", "install", "--autostart", "--force"]
            )
        assert result.exit_code == 0, result.output
        assert "<!-- old -->" not in dest.read_text()


    def test_force_activation_failure_warns_but_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under --force, a failed launchctl/systemctl activation is a
        warning, not an error: the file is written, 'Wrote' is printed,
        the warning goes to stderr, and the command exits 0."""
        _stub(tmp_path, monkeypatch)
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "boom"
            mock_run.return_value.stdout = ""
            result = CliRunner().invoke(
                daemon_cmd.daemon_group, ["t2", "install", "--autostart", "--force"]
            )
        assert result.exit_code == 0, result.output
        dest = tmp_path / "units" / "com.nexus.t2.plist"
        assert f"Wrote {dest}" in result.output
        assert "Warning:" in result.output
        assert "boom" in result.output
        assert dest.exists()


class TestUninstallCli:
    def test_uninstall_present_reports_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(tmp_path, monkeypatch)
        runner = CliRunner()
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            runner.invoke(daemon_cmd.daemon_group, ["t2", "install", "--autostart"])
            result = runner.invoke(
                daemon_cmd.daemon_group, ["t2", "uninstall", "--autostart"]
            )
        assert result.exit_code == 0, result.output
        dest = tmp_path / "units" / "com.nexus.t2.plist"
        assert f"Removed {dest}" in result.output
        assert not dest.exists()

    def test_uninstall_missing_reports_not_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(tmp_path, monkeypatch)
        result = CliRunner().invoke(
            daemon_cmd.daemon_group, ["t2", "uninstall", "--autostart"]
        )
        assert result.exit_code == 0
        assert "not installed" in result.output
