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

import subprocess
import sys
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
        with patch.object(daemon_cmd.subprocess, "run") as mock_run, \
                patch.object(installer, "run_bounded", new=mock_run):
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
        """``daemon_uninstall`` and ``upgrade_finish`` call uninstall_autostart()
        with no tier and must still target the T2 unit.

        LOAD-BEARING AFTER THE DAEMON'S RETIREMENT (nexus-i711w Stage 2 sub-stage
        B), not back-compat bookkeeping: a box upgraded from a pre-retirement
        install still carries a launchd/systemd unit firing `nx daemon t2 start`.
        Removal machinery outlives what it removes. Drop this default and that
        unit fires a nonexistent command on every boot, forever."""
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
        with patch.object(daemon_cmd.subprocess, "run") as mock_run, \
                patch.object(installer, "run_bounded", new=mock_run):
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
        with patch.object(daemon_cmd.subprocess, "run") as mock_run, \
                patch.object(installer, "run_bounded", new=mock_run):
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

    def test_install_requires_an_explicit_tier(self) -> None:
        """``install_autostart`` has NO default tier (nexus-i711w Stage 2
        sub-stage B). It defaulted to "t2" while that daemon existed; with the
        daemon retired there is no t2 render arm, so a surviving default would
        be a ValueError trap for any unqualified caller. Pair this with
        ``test_uninstall_default_tier_is_t2``: the asymmetry (INSTALL dies,
        REMOVE survives) is the contract, and a symmetric change to either half
        breaks a real upgrade path."""
        with pytest.raises(TypeError, match="tier"):
            installer.install_autostart()  # type: ignore[call-arg]

    def test_idempotent_reinstall_reports_already_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        with patch.object(daemon_cmd.subprocess, "run") as mock_run, \
                patch.object(installer, "run_bounded", new=mock_run):
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
        with patch.object(daemon_cmd.subprocess, "run") as mock_run, \
                patch.object(installer, "run_bounded", new=mock_run):
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
        with patch.object(daemon_cmd.subprocess, "run") as mock_run, \
                patch.object(installer, "run_bounded", new=mock_run):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            runner.invoke(daemon_cmd.daemon_group, ["service", "install", "--autostart"])
            result = runner.invoke(
                daemon_cmd.daemon_group, ["service", "install", "--autostart"]
            )
        assert result.exit_code == 0, result.output
        # nexus-mac7t: the short-circuit now also confirms registration
        assert "already up to date and registered; no changes" in result.output


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


class TestGeneratedUnitCarriesExplicitConfigDir:
    """nexus-cd1k0.19 review round 2, finding 4: a unit generated FROM THIS
    POINT ON must carry an explicit --config-dir <resolved absolute path>
    in its argv, never rely on storage_service_stack_matcher's
    flagless-matches-default fallback — that fallback exists only for
    units installed BEFORE this fix. Closes the false-positive surface a
    live, env-scoped `nx daemon service start`/`stop` invocation
    (upgrade_finish.py's bare CLI calls; e2e sandbox scripts) otherwise
    has against the matcher's default-dir target."""

    def test_rendered_service_plist_carries_config_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import plistlib
        import re

        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        env_scoped_dir = tmp_path / "env-scoped-nexus-config"
        env_scoped_dir.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(env_scoped_dir))

        _dest, rendered = installer.rendered_unit_content(tier="service")
        raw = re.sub(rb"<!--.*?-->", b"", rendered.encode(), flags=re.S)
        data = plistlib.loads(raw)
        argv = data["ProgramArguments"]
        assert "--config-dir" in argv, argv
        idx = argv.index("--config-dir")
        assert argv[idx + 1] == str(env_scoped_dir.resolve()), argv

    def test_rendered_systemd_unit_carries_config_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_platform(monkeypatch, "linux")
        _stub_paths(tmp_path, monkeypatch)
        env_scoped_dir = tmp_path / "env-scoped-nexus-config"
        env_scoped_dir.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(env_scoped_dir))

        _dest, rendered = installer.rendered_unit_content(tier="service")
        exec_start = next(
            ln for ln in rendered.splitlines() if ln.startswith("ExecStart=")
        )
        assert "--config-dir" in exec_start, exec_start
        assert str(env_scoped_dir.resolve()) in exec_start, exec_start

    def test_rendered_units_carry_config_dir_with_a_space(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A config_dir containing a space must survive both templates
        intact -- the plist as two separate argv array entries (never a
        single joined string), the systemd unit properly shell-quoted."""
        import plistlib
        import re
        import shlex

        spaced_dir = tmp_path / "Application Support" / "nexus"
        spaced_dir.mkdir(parents=True)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(spaced_dir))

        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        _dest, plist_rendered = installer.rendered_unit_content(tier="service")
        raw = re.sub(rb"<!--.*?-->", b"", plist_rendered.encode(), flags=re.S)
        argv = plistlib.loads(raw)["ProgramArguments"]
        idx = argv.index("--config-dir")
        assert argv[idx + 1] == str(spaced_dir.resolve()), argv

        _set_platform(monkeypatch, "linux")
        _dest, unit_rendered = installer.rendered_unit_content(tier="service")
        exec_start = next(
            ln for ln in unit_rendered.splitlines() if ln.startswith("ExecStart=")
        )
        tokens = shlex.split(exec_start[len("ExecStart="):])
        idx = tokens.index("--config-dir")
        assert tokens[idx + 1] == str(spaced_dir.resolve()), exec_start


class TestNoBackgroundProcessType:
    """nexus-rlp0v: ``ProcessType=Background`` in the launchd unit made
    macOS apply background QoS to the whole storage-service tree, confining
    the ONNX embedding inference to the E-cores — measured 15x slowdown
    (~7.6 chunks/s at normal QoS vs ~0.5 chunks/s under background QoS on an
    M4 Max), reproducing the field-reported ~0.5 chunks/s local-mode
    indexing symptom. Indexing is user-initiated work, not background
    maintenance — the key must never come back on any nexus autostart unit.
    """

    def test_macos_plist_template_has_no_process_type(self) -> None:
        import plistlib
        import re

        template = (
            Path(__file__).resolve().parents[2]
            / "conexus" / "daemon" / "com.nexus.service.plist"
        )
        raw = re.sub(rb"<!--.*?-->", b"", template.read_bytes(), flags=re.S)
        data = plistlib.loads(raw)
        assert "ProcessType" not in data, (
            "ProcessType must not be set on the nexus service launchd unit "
            "(nexus-rlp0v: Background QoS confines embedding inference to "
            "E-cores, a measured 15x slowdown) — indexing is user-initiated "
            "foreground work, raise per-thread QoS if background posture is "
            "ever needed, don't reintroduce this key"
        )

    def test_rendered_service_plist_has_no_process_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same assertion against the actually-INSTALLED (post-substitution)
        content, not just the template on disk — belt and suspenders against
        a future ``_render_for_service`` that re-adds the key at render time."""
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        import plistlib
        import re

        dest, rendered = installer.rendered_unit_content(tier="service")
        raw = re.sub(rb"<!--.*?-->", b"", rendered.encode(), flags=re.S)
        data = plistlib.loads(raw)
        assert "ProcessType" not in data

    def test_linux_unit_has_no_cpu_qos_clamp(self) -> None:
        """Linux was already clean (no Nice=/CPUSchedulingPolicy=/
        IOSchedulingClass=) — nexus-rlp0v made no change there; this pins
        that it stays that way."""
        template = (
            Path(__file__).resolve().parents[2]
            / "conexus" / "daemon" / "nexus-service.service"
        )
        text = template.read_text()
        for directive in ("Nice=", "CPUSchedulingPolicy=", "IOSchedulingClass=", "CPUWeight="):
            assert directive not in text, (
                f"{directive} found in the systemd unit — this would clamp "
                "the storage service the same way ProcessType=Background did "
                "on macOS (nexus-rlp0v)"
            )


class TestUnitRestartPolicyMatchesFencedExitContract:
    """nexus-cd1k0.1 / nexus-cd1k0.2: the storage-service supervisor's fix
    for a clean stop (reap-not-poll, exit 0) and for a fenced stand-down
    (fenced_exit_code() -> 0) both rely on a SPECIFIC fact about the two
    shipped units — that a SUCCESSFUL exit does not restart the stack.
    ``TestServicePlistRespawnPosture.test_keepalive_is_successful_exit_false``
    already pins the launchd half of this; this class pins BOTH units
    together as the single fact the supervisor code depends on, so a
    future edit to either unit's restart policy fails a test that names
    WHY, rather than silently reopening the "stopped stack comes back"
    defect this bead fixed."""

    def test_launchd_unit_does_not_restart_on_a_successful_exit(self) -> None:
        import plistlib
        import re

        template = (
            Path(__file__).resolve().parents[2]
            / "conexus" / "daemon" / "com.nexus.service.plist"
        )
        raw = re.sub(rb"<!--.*?-->", b"", template.read_bytes(), flags=re.S)
        data = plistlib.loads(raw)
        assert data["KeepAlive"] == {"SuccessfulExit": False}, (
            "the storage supervisor's stop (nexus-cd1k0.1) and fenced "
            "stand-down (nexus-cd1k0.2) fixes both rely on exit 0 meaning "
            "'stay stopped' under launchd — KeepAlive must stay the "
            "SuccessfulExit=false dict form"
        )

    def test_systemd_unit_restarts_on_failure_only(self) -> None:
        template = (
            Path(__file__).resolve().parents[2]
            / "conexus" / "daemon" / "nexus-service.service"
        )
        active = [
            ln.strip() for ln in template.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        assert "Restart=on-failure" in active, (
            "the storage supervisor's stop (nexus-cd1k0.1) and fenced "
            "stand-down (nexus-cd1k0.2) fixes both rely on exit 0 NOT "
            "restarting under systemd — must stay Restart=on-failure, "
            f"never Restart=always; active directives: {active}"
        )
        assert not any(ln.startswith("Restart=") and ln != "Restart=on-failure" for ln in active), (
            f"exactly one Restart= directive, and it must be on-failure; got {active}"
        )


class TestActivationFailure:
    """nexus-cd1k0.4 and .5: the unit file's fate around activation."""

    def test_activation_failure_restores_the_tree_so_the_retry_activates_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The unit file was written before activation and stayed on an
        ActivationError, so the next run read file == render, answered
        ALREADY_PRESENT and activated nothing (activation attempts stayed
        at 1 across two installs). Reproduced pre-fix. Since nexus-mac7t
        the invariant is held at the short-circuit, which asks the manager
        before it answers ALREADY_PRESENT. The fake here reports the state
        a failed bootstrap REALLY leaves on launchd (critic on 6867dbe4d,
        measured): the label is unlisted by print-disabled (bootstrap
        writes no override, so registration for login reads fine) and
        `launchctl print` says "Could not find service" (not loaded now).
        The file itself stays, so `nx doctor` can report on it."""
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        calls: list[list[str]] = []

        def _fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            if cmd[1] == "print-disabled":
                return subprocess.CompletedProcess(cmd, 0, stdout="\tdisabled services = {\n\t}\n", stderr="")
            if cmd[1] == "print":
                return subprocess.CompletedProcess(cmd, 113, stdout="", stderr="Could not find service \"com.nexus.service\" in domain for uid: 501")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Failed to bootstrap")

        monkeypatch.setattr(installer.subprocess, "run", _fake_run)
        # nexus-t10nc: the timed activation probes go through run_bounded now.
        monkeypatch.setattr(installer, "run_bounded", _fake_run)
        dest = tmp_path / "units" / "com.nexus.service.plist"
        with pytest.raises(installer.ActivationError):
            installer.install_autostart(tier="service")
        assert dest.exists(), "the file stays; the retry is gated on the manager's answer, not on the file"
        with pytest.raises(installer.ActivationError):
            installer.install_autostart(tier="service")
        assert [c[1] for c in calls] == ["bootstrap", "print-disabled", "print", "bootstrap"], calls

    def test_force_over_identical_content_bypasses_the_short_circuit_and_reactivates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--force is the operator's explicit re-activation; it never asks
        the manager and never answers ALREADY_PRESENT. It unloads first
        (cd1k0.5) so a loaded job re-reads the file."""
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        dest = tmp_path / "units" / "com.nexus.service.plist"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _, rendered = installer.rendered_unit_content(tier="service")
        dest.write_text(rendered)
        calls: list[list[str]] = []

        def _fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(installer.subprocess, "run", _fake_run)
        # nexus-t10nc: the timed activation probes go through run_bounded now.
        monkeypatch.setattr(installer, "run_bounded", _fake_run)
        result = installer.install_autostart(tier="service", force=True)
        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert [c[1] for c in calls] == ["bootout", "bootstrap"], calls

    def test_force_over_a_differing_unit_unloads_it_before_activating(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--force over a loaded unit ran no bootout, so launchd kept running
        the old definition (bootstrap of a loaded label is a no-op)."""
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        dest = tmp_path / "units" / "com.nexus.service.plist"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("<plist>old definition</plist>\n")
        calls: list[list[str]] = []

        def _fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(installer.subprocess, "run", _fake_run)
        # nexus-t10nc: the timed activation probes go through run_bounded now.
        monkeypatch.setattr(installer, "run_bounded", _fake_run)
        result = installer.install_autostart(tier="service", force=True)
        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        verbs = [c[1] for c in calls if c and c[0] == "launchctl"]
        assert verbs == ["bootout", "bootstrap"], calls


# ── library: autostart_activation_state (nexus-mac7t) ────────────────────────


_DISABLED_LISTING = '\tdisabled services = {\n\t\t"com.other.agent" => enabled\n\t\t"com.nexus.service" => disabled\n\t}\n'
_ENABLED_LISTING = '\tdisabled services = {\n\t\t"com.other.agent" => enabled\n\t\t"com.nexus.service" => enabled\n\t}\n'


def _fake_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, body: str) -> Path:
    """A fake manager binary as the ONLY thing on PATH, with the absolute
    fallbacks emptied so the real /bin/launchctl is never consulted."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir(exist_ok=True)
    script = fake_bin / name
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setattr(installer, "_MANAGER_ABSOLUTE_PATHS", {})
    return script


class TestAutostartActivationState:
    """The manager's answer, through the REAL subprocess path against fake
    manager binaries on a PATH of exactly one directory. NOT_ACTIVE only on
    a positive answer; everything the manager cannot answer is UNREACHABLE."""

    def test_query_cmd_shapes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import os  # noqa: PLC0415 — local import, test-only convenience

        dest = tmp_path / "units" / "com.nexus.service.plist"
        _set_platform(monkeypatch, "darwin")
        # print-disabled, not print: `launchctl print` answers "loaded now",
        # and a booted-out job loads again at the next login.
        assert installer._activation_query_cmd(dest, tier="service") == [
            "launchctl", "print-disabled", f"gui/{os.getuid()}",
        ]
        _set_platform(monkeypatch, "linux")
        dest = tmp_path / "units" / "nexus-service.service"
        assert installer._activation_query_cmd(dest, tier="service") == [
            "systemctl", "--user", "is-enabled", "nexus-service.service",
        ]

    def test_darwin_label_listed_disabled_is_not_active_with_an_enable_first_remedy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os  # noqa: PLC0415 — local import, test-only convenience

        _set_platform(monkeypatch, "darwin")
        _fake_manager(tmp_path, monkeypatch, "launchctl", f"printf '%s' '{_DISABLED_LISTING}'\nexit 0\n")
        probe = installer.autostart_activation_state(tmp_path / "com.nexus.service.plist", tier="service")
        assert probe.state is installer.ActivationState.NOT_ACTIVE
        assert "reports com.nexus.service disabled" in probe.detail
        # a disabled label refuses bootstrap, so the remedy enables it first
        assert probe.remedy.startswith(f"launchctl enable gui/{os.getuid()}/com.nexus.service && ")
        assert probe.remedy.endswith("nx daemon service uninstall --autostart && nx daemon service install --autostart")

    def test_darwin_label_enabled_or_unlisted_is_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        script = _fake_manager(tmp_path, monkeypatch, "launchctl", f"printf '%s' '{_ENABLED_LISTING}'\nexit 0\n")
        probe = installer.autostart_activation_state(tmp_path / "com.nexus.service.plist", tier="service")
        assert probe.state is installer.ActivationState.ACTIVE and probe.detail == ""
        # unlisted labels are enabled by default
        script.write_text("#!/bin/sh\nprintf '%s' '\tdisabled services = {\n\t}\n'\nexit 0\n")
        probe = installer.autostart_activation_state(tmp_path / "com.nexus.service.plist", tier="service")
        assert probe.state is installer.ActivationState.ACTIVE

    def test_darwin_older_dialect_true_is_disabled_and_an_unknown_token_is_unreachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`=> true` was the older launchctl dialect for disabled; a token
        the parser does not know must not read as good news."""
        _set_platform(monkeypatch, "darwin")
        script = _fake_manager(tmp_path, monkeypatch, "launchctl", "printf '%s' '\t\t\"com.nexus.service\" => true\n'\nexit 0\n")
        probe = installer.autostart_activation_state(tmp_path / "com.nexus.service.plist", tier="service")
        assert probe.state is installer.ActivationState.NOT_ACTIVE
        script.write_text("#!/bin/sh\nprintf '%s' '\t\t\"com.nexus.service\" => maybe\n'\nexit 0\n")
        probe = installer.autostart_activation_state(tmp_path / "com.nexus.service.plist", tier="service")
        assert probe.state is installer.ActivationState.UNREACHABLE
        assert "`maybe`" in probe.detail

    @pytest.mark.skipif(sys.platform != "darwin", reason="parses the real launchctl on a Mac only")
    def test_real_launchctl_print_disabled_speaks_a_dialect_the_parser_knows(self) -> None:
        """The regex and token sets are pinned to output the author typed;
        this reads the REAL binary once so a dialect change surfaces here
        rather than as a silent ACTIVE."""
        import os  # noqa: PLC0415 — local import, test-only convenience
        import re  # noqa: PLC0415 — local import, test-only convenience

        result = subprocess.run(
            ["/bin/launchctl", "print-disabled", f"gui/{os.getuid()}"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if result.returncode != 0:
            pytest.skip(f"no gui domain for this uid here: {result.stderr.strip()}")
        rows = re.findall(r'"([^"]+)"\s*=>\s*(\S+)', result.stdout)
        assert rows, result.stdout
        known = installer._LAUNCHD_DISABLED_TOKENS | installer._LAUNCHD_ENABLED_TOKENS
        unknown = sorted({tok for _, tok in rows if tok not in known})
        assert unknown == [], f"launchctl print-disabled speaks tokens the parser does not know: {unknown}"

    def test_a_manager_that_will_not_run_is_unreachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-executable launchctl on PATH: which() skips it, the bare
        spawn hits it and raises PermissionError -- the OSError arm."""
        _set_platform(monkeypatch, "darwin")
        script = _fake_manager(tmp_path, monkeypatch, "launchctl", "exit 0\n")
        script.chmod(0o644)
        probe = installer.autostart_activation_state(tmp_path / "com.nexus.service.plist", tier="service")
        assert probe.state is installer.ActivationState.UNREACHABLE
        assert "could not run" in probe.detail

    def test_darwin_no_gui_domain_is_unreachable_never_not_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Headless / ssh: launchd has no gui domain for the uid. That is
        "cannot tell", and reading it as NOT_ACTIVE once sent restart-stale
        into a bounce that deleted a working unit."""
        _set_platform(monkeypatch, "darwin")
        _fake_manager(tmp_path, monkeypatch, "launchctl", "echo 'Could not find domain for gui/501' >&2\nexit 113\n")
        probe = installer.autostart_activation_state(tmp_path / "com.nexus.service.plist", tier="service")
        assert probe.state is installer.ActivationState.UNREACHABLE
        assert "exited 113" in probe.detail and "Could not find domain" in probe.detail
        assert probe.remedy == ""

    def test_linux_enabled_is_active(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_platform(monkeypatch, "linux")
        _fake_manager(tmp_path, monkeypatch, "systemctl", "echo enabled\nexit 0\n")
        probe = installer.autostart_activation_state(tmp_path / "nexus-service.service", tier="service")
        assert probe.state is installer.ActivationState.ACTIVE

    def test_linux_disabled_or_not_found_is_not_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "linux")
        script = _fake_manager(tmp_path, monkeypatch, "systemctl", "echo disabled\nexit 1\n")
        probe = installer.autostart_activation_state(tmp_path / "nexus-service.service", tier="service")
        assert probe.state is installer.ActivationState.NOT_ACTIVE
        assert probe.detail.endswith("reports disabled"), probe.detail
        assert probe.remedy == "nx daemon service uninstall --autostart && nx daemon service install --autostart"
        script.write_text("#!/bin/sh\necho not-found\nexit 1\n")
        probe = installer.autostart_activation_state(tmp_path / "nexus-service.service", tier="service")
        assert probe.state is installer.ActivationState.NOT_ACTIVE
        assert probe.detail.endswith("reports not-found"), probe.detail

    def test_linux_no_user_bus_is_unreachable_never_not_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "linux")
        _fake_manager(tmp_path, monkeypatch, "systemctl", "echo 'Failed to connect to bus: No medium found' >&2\nexit 1\n")
        probe = installer.autostart_activation_state(tmp_path / "nexus-service.service", tier="service")
        assert probe.state is installer.ActivationState.UNREACHABLE
        assert "Failed to connect to bus" in probe.detail

    def test_no_manager_anywhere_is_no_manager(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_platform(monkeypatch, "darwin")
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        monkeypatch.setattr(installer, "_MANAGER_ABSOLUTE_PATHS", {"launchctl": (str(tmp_path / "nowhere" / "launchctl"),)})
        probe = installer.autostart_activation_state(tmp_path / "com.nexus.service.plist", tier="service")
        assert probe.state is installer.ActivationState.NO_MANAGER
        assert probe.detail.startswith("launchctl not found on PATH or at ")

    def test_trimmed_path_still_finds_the_manager_at_its_known_location(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An MCP server or cron entry runs nx doctor with a trimmed PATH;
        /bin/launchctl still exists, so that must not read as NO_MANAGER."""
        _set_platform(monkeypatch, "darwin")
        script = _fake_manager(tmp_path, monkeypatch, "launchctl", f"printf '%s' '{_ENABLED_LISTING}'\nexit 0\n")
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        monkeypatch.setattr(installer, "_MANAGER_ABSOLUTE_PATHS", {"launchctl": (str(script),)})
        probe = installer.autostart_activation_state(tmp_path / "com.nexus.service.plist", tier="service")
        assert probe.state is installer.ActivationState.ACTIVE

    def test_hung_manager_is_unreachable_not_an_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raise here would drop the whole doctor row (health.py swallows
        probe failures), so a timeout is an answer, not an exception."""
        _set_platform(monkeypatch, "darwin")
        _fake_manager(tmp_path, monkeypatch, "launchctl", "/bin/sleep 5\nexit 0\n")
        monkeypatch.setattr(installer, "_ACTIVATION_QUERY_TIMEOUT", 0.2)
        probe = installer.autostart_activation_state(tmp_path / "com.nexus.service.plist", tier="service")
        assert probe.state is installer.ActivationState.UNREACHABLE
        assert "did not answer within 0.2s" in probe.detail


class TestInstallAutostartConsultsTheManager:
    """nexus-mac7t: the identical-content short-circuit asks the manager.
    The file stays on every activation failure; cd1k0.4's invariant (a
    failed activation must not read as ALREADY_PRESENT on the retry) is
    held here instead of by deleting the file."""

    def test_missing_manager_raises_names_the_remedy_and_leaves_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        # the absolute fallback would otherwise reach the REAL /bin/launchctl
        monkeypatch.setattr(installer, "_MANAGER_ABSOLUTE_PATHS", {})
        dest = tmp_path / "units" / "com.nexus.service.plist"
        with pytest.raises(installer.ActivationError) as excinfo:
            installer.install_autostart(tier="service")
        assert isinstance(excinfo.value.__cause__, FileNotFoundError)
        assert dest.exists(), "with no manager to retry against the file stays installed"
        assert "file installed but not activated" in str(excinfo.value)
        assert "nx daemon service uninstall --autostart && nx daemon service install --autostart" in str(excinfo.value)

    def test_identical_file_the_manager_reports_disabled_is_reactivated_not_already_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        dest = tmp_path / "units" / "com.nexus.service.plist"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _, rendered = installer.rendered_unit_content(tier="service")
        dest.write_text(rendered)
        calls: list[list[str]] = []

        def _fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            if cmd[1] == "print-disabled":
                return subprocess.CompletedProcess(cmd, 0, stdout=_DISABLED_LISTING, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(installer.subprocess, "run", _fake_run)
        # nexus-t10nc: the timed activation probes go through run_bounded now.
        monkeypatch.setattr(installer, "run_bounded", _fake_run)
        result = installer.install_autostart(tier="service")
        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert [c[1] for c in calls] == ["print-disabled", "bootstrap"], calls

    def test_identical_file_registered_but_not_loaded_is_reactivated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The state after a failed bootstrap: unlisted by print-disabled,
        unknown to launchctl print. A retry must bootstrap again."""
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        dest = tmp_path / "units" / "com.nexus.service.plist"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _, rendered = installer.rendered_unit_content(tier="service")
        dest.write_text(rendered)
        calls: list[list[str]] = []

        def _fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            if cmd[1] == "print":
                return subprocess.CompletedProcess(cmd, 113, stdout="", stderr="Could not find service \"com.nexus.service\" in domain for uid: 501")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(installer.subprocess, "run", _fake_run)
        # nexus-t10nc: the timed activation probes go through run_bounded now.
        monkeypatch.setattr(installer, "run_bounded", _fake_run)
        result = installer.install_autostart(tier="service")
        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert [c[1] for c in calls] == ["print-disabled", "print", "bootstrap"], calls

    def test_identical_file_registered_and_loaded_is_already_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        dest = tmp_path / "units" / "com.nexus.service.plist"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _, rendered = installer.rendered_unit_content(tier="service")
        dest.write_text(rendered)
        calls: list[list[str]] = []

        def _fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="com.nexus.service = { active count = 1 }", stderr="")

        monkeypatch.setattr(installer.subprocess, "run", _fake_run)
        # nexus-t10nc: the timed activation probes go through run_bounded now.
        monkeypatch.setattr(installer, "run_bounded", _fake_run)
        result = installer.install_autostart(tier="service")
        assert result.status is installer.InstallStatus.ALREADY_PRESENT
        assert "registered" in result.detail
        assert [c[1] for c in calls] == ["print-disabled", "print"], calls

    def test_disabled_label_whose_bootstrap_refuses_names_the_enable_remedy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one path that hard-fails must carry the same remedy the
        doctor row and converge name (code-review-expert on 6867dbe4d)."""
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        dest = tmp_path / "units" / "com.nexus.service.plist"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _, rendered = installer.rendered_unit_content(tier="service")
        dest.write_text(rendered)

        def _fake_run(cmd, *a, **k):
            if cmd[1] == "print-disabled":
                return subprocess.CompletedProcess(cmd, 0, stdout=_DISABLED_LISTING, stderr="")
            return subprocess.CompletedProcess(cmd, 125, stdout="", stderr="Bootstrap failed: 125: Domain does not support specified action")

        monkeypatch.setattr(installer.subprocess, "run", _fake_run)
        # nexus-t10nc: the timed activation probes go through run_bounded now.
        monkeypatch.setattr(installer, "run_bounded", _fake_run)
        with pytest.raises(installer.ActivationError) as excinfo:
            installer.install_autostart(tier="service")
        assert "exited 125" in str(excinfo.value)
        assert "launchctl enable gui/" in str(excinfo.value)

    def test_identical_file_with_an_unreachable_manager_is_already_present_and_says_unconfirmed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Over ssh the activation would fail for the same reason the query
        did, so the command does not churn; it says what it could not
        confirm instead of claiming the unit is registered."""
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        dest = tmp_path / "units" / "com.nexus.service.plist"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _, rendered = installer.rendered_unit_content(tier="service")
        dest.write_text(rendered)
        calls: list[list[str]] = []

        def _fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 113, stdout="", stderr="Could not find domain for gui/501")

        monkeypatch.setattr(installer.subprocess, "run", _fake_run)
        # nexus-t10nc: the timed activation probes go through run_bounded now.
        monkeypatch.setattr(installer, "run_bounded", _fake_run)
        result = installer.install_autostart(tier="service")
        assert result.status is installer.InstallStatus.ALREADY_PRESENT
        assert "could not confirm" in result.detail and "Could not find domain" in result.detail
        assert [c[1] for c in calls] == ["print-disabled"], calls
