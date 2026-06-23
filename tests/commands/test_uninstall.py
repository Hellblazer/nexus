# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-165 Phase 3 (nexus-eu4u4) — the first-class `nx uninstall` command.

The CLI surface over `installer.uninstall_daemon` (the complete local teardown:
engine-service + PG + T2 daemon + autostart + marker + optional data wipe).
Dry-run is the DEFAULT (mirrors the daemon_uninstall MCP tool's confirm=false);
`--yes` confirms. `--remove-data` is gated and only meaningful with `--yes`.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


def _report(**kw):
    from nexus.daemon.installer import DaemonUninstallReport, UninstallStatus

    defaults = dict(
        confirmed=True,
        unit_status=UninstallStatus.REMOVED,
        unit_dest="/x/unit",
        marker_removed=True,
        data_removed=False,
        data_dir="/x/cfg",
        daemon_stopped=True,
        service_stopped=True,
        warnings=(),
        message="Daemon uninstall complete: service stack stopped; daemon stopped.",
    )
    defaults.update(kw)
    return DaemonUninstallReport(**defaults)


class TestUninstallCommand:
    @pytest.fixture(autouse=True)
    def _local_present(self):
        # These exercise the LOCAL branch — pin local presence True.
        with patch("nexus.commands.uninstall._local_service_present", return_value=True):
            yield

    def test_dry_run_is_default_no_yes(self) -> None:
        """No --yes → confirm=False (preview only), nothing is torn down."""
        with patch("nexus.commands.uninstall.uninstall_daemon") as m:
            m.return_value = _report(confirmed=False, message="This would remove: ...")
            res = CliRunner().invoke(main, ["uninstall"])
        assert res.exit_code == 0, res.output
        assert m.call_count == 1
        _, kw = m.call_args
        assert kw.get("confirm") is False
        assert "would remove" in res.output.lower()

    def test_yes_confirms_teardown(self) -> None:
        with patch("nexus.commands.uninstall.uninstall_daemon") as m:
            m.return_value = _report()
            res = CliRunner().invoke(main, ["uninstall", "--yes"])
        assert res.exit_code == 0, res.output
        _, kw = m.call_args
        assert kw.get("confirm") is True
        assert kw.get("remove_data") is False

    def test_remove_data_flag_threads_through_with_yes(self) -> None:
        with patch("nexus.commands.uninstall.uninstall_daemon") as m:
            m.return_value = _report(data_removed=True)
            res = CliRunner().invoke(main, ["uninstall", "--yes", "--remove-data"])
        assert res.exit_code == 0, res.output
        _, kw = m.call_args
        assert kw.get("confirm") is True
        assert kw.get("remove_data") is True

    def test_warnings_surfaced(self) -> None:
        with patch("nexus.commands.uninstall.uninstall_daemon") as m:
            m.return_value = _report(warnings=("service stop exited 1: not running",))
            res = CliRunner().invoke(main, ["uninstall", "--yes"])
        assert res.exit_code == 0, res.output
        assert "not running" in res.output


class TestManagedBranch:
    """wigzi: the managed-only teardown — clear service_url/token from config.yml,
    warn on a shell-env override, never stop a (nonexistent) local service or
    touch the remote tenant's data."""

    @pytest.fixture(autouse=True)
    def _no_local(self):
        # Managed-only persona: NO local service present. The local branch must
        # be skipped entirely (Sig-1: no spurious noise; Sig-2: no stop subprocess).
        with patch("nexus.commands.uninstall._local_service_present", return_value=False):
            yield

    def test_managed_config_cleared_with_yes(self, monkeypatch) -> None:
        for k in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        cleared: list[str] = []
        with patch("nexus.commands.uninstall.uninstall_daemon") as m_local, \
             patch("nexus.commands.uninstall.get_credential",
                   side_effect=lambda n: "https://api.conexus-nexus.com" if n == "service_url" else "tok"), \
             patch("nexus.commands.uninstall.unset_credential",
                   side_effect=lambda n: cleared.append(n) or True):
            res = CliRunner().invoke(main, ["uninstall", "--yes"])
        assert res.exit_code == 0, res.output
        assert cleared == ["service_url", "service_token"]
        assert "managed" in res.output.lower()
        # Sig-1/Sig-2: managed-only → the local teardown is NEVER invoked.
        assert m_local.call_count == 0
        assert "not running" not in res.output

    def test_managed_dry_run_does_not_clear(self, monkeypatch) -> None:
        for k in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        cleared: list[str] = []
        with patch("nexus.commands.uninstall.uninstall_daemon") as m_local, \
             patch("nexus.commands.uninstall.get_credential",
                   side_effect=lambda n: "https://api.conexus-nexus.com" if n == "service_url" else "tok"), \
             patch("nexus.commands.uninstall.unset_credential",
                   side_effect=lambda n: cleared.append(n) or True):
            res = CliRunner().invoke(main, ["uninstall"])
        assert res.exit_code == 0, res.output
        assert cleared == []  # dry run touches nothing
        assert m_local.call_count == 0
        assert "managed" in res.output.lower()

    def test_no_managed_no_local_reports_nothing_to_uninstall(self, monkeypatch) -> None:
        for k in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        cleared: list[str] = []
        with patch("nexus.commands.uninstall.uninstall_daemon") as m_local, \
             patch("nexus.commands.uninstall.get_credential", return_value=""), \
             patch("nexus.commands.uninstall.unset_credential",
                   side_effect=lambda n: cleared.append(n) or True):
            res = CliRunner().invoke(main, ["uninstall", "--yes"])
        assert res.exit_code == 0, res.output
        assert cleared == []
        assert m_local.call_count == 0
        assert "nothing to uninstall" in res.output.lower()

    def test_env_override_warns_cannot_unset_shell(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        with patch("nexus.commands.uninstall.uninstall_daemon"), \
             patch("nexus.commands.uninstall.get_credential",
                   side_effect=lambda n: "https://api.conexus-nexus.com" if n == "service_url" else "tok"), \
             patch("nexus.commands.uninstall.unset_credential", return_value=False):
            res = CliRunner().invoke(main, ["uninstall", "--yes"])
        assert res.exit_code == 0, res.output
        out = res.output.lower()
        assert "nx_service_url" in out and ("unset" in out or "export" in out)
