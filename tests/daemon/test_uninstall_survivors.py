# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-dmgvx (GH #1419 Issue 2): uninstall must name what it left running.

Steve Harris ran ``nx daemon service uninstall --autostart``, the LaunchAgent
went away, and a postgres process kept running until he found and killed it by
hand.

THE BEAD'S FRAMING WAS A RACE ("launchd respawns postgres in the
stop-to-remove-autostart window"). Reading the actual sequence, the ordering is
already correct — ``uninstall_autostart`` deactivates (launchctl bootout /
systemctl disable) BEFORE unlinking the unit file. And the command never stops
anything in the first place: it is documented as removing the autostart entry
only, and ``nx daemon service stop`` leaves Postgres running BY DESIGN ("it is
independently managed", daemon.py's own message).

So the defect is not ordering and not a race. It is that ``uninstall`` prints
"Removed <path>" — an unqualified success — while a service supervisor and a
Postgres cluster may both still be live, and says nothing about either. The
user reasonably reads "uninstalled" as "gone".

FIX: the shared primitive reports SURVIVORS, and both tiers' CLI wrappers
surface them. Placing this in ``uninstall_autostart`` rather than in the
service command satisfies the RDR-149 standing rule (lifecycle fixes land in
the shared primitive, never one tier's copy) — ``nx daemon t2 uninstall`` gets
the same treatment for free.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def installed_unit(tmp_path: Path):
    """A unit file present at the tier's install dir, so uninstall proceeds."""
    from nexus.daemon import installer

    install_dir = tmp_path / "LaunchAgents"
    install_dir.mkdir(parents=True)
    dest = install_dir / installer._autostart_filename_for("service")
    dest.write_text("<plist/>")
    with patch(
        "nexus.commands.daemon._autostart_install_dir", return_value=install_dir,
    ), patch(
        "nexus.daemon.installer.subprocess.run",
    ) as run:
        run.return_value.returncode = 0
        run.return_value.stderr = ""
        run.return_value.stdout = ""
        yield dest


class TestUninstallReportsSurvivors:
    def test_clean_uninstall_reports_no_survivors(
        self, installed_unit, tmp_path: Path,
    ) -> None:
        from nexus.daemon import installer

        with patch.object(installer, "_probe_survivors", return_value=()):
            result = installer.uninstall_autostart(tier="service")

        assert result.status is installer.UninstallStatus.REMOVED
        assert result.survivors == ()

    def test_live_service_is_named_as_a_survivor(self, installed_unit) -> None:
        """The unit is gone, so it will not come back — but it is still
        running now, and nothing else will stop it."""
        from nexus.daemon import installer

        with patch.object(
            installer, "_probe_survivors",
            return_value=("storage service (pid 4242) is still running",),
        ):
            result = installer.uninstall_autostart(tier="service")

        assert result.survivors
        assert "still running" in result.survivors[0]

    def test_survivors_do_not_change_the_status(self, installed_unit) -> None:
        """The uninstall genuinely SUCCEEDED at what it claims to do — remove
        the autostart entry. Survivors are a completeness report, not a
        failure, so the status stays REMOVED and callers that branch on it are
        unaffected."""
        from nexus.daemon import installer

        with patch.object(
            installer, "_probe_survivors", return_value=("postgres on :5433",),
        ):
            result = installer.uninstall_autostart(tier="service")

        assert result.status is installer.UninstallStatus.REMOVED

    def test_probe_failure_never_breaks_the_uninstall(self, installed_unit) -> None:
        """A survivor probe that blows up must not strand a half-uninstalled
        unit — the file removal already happened."""
        from nexus.daemon import installer

        with patch.object(
            installer, "_probe_survivors", side_effect=RuntimeError("probe boom"),
        ):
            result = installer.uninstall_autostart(tier="service")

        assert result.status is installer.UninstallStatus.REMOVED
        assert any("probe" in w.lower() for w in result.warnings)

    def test_not_installed_short_circuits_without_probing(self, tmp_path: Path) -> None:
        from nexus.daemon import installer

        install_dir = tmp_path / "empty"
        install_dir.mkdir()
        with patch(
            "nexus.commands.daemon._autostart_install_dir", return_value=install_dir,
        ), patch.object(installer, "_probe_survivors") as probe:
            result = installer.uninstall_autostart(tier="service")

        assert result.status is installer.UninstallStatus.NOT_INSTALLED
        probe.assert_not_called()


class TestProbeSurvivors:
    def test_reports_a_fresh_service_lease(self, tmp_path: Path) -> None:
        """Liveness comes from the RDR-149 lease primitive, not a bespoke ps
        sweep — the lifecycle gate exists to keep exactly that from being
        reinvented per tier."""
        from nexus.daemon import installer

        class _Lease:
            supervisor_pid = 4242
            port = 8899

        with patch.object(installer, "_discover_service_lease", return_value=_Lease()), \
                patch.object(installer, "_probe_live_postgres", return_value=None):
            out = installer._probe_survivors(tier="service")

        assert any("4242" in s for s in out)

    def test_reports_live_postgres_with_the_stop_command(self, tmp_path: Path) -> None:
        from nexus.daemon import installer

        with patch.object(installer, "_discover_service_lease", return_value=None), \
                patch.object(installer, "_probe_live_postgres", return_value=5433):
            out = installer._probe_survivors(tier="service")

        assert any("5433" in s for s in out)
        # The whole point is that the user should not have to hunt for it.
        assert any("--with-pg" in s or "pg_ctl" in s for s in out)

    def test_quiet_when_nothing_survives(self) -> None:
        from nexus.daemon import installer

        with patch.object(installer, "_discover_service_lease", return_value=None), \
                patch.object(installer, "_probe_live_postgres", return_value=None):
            assert installer._probe_survivors(tier="service") == ()

    def test_t2_tier_does_not_probe_the_storage_service(self) -> None:
        """t2 and service are different units; uninstalling the t2 agent must
        not report the storage service as ITS survivor."""
        from nexus.daemon import installer

        with patch.object(installer, "_discover_service_lease") as lease, \
                patch.object(installer, "_probe_live_postgres") as pg:
            installer._probe_survivors(tier="t2")

        lease.assert_not_called()
        pg.assert_not_called()


class TestPostgresProbeAgainstARealSocket:
    """The other survivor tests stub ``_probe_live_postgres``, which proves the
    reporting but not the DETECTION. These bind a real listener on port 0 and
    point the credentials at it, so the socket logic itself is exercised —
    otherwise a probe that never detects anything would pass every test above
    while reporting "nothing survived" on a box where postgres is very much
    alive (the precise failure being fixed)."""

    def _creds_at(self, tmp_path: Path, port: int):
        (tmp_path / "pg_credentials").write_text(f"PG_PORT={port}\n")
        return patch("nexus.config.nexus_config_dir", return_value=tmp_path)

    def test_detects_a_live_listener(self, tmp_path: Path) -> None:
        import socket as _s

        from nexus.daemon import installer

        srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            with self._creds_at(tmp_path, port):
                assert installer._probe_live_postgres() == port
        finally:
            srv.close()

    def test_reports_nothing_when_the_port_is_closed(self, tmp_path: Path) -> None:
        import socket as _s

        from nexus.daemon import installer

        # Bind then close: the port is real but nothing is listening on it now.
        srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()

        with self._creds_at(tmp_path, port):
            assert installer._probe_live_postgres() is None

    def test_absent_credentials_report_nothing(self, tmp_path: Path) -> None:
        from nexus.daemon import installer

        with patch("nexus.config.nexus_config_dir", return_value=tmp_path):
            assert installer._probe_live_postgres() is None


def test_service_uninstall_cmd_surfaces_survivors(tmp_path: Path) -> None:
    """A survivor list the CLI never prints is the same silence as before."""
    from click.testing import CliRunner

    from nexus.commands.daemon import daemon_group
    from nexus.daemon import installer

    fake = installer.UninstallResult(
        status=installer.UninstallStatus.REMOVED,
        dest=tmp_path / "com.nexus.service.plist",
        warnings=(),
        survivors=("storage service (pid 4242) is still running — nx daemon service stop",),
    )
    with patch("nexus.daemon.installer.uninstall_autostart", return_value=fake):
        result = CliRunner().invoke(
            daemon_group, ["service", "uninstall", "--autostart"],
        )

    assert result.exit_code == 0, result.output
    assert "4242" in result.output
    assert "still running" in result.output
