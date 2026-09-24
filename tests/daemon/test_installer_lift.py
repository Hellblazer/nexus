# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-126 §2 (nexus-dyqu7): contract for the lifted ``nexus.daemon.installer``.

The autostart install/uninstall logic is lifted out of the Click command bodies
in ``src/nexus/commands/daemon.py`` into pure library functions so it can be
called in-process by the ``daemon_uninstall`` MCP tool (RDR-126 §4),
``upgrade_finish``, and the ``nx daemon service install/uninstall`` CLI (thin
wrappers). Library functions NEVER call ``click.echo`` / ``sys.exit``; they
return ``InstallResult`` / ``UninstallResult`` or raise typed ``InstallerError``
subclasses.

TIER SPLIT AFTER THE T2 DAEMON'S RETIREMENT (nexus-i711w Stage 2 sub-stage B).
This file originally drove every case through ``tier="t2"``. That tier is no
longer installable, but the two halves did NOT die together:

- INSTALL is exercised against ``tier="service"``, the only installable tier.
  These are the GENERIC installer guards — symlink refusal, content-diff
  refusal, activation failure with and without ``force``, 0644 mode, and
  idempotent no-activation — and this file is their ONLY home; deleting it with
  the daemon would have silently dropped four guard classes from the surviving
  installer. The tier-specific render assertions that DID duplicate
  ``test_service_install.py`` were dropped rather than carried over.
- UNINSTALL stays on ``tier="t2"`` on purpose. Removal machinery outlives what
  it removes: a box upgraded from a pre-retirement install still carries a
  launchd/systemd unit firing ``nx daemon t2 start``, and these are the tests
  that prove it can still be booted out. Since the unit can no longer be
  INSTALLED, the setup writes a legacy one by hand — which is exactly the state
  such a box is in.

The generic autostart helpers stay in ``daemon.py`` (shared with the T3 paths);
``installer`` delegates to them, so tests stub the same ``daemon_cmd._autostart_*``
indirection points used by the T3 install tests. ``launchctl`` / ``systemctl``
shell-out is mocked; template substitution + file placement are exercised for real.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.commands import daemon as daemon_cmd
from nexus.daemon import installer

#: What a pre-retirement install left on disk. Content is deliberately opaque —
#: the uninstall path keys on the unit's NAME and label, never its body.
_LEGACY_T2_UNIT_BODY = "<!-- legacy T2 unit from a pre-retirement install -->\n"


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


def _plant_legacy_t2_unit(tmp_path: Path) -> Path:
    """Write the T2 autostart unit a pre-retirement install would have left.

    Deliberately NOT ``install_autostart(tier="t2")`` — that path is gone. The
    upgrade scenario these tests cover is precisely "a unit exists that this
    build can no longer produce", so planting the file directly is the faithful
    setup, not a shortcut around a missing API.
    """
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    dest = units / daemon_cmd._autostart_filename_t2()
    dest.write_text(_LEGACY_T2_UNIT_BODY)
    return dest


def _install_service(tmp_path: Path, *, force: bool = False) -> installer.InstallResult:
    """``install_autostart(tier="service")`` with activation mocked successful."""
    with patch.object(daemon_cmd.subprocess, "run") as mock_run, \
            patch.object(installer, "run_bounded", new=mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
        mock_run.return_value.stdout = ""
        return installer.install_autostart(tier="service", force=force)


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

    def test_no_installable_t2_tier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The T2 render arm is gone, so ``tier="t2"`` is not installable at all
        — not merely absent as a default. Pair with ``TestUninstall`` below,
        which proves the REMOVE half survives."""
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="t2"):
            installer.install_autostart(tier="t2")
        assert not (tmp_path / "units" / "com.nexus.t2.plist").exists()


class TestInstallMode:
    def test_installed_unit_is_mode_0644(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        result = _install_service(tmp_path)
        assert (result.dest.stat().st_mode & 0o777) == 0o644


class TestInstallActivationCommand:
    def test_linux_install_activates_via_systemctl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Linux activation argv. ``test_service_install`` covers the macOS
        launchctl side and the rendered body; this is the systemd half."""
        _set_platform(monkeypatch, "linux")
        _stub_paths(tmp_path, monkeypatch)
        result = _install_service(tmp_path)
        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert result.activated_cmd == [
            "systemctl", "--user", "enable", "--now", "nexus-service.service",
        ]


class TestInstallIdempotent:
    def test_reinstall_identical_content_reports_already_present_no_activation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        _install_service(tmp_path)

        # Second call: content matches the freshly rendered template, so
        # no write and no activation shell-out happens.
        with patch.object(daemon_cmd.subprocess, "run") as mock_run2, \
                patch.object(installer, "run_bounded", new=mock_run2):
            mock_run2.return_value.returncode = 0
            mock_run2.return_value.stdout = ""
            mock_run2.return_value.stderr = ""
            result = installer.install_autostart(tier="service")
        assert result.status is installer.InstallStatus.ALREADY_PRESENT
        # nexus-mac7t: the only calls are the read-only queries (registered
        # for login, loaded now), never an activation.
        assert [c.args[0][1] for c in mock_run2.call_args_list] == ["print-disabled", "print"]
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
        link = tmp_path / "units" / "com.nexus.service.plist"
        link.symlink_to(real)

        with pytest.raises(installer.SymlinkRefusedError):
            installer.install_autostart(tier="service")
        # The symlink target is left untouched.
        assert real.read_text() == "<!-- real -->\n"


class TestContentDiffGuard:
    def test_install_raises_when_content_differs_without_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        (tmp_path / "units").mkdir()
        dest = tmp_path / "units" / "com.nexus.service.plist"
        dest.write_text("<!-- operator customisation -->\n")

        with pytest.raises(installer.ContentDiffersError):
            installer.install_autostart(tier="service")
        assert dest.read_text() == "<!-- operator customisation -->\n"

    def test_force_overwrites_differing_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        (tmp_path / "units").mkdir()
        dest = tmp_path / "units" / "com.nexus.service.plist"
        dest.write_text("<!-- old -->\n")

        result = _install_service(tmp_path, force=True)

        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert "<!-- old -->" not in dest.read_text()
        assert "<string>/opt/conexus/bin/nx</string>" in dest.read_text()


class TestActivationFailure:
    def test_activation_failure_without_force_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)

        with patch.object(daemon_cmd.subprocess, "run") as mock_run, \
                patch.object(installer, "run_bounded", new=mock_run):
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "boom"
            mock_run.return_value.stdout = ""
            with pytest.raises(installer.ActivationError):
                installer.install_autostart(tier="service")
        # The file stays on an activation failure (nexus-mac7t): the retry
        # asks the service manager before it short-circuits, so cd1k0.4's
        # invariant no longer needs the file deleted.
        assert (tmp_path / "units" / "com.nexus.service.plist").exists()

    def test_activation_failure_with_force_returns_newly_installed_with_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)

        with patch.object(daemon_cmd.subprocess, "run") as mock_run, \
                patch.object(installer, "run_bounded", new=mock_run):
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "boom"
            mock_run.return_value.stdout = ""
            result = installer.install_autostart(tier="service", force=True)

        # Under --force, activation failure is downgraded to a warning:
        # the file is installed, the result reports it, no raise.
        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert result.warnings
        assert any("boom" in w for w in result.warnings)


class TestUninstall:
    """The SURVIVING half of the T2 tier. Every case here starts from a unit
    this build can no longer install — the state of any box upgraded across the
    daemon's retirement."""

    def test_uninstall_removes_legacy_t2_unit_and_calls_bootout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        dest = _plant_legacy_t2_unit(tmp_path)

        with patch.object(daemon_cmd.subprocess, "run") as mock_run, \
                patch.object(installer, "run_bounded", new=mock_run):
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
        assert "com.nexus.service" not in cmd[2]

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
        dest = _plant_legacy_t2_unit(tmp_path)

        with patch.object(daemon_cmd.subprocess, "run") as mock_run, \
                patch.object(installer, "run_bounded", new=mock_run):
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "bootout failed"
            mock_run.return_value.stdout = ""
            result = installer.uninstall_autostart()

        # bootout failure must NOT block file removal: the unit file is the
        # durable artifact, and leaving it is what makes the stale unit fire a
        # nonexistent command on the next boot.
        assert result.status is installer.UninstallStatus.REMOVED
        assert not dest.exists()
        assert result.warnings


class TestLinuxUninstall:
    def test_uninstall_calls_systemctl_disable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "linux")
        _stub_paths(tmp_path, monkeypatch)
        _plant_legacy_t2_unit(tmp_path)

        with patch.object(daemon_cmd.subprocess, "run") as mock_run, \
                patch.object(installer, "run_bounded", new=mock_run):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = ""
            result = installer.uninstall_autostart()

        assert result.status is installer.UninstallStatus.REMOVED
        cmd = mock_run.call_args[0][0]
        assert cmd == ["systemctl", "--user", "disable", "--now", "nexus-t2.service"]


class TestManagerActionsCarryATimeout:
    """nexus-k9i56: ``_run_manager``'s ``timeout`` is now a required,
    keyword-only parameter rather than something read out of ``**kwargs``
    with a stock-``subprocess.run`` fallback when a caller omitted it.
    That closes the untimed branch structurally (a caller that forgets
    ``timeout`` gets Python's own ``TypeError`` at the call), but each
    site is pinned here too, one per call site, so a future edit that
    reintroduces an untimed branch fails on THIS test rather than only
    surfacing as an unbounded hang far from the diff that caused it.
    """

    def test_activation_probe_passes_the_query_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        calls: list[dict[str, object]] = []

        def _fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(kwargs)
            raise FileNotFoundError(2, "no such file or directory", cmd[0])

        monkeypatch.setattr(installer, "_run_manager", _fake)
        probe = installer.autostart_activation_state(tmp_path / "com.nexus.service.plist", tier="service")

        assert probe.state is installer.ActivationState.NO_MANAGER
        assert len(calls) == 1
        assert calls[0]["timeout"] == installer._ACTIVATION_QUERY_TIMEOUT

    def test_loaded_now_probe_passes_the_query_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        calls: list[dict[str, object]] = []

        def _fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(kwargs)
            raise OSError("boom")

        monkeypatch.setattr(installer, "_run_manager", _fake)
        result = installer._launchd_loaded_now("service")

        assert result is None
        assert len(calls) == 1
        assert calls[0]["timeout"] == installer._ACTIVATION_QUERY_TIMEOUT

    def test_predeactivate_and_activate_pass_the_action_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``force=True`` over differing content runs BOTH untimed-before-
        this-bead call sites in one pass: the best-effort predeactivate
        unload, then the real activation."""
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        (tmp_path / "units").mkdir()
        (tmp_path / "units" / "com.nexus.service.plist").write_text("<!-- old -->\n")

        calls: list[dict[str, object]] = []

        def _fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(installer, "_run_manager", _fake)
        result = installer.install_autostart(tier="service", force=True)

        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert len(calls) == 2, "expected one predeactivate call and one activate call"
        assert all(
            kw.get("timeout") == installer._MANAGER_ACTION_TIMEOUT_S for kw in calls
        )

    def test_uninstall_deactivate_passes_the_action_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        _plant_legacy_t2_unit(tmp_path)

        calls: list[dict[str, object]] = []

        def _fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(installer, "_run_manager", _fake)
        result = installer.uninstall_autostart()

        assert result.status is installer.UninstallStatus.REMOVED
        assert len(calls) == 1
        assert calls[0].get("timeout") == installer._MANAGER_ACTION_TIMEOUT_S


class TestHungManagerIsKilledAtTheBound:
    """nexus-k9i56: a manager that never answers -- a fake binary that
    sleeps, standing in for a wedged ``launchctl``/``systemctl`` -- must be
    killed at the bound rather than left to hang install/uninstall
    forever, and the resulting report must name the verb (the command
    that hung) and the bound, never a silent pass.
    """

    def test_hung_activation_is_killed_and_raises_naming_the_verb_and_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        monkeypatch.setattr(installer, "_activate_cmd", lambda dest: ["sleep", "30"])
        monkeypatch.setattr(installer, "_MANAGER_ACTION_TIMEOUT_S", 0.2)

        start = time.monotonic()
        with pytest.raises(installer.ActivationError) as excinfo:
            installer.install_autostart(tier="service")
        elapsed = time.monotonic() - start

        assert elapsed < 10, (
            f"took {elapsed:.2f}s against a 0.2s bound -- the hang was not "
            "actually bounded"
        )
        msg = str(excinfo.value)
        assert "sleep 30" in msg, f"error does not name the verb: {msg!r}"
        assert "0.2s" in msg, f"error does not name the bound: {msg!r}"

    def test_hung_deactivate_during_uninstall_is_killed_and_warned_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uninstall's deactivate is best-effort (the unit file is removed
        either way -- ``uninstall_autostart``'s own docstring), so a hung
        manager here is a warning naming the verb and bound, not a raise."""
        _set_platform(monkeypatch, "darwin")
        _stub_paths(tmp_path, monkeypatch)
        dest = _plant_legacy_t2_unit(tmp_path)
        monkeypatch.setattr(
            installer, "_deactivate_cmd", lambda dest, *, tier="t2": ["sleep", "30"]
        )
        monkeypatch.setattr(installer, "_MANAGER_ACTION_TIMEOUT_S", 0.2)

        start = time.monotonic()
        result = installer.uninstall_autostart()
        elapsed = time.monotonic() - start

        assert elapsed < 10, (
            f"took {elapsed:.2f}s against a 0.2s bound -- the hang was not "
            "actually bounded"
        )
        assert result.status is installer.UninstallStatus.REMOVED
        assert not dest.exists()
        assert result.deactivated is False
        assert result.warnings
        assert any("sleep 30" in w and "0.2s" in w for w in result.warnings), result.warnings
