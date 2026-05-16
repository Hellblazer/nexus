"""Tests for ``nx daemon t2 install/uninstall --autostart`` (RDR-112, nexus-6w0c).

Templates ship under ``src/nexus/_resources/daemon/`` (symlinked to
``nx/daemon/``). The CLI substitutes ``__NX_BIN__``, ``__LOG_DIR__``,
and ``__PATH_ENV__`` at install time and drops the rendered file into
the per-OS autostart location.

The shell-out to ``launchctl`` / ``systemctl`` is mocked; the template
substitution and file placement are exercised for real.
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


def test_template_files_ship_in_resources() -> None:
    """Both template files resolve via importlib.resources."""
    plist = daemon_cmd._read_template("com.nexus.t2.plist")
    service = daemon_cmd._read_template("nexus-t2.service")
    assert "__NX_BIN__" in plist
    assert "com.nexus.t2" in plist
    assert "RunAtLoad" in plist
    assert "__NX_BIN__" in service
    assert "WantedBy=default.target" in service
    assert "Restart=on-failure" in service


def test_render_template_substitutes_placeholders(tmp_path: Path) -> None:
    rendered = daemon_cmd._render_template(
        "com.nexus.t2.plist",
        nx_bin="/opt/nx/bin/nx",
        log_dir=str(tmp_path / "logs"),
        path_env="/usr/local/bin:/usr/bin",
    )
    assert "__NX_BIN__" not in rendered
    assert "__LOG_DIR__" not in rendered
    assert "__PATH_ENV__" not in rendered
    assert "/opt/nx/bin/nx" in rendered
    assert str(tmp_path / "logs") in rendered


def test_resolve_nx_bin_uses_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_cmd.shutil, "which", lambda name: "/opt/uv/bin/nx")
    assert daemon_cmd._resolve_nx_bin() == "/opt/uv/bin/nx"


def test_resolve_nx_bin_falls_back_to_python_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon_cmd.shutil, "which", lambda name: None)
    resolved = daemon_cmd._resolve_nx_bin()
    # Falls back to ``<python> -m nexus.cli``; both tokens present.
    assert sys.executable in resolved
    assert "nexus.cli" in resolved


def test_install_autostart_macos_writes_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_platform(monkeypatch, "darwin")
    monkeypatch.setattr(daemon_cmd, "_autostart_install_dir", lambda: tmp_path / "LaunchAgents")
    monkeypatch.setattr(daemon_cmd, "_autostart_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(daemon_cmd, "_resolve_nx_bin", lambda: "/opt/nx/bin/nx")

    with patch.object(daemon_cmd.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        runner = CliRunner()
        result = runner.invoke(
            daemon_cmd.daemon_group, ["t2", "install", "--autostart"]
        )

    assert result.exit_code == 0, result.output
    plist_path = tmp_path / "LaunchAgents" / "com.nexus.t2.plist"
    assert plist_path.exists()
    body = plist_path.read_text()
    assert "/opt/nx/bin/nx" in body
    assert "__NX_BIN__" not in body
    # File permissions: 0o644 (world-readable, owner-writable).
    mode = plist_path.stat().st_mode & 0o777
    assert mode == 0o644
    # launchctl bootstrap was invoked.
    assert mock_run.called
    args = mock_run.call_args[0][0]
    assert "launchctl" in args[0]


def test_install_autostart_linux_writes_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_platform(monkeypatch, "linux")
    monkeypatch.setattr(daemon_cmd, "_autostart_install_dir", lambda: tmp_path / "systemd")
    monkeypatch.setattr(daemon_cmd, "_autostart_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(daemon_cmd, "_resolve_nx_bin", lambda: "/opt/nx/bin/nx")

    with patch.object(daemon_cmd.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        runner = CliRunner()
        result = runner.invoke(
            daemon_cmd.daemon_group, ["t2", "install", "--autostart"]
        )

    assert result.exit_code == 0, result.output
    unit_path = tmp_path / "systemd" / "nexus-t2.service"
    assert unit_path.exists()
    body = unit_path.read_text()
    assert "ExecStart=/opt/nx/bin/nx daemon t2 start --foreground" in body
    assert "__NX_BIN__" not in body
    assert mock_run.called
    args = mock_run.call_args[0][0]
    assert "systemctl" in args[0]


def test_install_autostart_unsupported_platform_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_platform(monkeypatch, "win32")
    runner = CliRunner()
    result = runner.invoke(daemon_cmd.daemon_group, ["t2", "install", "--autostart"])
    assert result.exit_code == 1
    assert "unsupported" in result.output.lower() or "not supported" in result.output.lower()


def test_uninstall_autostart_macos_removes_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_platform(monkeypatch, "darwin")
    install_dir = tmp_path / "LaunchAgents"
    install_dir.mkdir()
    plist = install_dir / "com.nexus.t2.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(daemon_cmd, "_autostart_install_dir", lambda: install_dir)

    with patch.object(daemon_cmd.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        runner = CliRunner()
        result = runner.invoke(
            daemon_cmd.daemon_group, ["t2", "uninstall", "--autostart"]
        )

    assert result.exit_code == 0, result.output
    assert not plist.exists()
    assert mock_run.called


def test_uninstall_autostart_linux_removes_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_platform(monkeypatch, "linux")
    install_dir = tmp_path / "systemd"
    install_dir.mkdir()
    unit = install_dir / "nexus-t2.service"
    unit.write_text("[Service]")
    monkeypatch.setattr(daemon_cmd, "_autostart_install_dir", lambda: install_dir)

    with patch.object(daemon_cmd.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        runner = CliRunner()
        result = runner.invoke(
            daemon_cmd.daemon_group, ["t2", "uninstall", "--autostart"]
        )

    assert result.exit_code == 0, result.output
    assert not unit.exists()


def test_uninstall_autostart_missing_file_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_platform(monkeypatch, "darwin")
    monkeypatch.setattr(daemon_cmd, "_autostart_install_dir", lambda: tmp_path)
    with patch.object(daemon_cmd.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        runner = CliRunner()
        result = runner.invoke(
            daemon_cmd.daemon_group, ["t2", "uninstall", "--autostart"]
        )
    assert result.exit_code == 0
    assert "not installed" in result.output.lower() or "nothing" in result.output.lower()


def test_install_without_autostart_flag_errors() -> None:
    """``install`` without ``--autostart`` is the only mode today; missing flag is rejected."""
    runner = CliRunner()
    result = runner.invoke(daemon_cmd.daemon_group, ["t2", "install"])
    assert result.exit_code != 0
