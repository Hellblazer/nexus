# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-126 §4 (nexus-xxe66): ``daemon_uninstall`` orchestration contract.

The ``daemon_uninstall`` MCP tool (exposed on ``nx-mcp`` only) wraps a
library function ``nexus.daemon.installer.uninstall_daemon`` so the
destructive logic is testable without MCP. Semantics:

- ``confirm=False`` (default): a dry run. Describe what WOULD be removed,
  touch nothing. Matches MCP destructive-op convention (the model must
  re-call with ``confirm=True``).
- ``confirm=True``: remove the OS autostart unit (via
  ``uninstall_autostart``), stop the daemon (best-effort), remove the
  first-run marker.
- ``remove_data=True`` (with confirm): additionally wipe the nexus config
  / data directory.

``launchctl`` / ``systemctl`` / ``nx daemon t2 stop`` shell-out is mocked;
file placement / removal is exercised for real under a tmp config dir.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.commands import daemon as daemon_cmd
from nexus.daemon import installer
from nexus.mcp import _first_run


def _set_platform(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: platform)


@pytest.fixture
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated config dir + stubbed autostart paths + an installed unit."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    _set_platform(monkeypatch, "darwin")
    monkeypatch.setattr(
        daemon_cmd, "_autostart_install_dir", lambda: tmp_path / "units"
    )
    monkeypatch.setattr(daemon_cmd, "_autostart_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(daemon_cmd, "_resolve_nx_bin", lambda: ["/opt/conexus/bin/nx"])
    # Install a unit + write the marker so there is something to remove.
    with patch.object(daemon_cmd.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
        mock_run.return_value.stdout = ""
        installer.install_autostart()
    _first_run.mark_shown()
    return tmp_path


class TestDryRun:
    def test_confirm_false_touches_nothing(self, _env: Path) -> None:
        unit = _env / "units" / "com.nexus.t2.plist"
        marker = _first_run._first_run_marker_path()
        assert unit.exists() and marker.exists()

        with patch.object(installer.subprocess, "run") as mock_run:
            report = installer.uninstall_daemon(confirm=False)

        assert report.confirmed is False
        # No side effects at all on a dry run.
        assert mock_run.call_count == 0
        assert unit.exists()
        assert marker.exists()
        # The message names what would be removed.
        assert "com.nexus.t2.plist" in report.message
        assert "confirm" in report.message.lower()


class TestConfirmedUninstall:
    def test_removes_unit_marker_and_stops_daemon(self, _env: Path) -> None:
        unit = _env / "units" / "com.nexus.t2.plist"
        marker = _first_run._first_run_marker_path()
        config_dir = _env / "cfg"

        with patch.object(installer.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            report = installer.uninstall_daemon(confirm=True)

        assert report.confirmed is True
        assert report.unit_status is installer.UninstallStatus.REMOVED
        assert not unit.exists()
        assert report.marker_removed is True
        assert not marker.exists()
        assert report.daemon_stopped is True
        # Data dir preserved when remove_data is False.
        assert report.data_removed is False
        assert config_dir.exists()
        # A daemon-stop command was issued (exact argv, not substring).
        stop_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert ["/opt/conexus/bin/nx", "daemon", "t2", "stop"] in stop_cmds

    def test_remove_data_wipes_config_dir(self, _env: Path) -> None:
        config_dir = _env / "cfg"
        assert config_dir.exists()

        with patch.object(installer.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            report = installer.uninstall_daemon(confirm=True, remove_data=True)

        assert report.confirmed is True
        assert report.data_removed is True
        assert not config_dir.exists()

    def test_remove_data_refuses_shallow_config_dir(
        self, _env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A misconfigured NEXUS_CONFIG_DIR pointing at a shallow path
        (review H2) must NOT be rmtree'd even with confirm+remove_data."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", "/Users")
        with patch.object(installer.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            report = installer.uninstall_daemon(confirm=True, remove_data=True)
        assert report.data_removed is False
        assert any("refusing to remove" in w for w in report.warnings)
        assert Path("/Users").exists()

    def test_confirmed_when_unit_already_absent_is_graceful(
        self, _env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Remove the unit out from under it first.
        (_env / "units" / "com.nexus.t2.plist").unlink()

        with patch.object(installer.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            report = installer.uninstall_daemon(confirm=True)

        assert report.confirmed is True
        assert report.unit_status is installer.UninstallStatus.NOT_INSTALLED
        # Marker is still cleaned up even when the unit was already gone.
        assert report.marker_removed is True


class TestMcpToolRegistration:
    def test_daemon_uninstall_registered_on_core_not_catalog(self) -> None:
        import asyncio

        from nexus.mcp import catalog, core

        core_tools = asyncio.run(core.mcp.list_tools())
        core_names = {t.name for t in core_tools}
        assert "daemon_uninstall" in core_names

        catalog_tools = asyncio.run(catalog.mcp.list_tools())
        catalog_names = {t.name for t in catalog_tools}
        assert "daemon_uninstall" not in catalog_names
