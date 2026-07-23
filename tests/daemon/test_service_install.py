# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-174 P2.1 (nexus-y2yj6): autostart install for the storage *service* tier.

The service that serves every tier (T2 + T3 via the RDR-152 Java engine +
local Postgres) previously had start / install-binary / stop / status but NO
``install --autostart`` — so it had no reboot-persistence. P2.1 adds it on the
RDR-126 installer substrate (``nexus.daemon.installer``), mirroring the T2 path
(NOT the T3 inline command pattern), so it is an in-process callable with the
same structured ``InstallResult`` contract.

The unit execs ``nx daemon service start --foreground`` (run_storage_supervisor,
blocks until SIGTERM). PG boot-ordering + supervisor-handoff deltas are deferred
to P2.2 / P2.3; this lands the plain unit + command + render path.

Mirrors ``test_installer_lift.py`` + ``test_t2_install_cli.py``. ``launchctl`` /
``systemctl`` shell-out is mocked; template substitution + file placement run
for real.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.commands import daemon as daemon_cmd
from nexus.daemon import installer


def _set_platform(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: platform)


def _stub_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_cmd, "_autostart_install_dir", lambda: tmp_path / "units")
    monkeypatch.setattr(daemon_cmd, "_autostart_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(daemon_cmd, "_resolve_nx_bin", lambda: ["/opt/conexus/bin/nx"])


# ── library: install_autostart(tier="service") ────────────────────────────────


class TestServiceConstantsAndFilename:
    def test_service_constants(self) -> None:
        assert daemon_cmd._SERVICE_PLIST_NAME == "com.nexus.service.plist"
        assert daemon_cmd._SERVICE_SERVICE_NAME == "nexus-service.service"
        assert daemon_cmd._SERVICE_LAUNCHD_LABEL == "com.nexus.service"

    def test_filename_service_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_platform(monkeypatch, "darwin")
        assert daemon_cmd._autostart_filename_service() == "com.nexus.service.plist"

    def test_filename_service_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_platform(monkeypatch, "linux")
        assert daemon_cmd._autostart_filename_service() == "nexus-service.service"


class TestRenderForService:
    def test_render_execs_service_start_foreground_macos(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        dest, body = installer._render_for_service()
        assert dest == tmp_path / "units" / "com.nexus.service.plist"
        # ProgramArguments collapse to: nx daemon service start --foreground
        assert "<string>/opt/conexus/bin/nx</string>" in body
        assert "<string>service</string>" in body
        assert "<string>start</string>" in body
        assert "<string>--foreground</string>" in body
        # the placeholder string element is substituted (the bare token survives
        # in the template's prose comment, as it does for T2 — assert the wrapped
        # form is gone, matching test_installer_lift).
        assert "<string>__NX_BIN__</string>" not in body
        assert "com.nexus.service" in body

    def test_render_execs_service_start_foreground_linux(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "linux")
        _stub_paths(tmp_path, monkeypatch)
        dest, body = installer._render_for_service()
        assert dest == tmp_path / "units" / "nexus-service.service"
        assert "ExecStart=/opt/conexus/bin/nx daemon service start --foreground" in body
        # ExecStart placeholder substituted (bare token survives in the comment).
        assert "ExecStart=__NX_BIN__" not in body

    def test_service_unit_ships_no_postgresql_ordering(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RDR-174 P2.2 (nexus-exfns): the supervisor self-starts its own
        nx-owned PG, so the unit must NOT order against an external
        postgresql.service. Assert no ACTIVE directive references it (the prose
        comment explaining why is allowed to mention the string)."""
        _set_platform(monkeypatch, "linux")
        _stub_paths(tmp_path, monkeypatch)
        _dest, body = installer._render_for_service()
        active = [
            ln for ln in body.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        offenders = [ln for ln in active if "postgresql.service" in ln]
        assert not offenders, (
            f"unit must not order against external postgresql.service; got {offenders}"
        )

    def test_service_unit_never_gives_up_and_keeps_graceful_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RDR-175 P1 Step 2 (Gap 4): with the in-process respawn retired, OS
        init is the single watchdog. The systemd unit must never enter 'failed'
        after a restart burst (default StartLimitIntervalSec=10s/Burst=5 gives
        up where launchd KeepAlive+ThrottleInterval=30 does not), so
        StartLimitIntervalSec=0 (never-give-up parity). The edit must NOT drop
        the existing SuccessExitStatus=143 graceful-SIGTERM-stop directive."""
        _set_platform(monkeypatch, "linux")
        _stub_paths(tmp_path, monkeypatch)
        _dest, body = installer._render_for_service()
        active = [
            ln.strip() for ln in body.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        assert "StartLimitIntervalSec=0" in active, (
            "systemd unit must set StartLimitIntervalSec=0 for never-give-up "
            f"parity with launchd; active directives: {active}"
        )
        assert "SuccessExitStatus=143" in active, (
            "the StartLimitIntervalSec edit must not drop the graceful-stop "
            f"SuccessExitStatus=143 directive; active directives: {active}"
        )


class TestServicePlistThrottle:
    def test_plist_has_throttle_interval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SIG-1: KeepAlive=<true/> with no ThrottleInterval is an unthrottled
        crash loop when the supervisor can't start (no pg_credentials / binary
        yet — the normal state right after install, before `nx init --service`).
        The plist must throttle restarts."""
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        _dest, body = installer._render_for_service()
        assert "<key>ThrottleInterval</key>" in body
        assert "<key>KeepAlive</key>" in body


class TestServiceDeactivateLabel:
    def test_deactivate_uses_service_label_macos(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H-1 regression: the launchd bootout for the service tier must target
        com.nexus.service, NOT the hardcoded T2 label (which would no-op or, worse,
        boot out the T2 unit)."""
        _set_platform(monkeypatch, "darwin")
        cmd = installer._deactivate_cmd(Path("/x/com.nexus.service.plist"), tier="service")
        assert cmd[0] == "launchctl" and cmd[1] == "bootout"
        assert cmd[-1].endswith("/com.nexus.service")

    def test_deactivate_default_tier_is_t2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_platform(monkeypatch, "darwin")
        cmd = installer._deactivate_cmd(Path("/x/com.nexus.t2.plist"))
        assert cmd[-1].endswith("/com.nexus.t2")


class TestUninstallService:
    def test_uninstall_removes_service_unit_macos(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            installer.install_autostart(tier="service")
            result = installer.uninstall_autostart(tier="service")
        dest = tmp_path / "units" / "com.nexus.service.plist"
        assert result.status is installer.UninstallStatus.REMOVED
        assert result.dest == dest
        assert not dest.exists()

    def test_uninstall_not_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        result = installer.uninstall_autostart(tier="service")
        assert result.status is installer.UninstallStatus.NOT_INSTALLED

    def test_uninstall_default_tier_is_t2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Back-compat: daemon_uninstall calls uninstall_autostart() with no tier
        and must still target the T2 unit."""
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        result = installer.uninstall_autostart()
        assert result.dest == tmp_path / "units" / "com.nexus.t2.plist"


class TestServiceUninstallCli:
    def test_uninstall_cli_removes_unit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        runner = CliRunner()
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            runner.invoke(daemon_cmd.daemon_group, ["service", "install", "--autostart"])
            result = runner.invoke(
                daemon_cmd.daemon_group, ["service", "uninstall", "--autostart"]
            )
        dest = tmp_path / "units" / "com.nexus.service.plist"
        assert result.exit_code == 0, result.output
        assert f"Removed {dest}" in result.output
        assert not dest.exists()

    def test_uninstall_cli_not_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        result = CliRunner().invoke(
            daemon_cmd.daemon_group, ["service", "uninstall", "--autostart"]
        )
        assert result.exit_code == 0, result.output
        assert "not installed" in result.output.lower()


class TestInstallServiceLibrary:
    def test_install_writes_plist_and_activates_macos(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            result = installer.install_autostart(tier="service")
        dest = tmp_path / "units" / "com.nexus.service.plist"
        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert result.dest == dest
        assert dest.exists()
        assert result.activated_cmd is not None
        assert result.activated_cmd[0] == "launchctl"
        assert result.activated_cmd[-1] == str(dest)

    def test_default_tier_is_still_t2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Back-compat: existing callers invoke install_autostart() with no tier
        and must still target T2 (first-run + daemon_uninstall depend on this)."""
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            result = installer.install_autostart()
        assert result.dest == tmp_path / "units" / "com.nexus.t2.plist"

    def test_idempotent_reinstall_reports_already_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            installer.install_autostart(tier="service")
            result = installer.install_autostart(tier="service")
        assert result.status is installer.InstallStatus.ALREADY_PRESENT


# ── CLI: nx daemon service install --autostart ────────────────────────────────


class TestServiceInstallCli:
    def test_fresh_install_reports_wrote_and_activated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            result = CliRunner().invoke(
                daemon_cmd.daemon_group, ["service", "install", "--autostart"]
            )
        assert result.exit_code == 0, result.output
        dest = tmp_path / "units" / "com.nexus.service.plist"
        assert f"Wrote {dest}" in result.output
        assert "Activated via:" in result.output
        assert dest.exists()

    def test_idempotent_reinstall_reports_already_up_to_date(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        runner = CliRunner()
        with patch.object(daemon_cmd.subprocess, "run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            runner.invoke(daemon_cmd.daemon_group, ["service", "install", "--autostart"])
            result = runner.invoke(
                daemon_cmd.daemon_group, ["service", "install", "--autostart"]
            )
        assert result.exit_code == 0, result.output
        assert "already up to date; no changes" in result.output


class TestServicePlistRespawnPosture:
    """nexus-6bmph (RDR-183 defect-3): the launchd unit restarts on FAILURE
    only — SuccessfulExit=false gives parity with the systemd unit's
    Restart=on-failure. A bare KeepAlive=<true/> respawned exit-0 (healthy
    coexistence; graceful stop) every ThrottleInterval forever."""

    def test_keepalive_is_successful_exit_false(self):
        import plistlib
        import re
        from pathlib import Path

        template = (
            Path(__file__).resolve().parents[2]
            / "conexus" / "daemon" / "com.nexus.service.plist"
        )
        # The template's prose comments legitimately contain `--` (CLI flags),
        # which strict XML parsers reject inside comments (launchd is lenient).
        # Strip comments before parsing the real structure.
        raw = re.sub(rb"<!--.*?-->", b"", template.read_bytes(), flags=re.S)
        data = plistlib.loads(raw)
        ka = data["KeepAlive"]
        assert isinstance(ka, dict), (
            "KeepAlive must be the SuccessfulExit dict form — a bare <true/> "
            "respawns successful exits (GH #1405 defect-3 steady-state churn)"
        )
        assert ka == {"SuccessfulExit": False}
        assert data["ThrottleInterval"] == 30
        assert data["RunAtLoad"] is True
