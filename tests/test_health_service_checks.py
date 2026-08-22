# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the three storage-service health checks introduced in bead nexus-gmiaf.33.

Tests are FAST (no subprocesses, no real PG, no network) — psql runner and HTTP
client are injected as callables so unit tests exercise all parsing/result logic
in-process.

Integration tests (marked @pytest.mark.integration) live in
tests/db/test_health_service_integration.py and require the real JAR + PG16.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from nexus.db import chash_tables

from nexus.health import (
    HealthResult,
    _check_engine_convergence,
    _check_migration_state,
    _check_rls_present,
    _check_service_autostart_drift,
    _check_storage_service_health,
    _check_t2_launchagent_stray,
    _check_service_launchagent_stray,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_creds_file(tmp_path: Path, **overrides) -> Path:
    """Write a minimal pg_credentials file and return its path.

    Includes the RDR-182 nexus_diag keys by default (the chash-conformance
    probe resolves them, nexus-vounk); pass ``NX_DB_DIAG_USER=None`` etc. via
    overrides to simulate a pre-P2.1 file with no diagnostic role.
    """
    defaults = {
        "PG_PORT": "54321",
        "NX_DB_ADMIN_URL": "jdbc:postgresql://127.0.0.1:54321/nexus",
        "NX_DB_ADMIN_USER": "nexus_admin",
        "NX_DB_ADMIN_PASS": "testpass",
        "NX_DB_URL": "jdbc:postgresql://127.0.0.1:54321/nexus",
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "svcpass",
        "NX_DB_DIAG_USER": "nexus_diag",
        "NX_DB_DIAG_PASS": "diagpass",
    }
    defaults.update(overrides)
    content = "\n".join(
        f"{k}={v}" for k, v in defaults.items() if v is not None
    ) + "\n"
    p = tmp_path / "pg_credentials"
    p.write_text(content)
    return p


def _diag_runner_counts(*per_statement: int):
    """A run_diagnostic_sql psql_runner seam (argv, env) -> CompletedProcess.

    Returns ``per_statement[i]`` for the i-th statement in order — the
    chash-conformance probe runs one count statement per chash-bearing table,
    summed. A single int broadcasts a per-table count; the poison total is the
    sum, matching the nexus_diag/BYPASSRLS 'sees every tenant's rows' path.
    """
    state = {"i": 0}

    def runner(argv, env):
        i = state["i"]
        state["i"] += 1
        val = per_statement[i] if i < len(per_statement) else 0
        return subprocess.CompletedProcess(argv, 0, stdout=f"{val}\n", stderr="")

    return runner


def _diag_runner_fail():
    def runner(argv, env):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    return runner


def _diag_runner_unparseable():
    def runner(argv, env):
        return subprocess.CompletedProcess(
            argv, 0, stdout="not-a-number\n", stderr="",
        )

    return runner


# ── _check_storage_service_health ────────────────────────────────────────────


class TestCheckStorageServiceHealth:
    """Unit tests for _check_storage_service_health — injected HTTP client."""

    def _fake_response(self, status_code: int, body: dict) -> MagicMock:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status_code
        resp.json.return_value = body
        return resp

    def test_up_returns_ok(self, tmp_path):
        """200 + db:up -> single ok HealthResult."""
        creds = _make_creds_file(tmp_path)

        def fake_http(url: str, timeout: float) -> httpx.Response:
            assert "/health" in url
            return self._fake_response(200, {"status": "ok", "db": "up"})

        results = _check_storage_service_health(
            creds_path=creds,
            endpoint=("127.0.0.1", 8080),
            http_get=fake_http,
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert r.fatal is False

    def test_503_returns_fatal(self, tmp_path):
        """503 (service reports db down) -> fatal HealthResult."""
        creds = _make_creds_file(tmp_path)

        def fake_http(url: str, timeout: float) -> httpx.Response:
            return self._fake_response(503, {"status": "error", "db": "down"})

        results = _check_storage_service_health(
            creds_path=creds,
            endpoint=("127.0.0.1", 8080),
            http_get=fake_http,
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True
        assert r.warn is False

    def test_connection_refused_returns_fatal(self, tmp_path):
        """Connection error -> fatal (not soft-warn) when endpoint is known."""
        creds = _make_creds_file(tmp_path)

        def fake_http(url: str, timeout: float) -> httpx.Response:
            raise httpx.ConnectError("refused")

        results = _check_storage_service_health(
            creds_path=creds,
            endpoint=("127.0.0.1", 8080),
            http_get=fake_http,
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True
        assert r.warn is False

    def test_no_pg_credentials_skips_with_soft_warn(self, tmp_path):
        """No pg_credentials file, LOCAL mode (pinned explicitly, per repo
        directive "pin is_local_mode in cloud-path tests" — never let
        ambient machine state decide) -> soft warn, not fatal, existing
        message."""
        missing_creds = tmp_path / "pg_credentials"  # does not exist

        with patch("nexus.config.is_local_mode", return_value=True):
            results = _check_storage_service_health(
                creds_path=missing_creds,
                endpoint=None,
                http_get=None,  # should never be called
            )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True  # soft warn
        assert r.fatal is False
        assert "pg_credentials absent" in r.detail

    def test_no_pg_credentials_managed_mode_names_server_side_contract(self, tmp_path):
        """nexus-g7ijj: a managed deployment has no local pg_credentials by
        design (the store operator holds those, nexus-y3wuu) — the skip
        detail must say so, not the misleading "not configured" message.
        Still ok=False/warn=True: a managed box's check was NOT performed
        from this client, and doctor's checkmark must never render for an
        unperformed check."""
        missing_creds = tmp_path / "pg_credentials"  # does not exist

        with patch("nexus.config.is_local_mode", return_value=False):
            results = _check_storage_service_health(
                creds_path=missing_creds,
                endpoint=None,
                http_get=None,  # should never be called
            )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert r.fatal is False
        assert "server-side" in r.detail
        assert "y3wuu" in r.detail
        assert "not configured" not in r.detail

    def test_endpoint_undiscoverable_soft_warn(self, tmp_path):
        """pg_credentials present but endpoint=None -> soft warn, not fatal."""
        creds = _make_creds_file(tmp_path)

        results = _check_storage_service_health(
            creds_path=creds,
            endpoint=None,
            http_get=None,  # should never be called
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert r.fatal is False

    def test_db_field_down_is_fatal(self, tmp_path):
        """200 but db:down -> fatal (service is degraded)."""
        creds = _make_creds_file(tmp_path)

        def fake_http(url: str, timeout: float) -> httpx.Response:
            return self._fake_response(200, {"status": "ok", "db": "down"})

        results = _check_storage_service_health(
            creds_path=creds,
            endpoint=("127.0.0.1", 8080),
            http_get=fake_http,
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True


# ── _check_engine_convergence (nexus-cfgo9) ─────────────────────────────────


class TestCheckEngineConvergence:
    """nx doctor backstop for the ONE-engine convergence model — reports
    drift as convergence-pending, never as a refusal/violation."""

    def test_not_applicable_yields_no_result(self, tmp_path):
        from nexus.upgrade_finish import EngineConvergence

        with patch(
            "nexus.upgrade_finish.detect_engine_convergence",
            return_value=EngineConvergence(
                applicable=False, installed_version=None,
                required_version=(0, 1, 43), converged=True,
                reason="cloud mode",
            ),
        ):
            results = _check_engine_convergence(config_dir=tmp_path)
        assert results == []

    def test_converged_returns_ok(self, tmp_path):
        from nexus.upgrade_finish import EngineConvergence

        with patch(
            "nexus.upgrade_finish.detect_engine_convergence",
            return_value=EngineConvergence(
                applicable=True, installed_version=(0, 1, 43),
                required_version=(0, 1, 43), converged=True, reason=None,
            ),
        ):
            results = _check_engine_convergence(config_dir=tmp_path)
        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].fatal is False

    def test_mismatch_returns_soft_warn_with_convergence_framing(self, tmp_path):
        from nexus.upgrade_finish import EngineConvergence

        with patch(
            "nexus.upgrade_finish.detect_engine_convergence",
            return_value=EngineConvergence(
                applicable=True, installed_version=(0, 1, 42),
                required_version=(0, 1, 43), converged=False,
                reason="installed engine v0.1.42 != required v0.1.43",
            ),
        ):
            results = _check_engine_convergence(config_dir=tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True  # convergence pending, not a hard violation
        assert r.fatal is False
        assert "0.1.42" in r.detail and "0.1.43" in r.detail
        assert "convergence" in r.detail.lower()
        assert "violation" not in r.detail.lower()
        assert r.fix_suggestions

    def test_probe_failure_degrades_silently(self, tmp_path):
        with patch(
            "nexus.upgrade_finish.detect_engine_convergence",
            side_effect=RuntimeError("boom"),
        ):
            results = _check_engine_convergence(config_dir=tmp_path)
        assert results == []


# ── _check_t2_launchagent_stray (nexus-c0vby, GH #1405 defect 2) ────────────


class TestCheckServiceLaunchagentStray:
    """nexus-6bmph (RDR-183 residual): a com.nexus.service autostart unit on a
    NON-local install launches the local engine against a config with no
    pg_credentials — launchd respawns the exit-2 process every
    ThrottleInterval forever (live evidence: 810 err lines in one morning on
    a cloud-mode box, 2026-07-22). Doctor surfaces it with the removal verb;
    the c0vby sibling for the SERVICE unit."""

    def test_local_mode_yields_no_result(self):
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed") as probe:
            results = _check_service_launchagent_stray()
        assert results == []
        probe.assert_not_called()

    def test_nonlocal_no_unit_returns_ok(self):
        with patch("nexus.config.is_local_mode", return_value=False), \
             patch("nexus.commands.daemon._service_autostart_unit_installed",
                   return_value=None):
            results = _check_service_launchagent_stray()
        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].fatal is False

    def test_nonlocal_with_unit_returns_soft_warn_naming_removal(self, tmp_path):
        dest = tmp_path / "com.nexus.service.plist"
        with patch("nexus.config.is_local_mode", return_value=False), \
             patch("nexus.commands.daemon._service_autostart_unit_installed",
                   return_value=dest):
            results = _check_service_launchagent_stray()
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert r.fatal is False
        assert str(dest) in r.detail
        assert any("nx daemon service uninstall --autostart" in f for f in r.fix_suggestions)

    def test_probe_failure_degrades_silently(self):
        with patch("nexus.config.is_local_mode", side_effect=RuntimeError("boom")):
            assert _check_service_launchagent_stray() == []


class TestCheckServiceAutostartDrift:
    """nexus-rlp0v (substantive-critic round 1, Significant): the `nx doctor`
    backstop for converge_service_autostart_unit's automatic-pass NOTE leg —
    a user who never runs `nx daemon restart-stale` must still be able to
    SEE a drifted (e.g. ProcessType=Background-vintage) autostart unit via
    plain `nx doctor`. Delegates to the SAME probe
    (nexus.upgrade_finish._probe_service_autostart_drift) the convergence
    leg itself uses -- patched here at the leaf calls it makes, exactly the
    same mocking surface as TestConvergeServiceAutostartUnit in
    tests/test_upgrade_finish.py."""

    def test_nonlocal_mode_yields_no_result(self):
        with patch("nexus.config.is_local_mode", return_value=False), \
             patch("nexus.commands.daemon._service_autostart_unit_installed") as probe:
            results = _check_service_autostart_drift()
        assert results == []
        probe.assert_not_called()

    def test_local_no_unit_installed_yields_no_result(self):
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed", return_value=None):
            results = _check_service_autostart_drift()
        assert results == []

    def test_local_content_matches_returns_ok(self, tmp_path):
        dest = tmp_path / "com.nexus.service.plist"
        dest.write_text("same content\n")
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed", return_value=dest), \
             patch("nexus.daemon.installer.rendered_unit_content",
                   return_value=(dest, "same content\n")):
            results = _check_service_autostart_drift()
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert r.warn is False
        assert r.fatal is False
        assert str(dest) in r.detail

    def test_local_content_drifted_returns_warn_naming_restart_stale(self, tmp_path):
        dest = tmp_path / "com.nexus.service.plist"
        dest.write_text("old content with ProcessType Background\n")
        with patch("nexus.config.is_local_mode", return_value=True), \
             patch("nexus.commands.daemon._service_autostart_unit_installed", return_value=dest), \
             patch("nexus.daemon.installer.rendered_unit_content",
                   return_value=(dest, "new content\n")):
            results = _check_service_autostart_drift()
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert r.fatal is False
        assert str(dest) in r.detail
        assert any("nx daemon restart-stale" in f for f in r.fix_suggestions)

    def test_probe_failure_degrades_silently(self):
        """Deliberately DIFFERENT from converge_service_autostart_unit's
        NEEDS-HUMAN contract: a doctor check degrades silently on a probe
        failure, matching every sibling check in this module
        (_check_engine_convergence, _check_service_launchagent_stray) —
        best-effort, must never break `nx doctor`."""
        with patch("nexus.config.is_local_mode", side_effect=RuntimeError("boom")):
            assert _check_service_autostart_drift() == []


class TestCheckT2LaunchagentStray:
    """nx doctor backstop for the automatic stray-com.nexus.t2-LaunchAgent
    removal — surfaces the condition even outside a version transition.

    CONTRACT FLIPPED at nexus-i711w Stage 2 sub-stage B, deliberately, and
    for the same reason `unload_stale_t2_launchagent`'s service-mode gate
    was removed: with the T2 daemon deleted, no box of any mode can start
    one or reinstall the unit, so a surviving unit is stray EVERYWHERE.
    The old `test_local_mode_yields_no_result` asserted the gate that left
    SQLite-mode boxes — the ones most likely to carry a unit — with silent
    auto-removal and no doctor visibility. Its replacement below pins the
    NEW contract. Do not restore the storage-mode gate.
    """

    def test_sqlite_mode_ALSO_reports_after_the_daemon_retired(
        self, tmp_path, monkeypatch
    ):
        """The flip. A SQLite-mode box with a unit must be TOLD, not skipped.

        The env pin is load-bearing, not decoration: conftest's autouse
        ``_pin_t2_substrate`` sets ``NX_STORAGE_BACKEND=service`` for the whole
        suite, so without it this test runs in SERVICE mode — where the OLD
        gate also fell through — and would stay green if someone restored the
        gate. Verified by mutation (sub-stage B critic pass).
        """
        monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
        dest = tmp_path / "com.nexus.t2.plist"
        with patch(
            "nexus.commands.daemon._autostart_unit_installed", return_value=dest,
        ):
            results = _check_t2_launchagent_stray()
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert str(dest) in r.detail

    def test_storage_mode_is_not_consulted_at_all(self, tmp_path):
        """Non-vacuity guard for the flip above: a re-introduced gate would
        have to call `storage_backend_for`, so assert nothing does."""
        with patch(
            "nexus.db.storage_mode.storage_backend_for",
        ) as backend, patch(
            "nexus.commands.daemon._autostart_unit_installed",
            return_value=tmp_path / "com.nexus.t2.plist",
        ):
            _check_t2_launchagent_stray()
        backend.assert_not_called()

    def test_no_agent_returns_ok(self):
        with patch(
            "nexus.commands.daemon._autostart_unit_installed", return_value=None,
        ):
            results = _check_t2_launchagent_stray()
        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].fatal is False

    def test_with_agent_returns_soft_warn(self, tmp_path):
        dest = tmp_path / "com.nexus.t2.plist"
        with patch(
            "nexus.commands.daemon._autostart_unit_installed", return_value=dest,
        ):
            results = _check_t2_launchagent_stray()
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True  # soft warning, never fatal (benign log noise)
        assert r.fatal is False
        assert str(dest) in r.detail
        assert r.fix_suggestions
        assert any("restart-stale" in s for s in r.fix_suggestions)

    def test_every_fix_suggestion_names_a_LIVE_verb(self, tmp_path):
        """The defect this replaces: `nx daemon t2 uninstall --autostart` was
        suggested here after the whole `t2` verb group was deleted."""
        from click.testing import CliRunner

        from nexus.cli import main as cli

        dest = tmp_path / "com.nexus.t2.plist"
        with patch(
            "nexus.commands.daemon._autostart_unit_installed", return_value=dest,
        ):
            results = _check_t2_launchagent_stray()
        suggestions = results[0].fix_suggestions
        assert suggestions, "a warn result with no fix is useless"
        for s in suggestions:
            argv = s.split("#", 1)[0].split()
            assert argv[0] == "nx"
            res = CliRunner().invoke(cli, [*argv[1:], "--help"])
            assert res.exit_code == 0, f"dead verb in fix_suggestions: {s!r}\n{res.output}"

    def test_probe_failure_degrades_silently(self):
        with patch(
            "nexus.commands.daemon._autostart_unit_installed",
            side_effect=RuntimeError("boom"),
        ):
            results = _check_t2_launchagent_stray()
        assert results == []


# ── _check_migration_state ────────────────────────────────────────────────────


def _psql_runner_ok(n: int):
    """Return a psql runner that reports N EXECUTED rows, 0 FAILED, 0 RERAN, 0 null-md5sum."""
    def runner(cmd: list[str], *, capture_output: bool, text: bool,
               check: bool) -> subprocess.CompletedProcess:
        sql = " ".join(cmd)
        if "length(chash)<>32" in sql:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0\n", stderr="")
        if "FILTER (WHERE exectype='FAILED')" in sql:
            # Drift query: 0 FAILED, 0 RERAN/other = all good
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0|0\n", stderr="")
        if "md5sum IS NULL" in sql:
            # Checksum gap query: 0 null md5sums = all good
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0\n", stderr="")
        # Total count query
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout=str(n) + "\n",
            stderr="",
        )
    return runner


def _psql_runner_with_failed():
    """Return a psql runner that reports 1 genuinely FAILED changeset."""
    def runner(cmd: list[str], *, capture_output: bool, text: bool,
               check: bool) -> subprocess.CompletedProcess:
        sql = " ".join(cmd)
        if "FILTER (WHERE exectype='FAILED')" in sql:
            # Drift query: 1 FAILED, 0 RERAN
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="1|0\n", stderr="")
        if "md5sum IS NULL" in sql:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0\n", stderr="")
        # Total count query: 5 rows total
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="5\n", stderr="")
    return runner


def _psql_runner_with_reran_only():
    """Return a psql runner that reports 2 benign RERAN changesets, 0 FAILED
    (e.g. runOnChange grant changesets reapplied after a checksum change)."""
    def runner(cmd: list[str], *, capture_output: bool, text: bool,
               check: bool) -> subprocess.CompletedProcess:
        sql = " ".join(cmd)
        if "length(chash)<>32" in sql:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0\n", stderr="")
        if "FILTER (WHERE exectype='FAILED')" in sql:
            # Drift query: 0 FAILED, 2 RERAN
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0|2\n", stderr="")
        if "md5sum IS NULL" in sql:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0\n", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="5\n", stderr="")
    return runner


def _psql_runner_with_null_md5():
    """Return a psql runner that reports an EXECUTED row with NULL md5sum."""
    def runner(cmd: list[str], *, capture_output: bool, text: bool,
               check: bool) -> subprocess.CompletedProcess:
        sql = " ".join(cmd)
        if "md5sum IS NULL" in sql:
            # 1 row has null md5sum
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="1\n", stderr="")
        if "FILTER (WHERE exectype='FAILED')" in sql:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0|0\n", stderr="")
        # Total count
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="5\n", stderr="")
    return runner


def _psql_runner_unparseable_drift():
    """Return a psql runner where the drift query returns non-integer output."""
    def runner(cmd: list[str], *, capture_output: bool, text: bool,
               check: bool) -> subprocess.CompletedProcess:
        sql = " ".join(cmd)
        if "FILTER (WHERE exectype='FAILED')" in sql:
            # Unparseable output
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="not-a-number\n", stderr="",
            )
        if "md5sum IS NULL" in sql:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0\n", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="5\n", stderr="")
    return runner


def _psql_runner_no_table():
    """Return a psql runner that simulates missing databasechangelog."""
    def runner(cmd: list[str], *, capture_output: bool, text: bool,
               check: bool) -> subprocess.CompletedProcess:
        # Simulate table-not-found error
        return subprocess.CompletedProcess(
            args=cmd, returncode=1,
            stdout="",
            stderr='ERROR:  relation "databasechangelog" does not exist',
        )
    return runner


class TestCheckMigrationState:
    """Unit tests for _check_migration_state — injected psql runner."""

    def test_no_pg_credentials_local_mode_keeps_existing_message(self, tmp_path):
        """nexus-g7ijj: fills a coverage gap — this check had NO missing-
        creds skip test at all before this bead. LOCAL mode pinned
        explicitly (never ambient) -> the original "not configured"
        message, soft warn, not fatal."""
        missing_creds = tmp_path / "pg_credentials"  # does not exist

        with patch("nexus.config.is_local_mode", return_value=True):
            results = _check_migration_state(creds_path=missing_creds)
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert not r.fatal
        assert "pg_credentials absent" in r.detail

    def test_no_pg_credentials_managed_mode_names_server_side_contract(self, tmp_path):
        """nexus-g7ijj: managed deployment -> server-side contract detail
        (nexus-y3wuu), never the misleading "not configured" message.
        ok=False/warn=True preserved — the check still was not performed."""
        missing_creds = tmp_path / "pg_credentials"  # does not exist

        with patch("nexus.config.is_local_mode", return_value=False):
            results = _check_migration_state(creds_path=missing_creds)
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert not r.fatal
        assert "server-side" in r.detail
        assert "y3wuu" in r.detail
        assert "not configured" not in r.detail

    def test_all_executed_returns_ok(self, tmp_path):
        """All EXECUTED rows + a clean (0-poison) chash probe -> ok result."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_migration_state(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_runner_ok(7),
            diag_runner=_diag_runner_counts(0),  # 0 nonconforming per table
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert "7" in r.detail

    def test_legacy_chash_rows_warn_not_fatal(self, tmp_path):
        """nexus-pnwu0 / GH #1414: width-non-conformant chash rows -> a
        WARNING steering the upgrade-ladder heal (nexus-o513u ladder-first;
        the old 'Do NOT upgrade the engine / will crash-loop' claim was
        disproven for v0.1.48+ by nexus-joima), plus the still-ok Schema
        migrations result. Never fatal (the serving engine tolerates the
        rows). The count is SUMMED across the chash-bearing tables via
        nexus_diag."""
        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(5),
            diag_runner=_diag_runner_counts(9, 3),  # 9 + 3 = 12 across tables
        )
        labels = {r.label for r in results}
        assert "Chunk chash conformance" in labels
        chash = next(r for r in results if r.label == "Chunk chash conformance")
        assert chash.ok is False
        assert chash.warn is True
        assert chash.fatal is False
        assert "12" in chash.detail
        # nexus-2hklz (round-3 critique HIGH-1): the falling-count guidance
        # must scope the healing claim to re-indexing — deletions also
        # lower the count, so no blanket "shrinking = healing" claim.
        assert "Re-indexing affected content HEALS" in chash.detail
        assert "heal-by-replacement" in chash.detail
        assert "deleting affected content also lowers it" in chash.detail
        assert "not data loss" not in chash.detail
        # nexus-o513u: heal steps lead; no unconditional do-not-upgrade
        # gate; rollback only as the will-not-boot branch. The chash-rekey
        # rung was deleted at 5a3dcd16c, so re-indexing is the ONLY remedy
        # — these asserts must never again name a rung that cannot run
        # (default_registry() == [] and RUNG_ORDER == ()).
        assert any("nx index repo" in s for s in chash.fix_suggestions)
        assert not any("nx upgrade" in s for s in chash.fix_suggestions)
        assert not any("chash-rekey" in s for s in chash.fix_suggestions)
        assert not any("Do NOT upgrade" in s for s in chash.fix_suggestions)
        # the runbook rewrite replaced numbered headings with prose ones
        assert any(
            "If the service will not start" in s for s in chash.fix_suggestions
        )
        # RDR-155 P4b: the --rollback verb died with the migration machinery;
        # the will-not-boot branch now names the pinned-release redirect.
        pinned = [s for s in chash.fix_suggestions if "LAST_MIGRATION_CAPABLE" in s]
        assert pinned and all("will-not-boot" in s for s in pinned)
        # the migration result itself is still healthy (box works now)
        assert any(r.label == "Schema migrations" and r.ok for r in results)

    def test_chash_probe_runs_on_the_nexus_diag_path(self, tmp_path):
        """nexus-vounk: the chash counts go through the nexus_diag credentials
        (BYPASSRLS), NOT the admin psql_runner — proving the RLS-vacuous admin
        path is retired. The admin runner returns 0 for chash SQL; if the leg
        still used it, poison would read as clean. It doesn't."""
        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            # admin runner would report 0 for chash (the vacuous RLS result):
            psql_runner=_psql_runner_ok(5),
            # but the diag path sees the real rows:
            diag_runner=_diag_runner_counts(7),
        )
        chash = next(r for r in results if r.label == "Chunk chash conformance")
        assert chash.ok is False and chash.warn is True
        assert "7" in chash.detail

    def test_missing_diag_role_degrades_to_warn_not_clean(self, tmp_path):
        """nexus-vounk: a pre-P2.1 install (no NX_DB_DIAG_* keys) cannot run
        the probe — it must WARN 'could not run', never a false clean."""
        creds = _make_creds_file(
            tmp_path, NX_DB_DIAG_USER=None, NX_DB_DIAG_PASS=None,
        )
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(5),
        )
        chash = next(r for r in results if r.label == "Chunk chash conformance")
        assert chash.warn is True and chash.fatal is False
        assert "could NOT run" in chash.detail or "not run" in chash.detail
        assert "clean" in chash.detail.lower()  # explicit "do not read as clean"
        assert any(r.label == "Schema migrations" and r.ok for r in results)

    def test_chash_unparseable_output_warns_not_silent(self, tmp_path):
        """returncode==0 with non-numeric stdout must NOT silently read as
        clean — it surfaces a non-fatal warn."""
        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(5),
            diag_runner=_diag_runner_unparseable(),
        )
        chash = next(r for r in results if r.label == "Chunk chash conformance")
        assert chash.warn is True
        assert chash.fatal is False
        assert "did not run" in chash.detail
        assert any(r.label == "Schema migrations" and r.ok for r in results)

    def test_conformant_chash_adds_no_extra_result(self, tmp_path):
        """0 nonconforming rows -> only the Schema migrations result."""
        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(5),
            diag_runner=_diag_runner_counts(0),
        )
        assert [r.label for r in results] == ["Schema migrations"]

    def test_chash_probe_failure_degrades_to_warn(self, tmp_path):
        """A failing chash probe (missing table on a schema variant) is a
        non-fatal warn, never a false poison alarm."""
        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(5),
            diag_runner=_diag_runner_fail(),
        )
        chash = next(r for r in results if r.label == "Chunk chash conformance")
        assert chash.warn is True
        assert chash.fatal is False
        assert any(r.label == "Schema migrations" and r.ok for r in results)

    def test_zero_rows_returns_fatal(self, tmp_path):
        """Zero rows in databasechangelog -> fatal (not migrated at all)."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_migration_state(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_runner_ok(0),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True

    def test_failed_row_returns_fatal(self, tmp_path):
        """A genuinely FAILED changeset -> fatal migration drift."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_migration_state(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_runner_with_failed(),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True
        assert "FAILED" in r.detail

    def test_reran_only_returns_ok_not_fatal(self, tmp_path):
        """nexus incident 2026-07-01: benign RERAN changesets (e.g. a
        runOnChange grant reapplied after a checksum change) with 0 FAILED
        must pass, not be reported as a hard fail indistinguishable from
        real corruption."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_migration_state(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_runner_with_reran_only(),
            diag_runner=_diag_runner_counts(0),  # reaches the chash leg; clean
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert r.fatal is False
        assert "RERAN" in r.detail

    def test_missing_table_returns_fatal(self, tmp_path):
        """databasechangelog table missing -> fatal."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_migration_state(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_runner_no_table(),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True

    def test_null_md5sum_returns_fatal(self, tmp_path):
        """EXECUTED row with NULL md5sum -> fatal (Liquibase will fail on next boot)."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_migration_state(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_runner_with_null_md5(),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True
        assert "md5sum" in r.detail.lower() or "checksum" in r.detail.lower()

    def test_unparseable_drift_output_returns_fatal(self, tmp_path):
        """Non-integer drift query output -> fatal with a clear message (not '-1 changeset(s)')."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_migration_state(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_runner_unparseable_drift(),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True
        # Must NOT say "-1 changeset(s)" — that was the pre-fix nonsensical message.
        assert "-1" not in r.detail
        assert "unexpected output" in r.detail.lower() or "unparseable" in r.detail.lower() or "unexpected" in r.detail.lower()

    def test_no_credentials_soft_warn(self, tmp_path):
        """No pg_credentials -> soft warn, skip check."""
        missing = tmp_path / "pg_credentials"

        results = _check_migration_state(
            creds_path=missing,
            psql_bin=Path("/fake/psql"),
            psql_runner=None,
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert r.fatal is False


# ── _check_rls_present ────────────────────────────────────────────────────────

# The authoritative table list (schema.table) from the changelog baselines.
# Any table missing RLS should produce a FATAL result.
# Must be kept in sync with _RLS_TENANT_TABLES in health.py — the structural
# cross-walk test below enforces this via XML grep.
_ALL_TENANT_TABLES = [
    "nexus.aspect_extraction_queue",
    "nexus.aspect_promotion_log",
    "nexus.catalog_collections",
    "nexus.catalog_document_chunks",
    "nexus.catalog_documents",
    "nexus.catalog_links",
    "nexus.catalog_meta",
    "nexus.catalog_owners",
    # ("nexus.chash_index" removed — RDR-187/nexus-piwya.9: dropped table,
    # mirrors health._RLS_TENANT_TABLES)
    # ("nexus.chash_alias" removed — nexus-lgdel.l1: dropped table
    # (legacy-001-drop-chash-alias.xml), mirrors health._RLS_TENANT_TABLES)
    "nexus.chash_remap",
    "nexus.chunks",  # RDR-191 Phase 4 (nexus-o8dil.51)
    "nexus.claude_assisted_remediation_consents",
    "nexus.document_aspects",
    "nexus.document_highlights",
    "nexus.frecency",
    "nexus.gc_audit",  # nexus-jqvzk: destructive-T3-op audit record (catalog-018)
    "nexus.hook_failures",
    "nexus.ladder_completions",
    "nexus.memory",
    # ("nexus.migration_jobs" removed — nexus-tk070.p5b, reworked
    # 2026-08-20: dead table dropped (migration-002-tenant-pk.xml),
    # mirrors health._RLS_TENANT_TABLES)
    "nexus.nx_answer_runs",
    # RDR-196 .p1c (nexus-nyry9.9, telemetry-007-1/-2): per-step child of
    # nx_answer_runs, RLS enabled+forced like its parent (mirrors
    # health._RLS_TENANT_TABLES)
    "nexus.nx_answer_steps",
    "nexus.pdf_chunks",
    "nexus.pdf_pages",
    "nexus.pdf_pipeline",
    "nexus.plans",
    "nexus.relevance_log",
    "nexus.retention_markers",
    "nexus.search_telemetry",
    "nexus.taxonomy_centroids",  # RDR-191 Phase 4 (nexus-o8dil.51/.47)
    "nexus.taxonomy_meta",
    "nexus.tier_writes",
    "nexus.topic_assignments",
    "nexus.topic_links",
    "nexus.topics",
    "t1.scratch",
]


def _rls_row(schema_table: str, rls_on: str, rls_force: str, policy_count: int) -> str:
    """Format a psql RLS output row: schema|table|relrowsecurity|relforcerowsecurity|policy_count."""
    schema, _, table = schema_table.partition(".")
    return f"{schema}|{table}|{rls_on}|{rls_force}|{policy_count}"


def _psql_rls_all_ok():
    """Runner that reports all tables have RLS enabled + forced + policies present.

    Output format matches the 5-column SELECT in _check_rls_present:
    schema_name|table_name|relrowsecurity|relforcerowsecurity|policy_count
    Rows are returned in sorted order (ORDER BY schema, table) so the
    implementation's dict-based lookup resolves them correctly.
    """
    sorted_tables = sorted(_ALL_TENANT_TABLES)

    def runner(cmd: list[str], *, capture_output: bool, text: bool,
               check: bool) -> subprocess.CompletedProcess:
        rows = [_rls_row(st, "t", "t", 2) for st in sorted_tables]
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="\n".join(rows) + "\n", stderr="",
        )
    return runner


def _psql_rls_one_table_disabled(disabled_schema_table: str):
    """Runner that reports one specific table has RLS disabled.

    NON-VACUOUS negative test: the disabled table returns 'f|f|0';
    all others return 't|t|2'. Rows are sorted (ORDER BY schema, table) as
    the real psql would return them.
    """
    sorted_tables = sorted(_ALL_TENANT_TABLES)

    def runner(cmd: list[str], *, capture_output: bool, text: bool,
               check: bool) -> subprocess.CompletedProcess:
        rows = []
        for st in sorted_tables:
            if st == disabled_schema_table:
                rows.append(_rls_row(st, "f", "f", 0))
            else:
                rows.append(_rls_row(st, "t", "t", 2))
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="\n".join(rows) + "\n",
            stderr="",
        )
    return runner


def _psql_rls_one_table_absent(absent_schema_table: str):
    """Runner that reports one specific listed table as ABSENT from pg_class
    (the LEFT JOIN NULL case: relrowsecurity/relforcerowsecurity/policy_count
    all come back empty, since ``psql -t -A`` prints a NULL as ''). Every
    real pg_class row always has relrowsecurity 't' or 'f' -- never NULL --
    so an all-empty row can ONLY mean the driving VALUES row found no match.

    nexus-o8dil.51 defect 2 regression harness: against the pre-fix
    ``_check_rls_present`` (which never distinguished this from "RLS
    explicitly disabled"), this runner made the check report FATAL forever
    for a table that simply hadn't been migrated yet (or, post RDR-191, a
    dropped per-dim shard) -- the permanent-false-FATAL bug this bead fixes.
    """
    sorted_tables = sorted(_ALL_TENANT_TABLES)

    def runner(cmd: list[str], *, capture_output: bool, text: bool,
               check: bool) -> subprocess.CompletedProcess:
        rows = []
        for st in sorted_tables:
            if st == absent_schema_table:
                # relrowsecurity/relforcerowsecurity are NULL (LEFT JOIN
                # miss on pg_class) -> '' in -t -A output; policy_count is
                # COUNT(p.policyname), an aggregate that is always an
                # integer (0 here), never NULL.
                rows.append(_rls_row(st, "", "", 0))
            else:
                rows.append(_rls_row(st, "t", "t", 2))
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="\n".join(rows) + "\n",
            stderr="",
        )
    return runner


def _psql_rls_no_policies(table: str):
    """Runner where a table has RLS enabled but NO policies (policy_count=0)."""
    sorted_tables = sorted(_ALL_TENANT_TABLES)

    def runner(cmd: list[str], *, capture_output: bool, text: bool,
               check: bool) -> subprocess.CompletedProcess:
        rows = []
        for st in sorted_tables:
            if st == table:
                rows.append(_rls_row(st, "t", "t", 0))  # RLS enabled but no policies
            else:
                rows.append(_rls_row(st, "t", "t", 2))
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="\n".join(rows) + "\n",
            stderr="",
        )
    return runner


class TestCheckRlsPresent:
    """Unit tests for _check_rls_present — injected psql runner.

    The negative tests are NON-VACUOUS: they produce fatal results when
    specific RLS conditions are violated.
    """

    def test_no_pg_credentials_local_mode_keeps_existing_message(self, tmp_path):
        """nexus-g7ijj: fills a coverage gap — this check had NO missing-
        creds skip test at all before this bead. LOCAL mode pinned
        explicitly (never ambient) -> the original "not configured"
        message, soft warn, not fatal."""
        missing_creds = tmp_path / "pg_credentials"  # does not exist

        with patch("nexus.config.is_local_mode", return_value=True):
            results = _check_rls_present(creds_path=missing_creds)
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert not r.fatal
        assert "pg_credentials absent" in r.detail

    def test_no_pg_credentials_managed_mode_names_server_side_contract(self, tmp_path):
        """nexus-g7ijj: managed deployment -> server-side contract detail
        (nexus-y3wuu), never the misleading "not configured" message.
        ok=False/warn=True preserved — the check still was not performed."""
        missing_creds = tmp_path / "pg_credentials"  # does not exist

        with patch("nexus.config.is_local_mode", return_value=False):
            results = _check_rls_present(creds_path=missing_creds)
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert not r.fatal
        assert "server-side" in r.detail
        assert "y3wuu" in r.detail
        assert "not configured" not in r.detail

    def test_all_tables_rls_ok(self, tmp_path):
        """All tables have RLS enabled, forced, and policies -> ok."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_rls_present(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_rls_all_ok(),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert r.fatal is False
        # Should mention the count of tables
        assert str(len(_ALL_TENANT_TABLES)) in r.detail

    def test_rls_disabled_on_memory_is_fatal(self, tmp_path):
        """nexus.memory with RLS disabled -> FATAL (non-vacuous negative test)."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_rls_present(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_rls_one_table_disabled("nexus.memory"),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True  # SECURITY canary: must be fatal
        assert r.warn is False
        assert "nexus.memory" in r.detail or "memory" in r.detail.lower()

    def test_rls_disabled_on_plans_is_fatal(self, tmp_path):
        """nexus.plans with RLS disabled -> FATAL (non-vacuous negative test)."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_rls_present(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_rls_one_table_disabled("nexus.plans"),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True

    def test_rls_disabled_on_scratch_is_fatal(self, tmp_path):
        """t1.scratch with RLS disabled -> FATAL (non-vacuous negative test)."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_rls_present(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_rls_one_table_disabled("t1.scratch"),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True

    def test_rls_missing_policies_is_fatal(self, tmp_path):
        """Table has RLS flag set but NO policies -> FATAL (policy_count=0).

        RLS enabled without policies = open to all (or none) depending on config.
        This is the 'policy drop' negative test.
        """
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_rls_present(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_rls_no_policies("nexus.memory"),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True

    def test_no_credentials_soft_warn(self, tmp_path):
        """No pg_credentials -> soft warn, skip check."""
        missing = tmp_path / "pg_credentials"

        results = _check_rls_present(
            creds_path=missing,
            psql_bin=Path("/fake/psql"),
            psql_runner=None,
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert r.fatal is False

    # ── nexus-o8dil.51 defect 2: absent listed table must not be a false FATAL ──

    def test_absent_table_is_not_fatal_and_not_a_silent_pass(self, tmp_path):
        """A listed-but-absent table (LEFT JOIN NULL) is its OWN reported
        outcome: ok=False (so it's visible, not folded into a clean pass),
        fatal=False (never a permanent false FATAL -- the chash_index
        precedent), warn=True.

        Against the pre-fix implementation (which only ever compared
        rls_on/rls_force against 't', treating '' the same as 'f') this
        exact fixture produced a FATAL result -- the bug nexus-o8dil.51
        defect 2 describes.
        """
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_rls_present(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_rls_one_table_absent("nexus.chunks"),
        )
        assert len(results) == 1
        r = results[0]
        assert r.fatal is False, "an absent listed table must never be FATAL"
        assert r.ok is False, "absence must not be folded into a silent clean pass"
        assert r.warn is True
        assert "nexus.chunks" in r.detail

    def test_absent_table_message_distinguishes_from_disabled_rls(self, tmp_path):
        """The absent-table detail must not read as 'RLS not enabled' --
        that phrase is reserved for a table that EXISTS with bad RLS."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_rls_present(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_rls_one_table_absent("nexus.taxonomy_centroids"),
        )
        r = results[0]
        assert "RLS not enabled" not in r.detail
        assert "not yet present" in r.detail

    def test_chunks_rls_disabled_is_fatal(self, tmp_path):
        """nexus.chunks EXISTS with RLS disabled -> FATAL, non-vacuous:
        proves the check covers the table the RLS doctor never listed
        before nexus-o8dil.51 (defect 1)."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_rls_present(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_rls_one_table_disabled("nexus.chunks"),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True
        assert r.warn is False
        assert "nexus.chunks" in r.detail

    def test_taxonomy_centroids_rls_disabled_is_fatal(self, tmp_path):
        """nexus.taxonomy_centroids EXISTS with RLS disabled -> FATAL,
        same coverage proof as nexus.chunks above (RDR-191 .47 "one era")."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_rls_present(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_rls_one_table_disabled("nexus.taxonomy_centroids"),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True

    def test_mixed_absent_and_disabled_reports_fatal_for_the_disabled_table(self, tmp_path):
        """A table with bad RLS stays FATAL even when a DIFFERENT listed
        table is simultaneously absent -- absence must never mask a real
        failure elsewhere, and a real failure must not suppress the
        absent-table note."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")
        sorted_tables = sorted(_ALL_TENANT_TABLES)

        def runner(cmd, *, capture_output, text, check):
            rows = []
            for st in sorted_tables:
                if st == "nexus.chunks":
                    rows.append(_rls_row(st, "", "", 0))  # absent
                elif st == "nexus.memory":
                    rows.append(_rls_row(st, "f", "f", 0))  # present, RLS off
                else:
                    rows.append(_rls_row(st, "t", "t", 2))
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="\n".join(rows) + "\n", stderr="",
            )

        results = _check_rls_present(creds_path=creds, psql_bin=psql, psql_runner=runner)
        assert len(results) == 1
        r = results[0]
        assert r.fatal is True
        assert r.ok is False
        assert "nexus.memory" in r.detail
        assert "nexus.chunks" in r.detail  # noted, not silently dropped

    def test_psql_error_returns_fatal(self, tmp_path):
        """psql failure (non-zero returncode) -> fatal."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        def broken_runner(cmd: list[str], *, capture_output: bool, text: bool,
                          check: bool) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1,
                stdout="",
                stderr="ERROR: connection refused",
            )

        results = _check_rls_present(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=broken_runner,
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True

    def test_rls_disabled_on_catalog_meta_is_fatal(self, tmp_path):
        """nexus.catalog_meta with RLS disabled -> FATAL (was missing from original list)."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_rls_present(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_rls_one_table_disabled("nexus.catalog_meta"),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True

    def test_rls_disabled_on_frecency_is_fatal(self, tmp_path):
        """nexus.frecency with RLS disabled -> FATAL (was missing from original list)."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_rls_present(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_rls_one_table_disabled("nexus.frecency"),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True

    def test_rls_disabled_on_any_tenant_table_is_fatal(self, tmp_path):
        """Parameterized: RLS off on ANY tenant table -> fatal.

        Exercises the full table list to ensure no table can silently skip
        the check.
        """
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        # Test a sampling of tables covering the two previously-missing ones and others
        sample_tables = [
            "nexus.document_aspects",
            "nexus.catalog_documents",
            "nexus.catalog_meta",
            "nexus.frecency",
            "nexus.tier_writes",
            "nexus.hook_failures",
        ]
        for table in sample_tables:
            results = _check_rls_present(
                creds_path=creds,
                psql_bin=psql,
                psql_runner=_psql_rls_one_table_disabled(table),
            )
            assert len(results) == 1
            r = results[0]
            assert r.ok is False, f"Expected fatal for {table} with RLS disabled"
            assert r.fatal is True, f"Expected fatal=True for {table} with RLS disabled"


# ── Structural guard: changelog cross-walk ────────────────────────────────────


class TestRlsTableCompleteness:
    """Structural guard: _RLS_TENANT_TABLES must equal the set of tables with
    ENABLE ROW LEVEL SECURITY in the Liquibase changelog XMLs.

    This test greps the actual XML files at test time so any new changelog
    baseline that adds RLS to a new table will fail loudly here, prompting
    the developer to update _RLS_TENANT_TABLES.

    NON-VACUOUS: removing a table from _RLS_TENANT_TABLES while the XMLs
    still have ENABLE ROW LEVEL SECURITY for it will cause this test to fail.
    """

    _CHANGELOG_DIR = (
        Path(__file__).resolve().parent.parent
        / "service" / "src" / "main" / "resources" / "db" / "changelog"
    )

    def _extract_rls_tables_from_xmls(self) -> frozenset[str]:
        """Grep all *.xml in the changelog dir for ENABLE ROW LEVEL SECURITY,
        extract schema.table names."""
        import re
        pattern = re.compile(
            r"ALTER\s+TABLE\s+((nexus|t1)\.[a-z_]+)\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY"
        )
        found: set[str] = set()
        for xml_path in self._CHANGELOG_DIR.glob("*.xml"):
            text = xml_path.read_text(encoding="utf-8")
            for m in pattern.finditer(text):
                found.add(m.group(1))
        # Tables DROPPED by a later changeset: the ENABLE ROW LEVEL SECURITY
        # line is immutable history, but the live _check_rls_present probe
        # must not expect a dropped table (a listed-but-dropped table is a
        # permanent false FATAL — RDR-187 .9 review High). One entry per
        # retirement, with the dropping changeset named.
        dropped = {
            "nexus.chash_index",  # rdr187-001-drop-chash-index.xml (RDR-187)
            "nexus.chash_alias",  # legacy-001-drop-chash-alias.xml (nexus-lgdel.l1)
            "nexus.migration_jobs",  # migration-002-tenant-pk.xml, reworked (nexus-tk070.p5b)
        }
        return frozenset(found - dropped)

    def test_rls_tenant_tables_matches_changelogs(self):
        """_RLS_TENANT_TABLES equals the set of RLS tables found in XMLs.

        Fails if:
        - A changelog adds ENABLE ROW LEVEL SECURITY to a new table but
          _RLS_TENANT_TABLES is not updated (table in XMLs but not in tuple).
        - _RLS_TENANT_TABLES lists a table that no XML grants RLS on
          (table in tuple but not in XMLs).
        """
        from nexus.health import _RLS_TENANT_TABLES

        if not self._CHANGELOG_DIR.exists():
            pytest.skip(
                f"changelog dir not found: {self._CHANGELOG_DIR}; "
                "cannot run structural guard"
            )

        xml_tables = self._extract_rls_tables_from_xmls()
        assert xml_tables, (
            f"No ENABLE ROW LEVEL SECURITY statements found in {self._CHANGELOG_DIR}; "
            "check that the XML files are present and parseable"
        )

        impl_tables = frozenset(_RLS_TENANT_TABLES)

        missing_from_impl = xml_tables - impl_tables
        extra_in_impl = impl_tables - xml_tables

        errors = []
        if missing_from_impl:
            errors.append(
                f"Tables in XMLs but MISSING from _RLS_TENANT_TABLES "
                f"(canary has a hole): {sorted(missing_from_impl)}"
            )
        if extra_in_impl:
            errors.append(
                f"Tables in _RLS_TENANT_TABLES but NOT in XMLs "
                f"(phantom entries): {sorted(extra_in_impl)}"
            )

        assert not errors, "\n".join(errors)

    def test_cross_walk_fails_if_table_removed_from_impl(self):
        """Non-vacuous: removing nexus.memory from _RLS_TENANT_TABLES while
        the XMLs still have it -> the cross-walk detects a hole.

        This test directly exercises the guard logic rather than the production
        constant so it is independent of _RLS_TENANT_TABLES correctness.
        """
        if not self._CHANGELOG_DIR.exists():
            pytest.skip("changelog dir not found")

        xml_tables = self._extract_rls_tables_from_xmls()
        # Simulate a tuple that is missing nexus.memory
        impl_with_hole = frozenset(xml_tables - {"nexus.memory"})

        missing = xml_tables - impl_with_hole
        assert "nexus.memory" in missing, (
            "Expected cross-walk to detect nexus.memory as missing from impl"
        )


# ── RDR-160 nexus-gzqvg: service bge-768 model doctor check ────────────────────


class TestServiceBgeModelCheck:
    """`_check_service_bge_model` — fires ONLY for a local service install."""

    def _setup(self, tmp_path, monkeypatch, *, creds: bool, model: bool, truncated: bool = False):
        from nexus.db import service_bge_model as sbm

        cfg = tmp_path / "cfg"
        cfg.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
        if creds:
            (cfg / "pg_credentials").write_text("PG_PORT=15432\n")

        bge = tmp_path / "bge"
        monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(bge))
        monkeypatch.setattr(sbm, "_MIN_MODEL_BYTES", 4)
        monkeypatch.setattr(sbm, "_MIN_TOKENIZER_BYTES", 1)
        if model:
            bge.mkdir(parents=True)
            (bge / "model.onnx").write_bytes(b"x" if truncated else b"MODEL")
            (bge / "tokenizer.json").write_bytes(b"T")

    def test_not_service_install_returns_nothing(self, tmp_path, monkeypatch):
        from nexus.health import _check_service_bge_model
        self._setup(tmp_path, monkeypatch, creds=False, model=False)
        assert _check_service_bge_model() == []

    def test_service_with_model_present_ok(self, tmp_path, monkeypatch):
        from nexus.health import _check_service_bge_model
        self._setup(tmp_path, monkeypatch, creds=True, model=True)
        res = _check_service_bge_model()
        assert len(res) == 1
        assert res[0].ok is True
        assert "present" in res[0].detail

    def test_service_with_model_missing_is_soft_warn_with_remedy(self, tmp_path, monkeypatch):
        # SOFT warn (not fatal): surfaces the gap without red-X-ing doctor for a
        # mid-setup user; the Bge768Embedder boot preflight is the hard gate.
        from nexus.health import _check_service_bge_model
        self._setup(tmp_path, monkeypatch, creds=True, model=False)
        res = _check_service_bge_model()
        assert len(res) == 1
        assert res[0].ok is False and res[0].warn is True and res[0].fatal is False
        assert "will not boot" in res[0].detail
        assert any("nx init --service" in s for s in res[0].fix_suggestions)

    def test_service_with_truncated_model_is_flagged(self, tmp_path, monkeypatch):
        # below the size floor → "incomplete", treated as not-present
        from nexus.health import _check_service_bge_model
        self._setup(tmp_path, monkeypatch, creds=True, model=True, truncated=True)
        res = _check_service_bge_model()
        assert len(res) == 1 and res[0].ok is False and res[0].warn is True

    def test_present_model_is_not_fatal_or_warn(self, tmp_path, monkeypatch):
        from nexus.health import _check_service_bge_model
        self._setup(tmp_path, monkeypatch, creds=True, model=True)
        res = _check_service_bge_model()
        assert res[0].ok is True and res[0].fatal is False and res[0].warn is False


# ── Amendment A6 fallback (review 47dcb65e Critical) ──────────────────────────


class TestChashProbeViewFallback:
    """The view-era probe falls back to the legacy direct-table statements
    ONLY on execution failure (pre-A6 engine); a LINT violation is a product
    defect and must surface as the WARN, never a silent legacy retry."""

    def test_view_failure_falls_back_to_legacy_and_counts(self, tmp_path):
        # Poison subset only: the gate statements (and their legacy fallback)
        # deliberately exclude the nexus-z5j0t debt tables, and the debt
        # probe is skipped entirely when the view path failed.
        n = len(chash_tables.POISON_CHASH_TABLES)
        state = {"i": 0}

        def runner(argv, env):
            i = state["i"]
            state["i"] += 1
            if i == 0:
                # run_diagnostic_sql aborts on the FIRST failing statement,
                # so exactly one view-era call precedes the fallback.
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="relation does not exist",
                )
            return subprocess.CompletedProcess(argv, 0, stdout="2\n", stderr="")

        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),  # hermetic: never ambient discovery
            psql_runner=_psql_runner_ok(160),
            diag_runner=runner,
        )
        chash = [r for r in results if "chash" in r.label.lower()]
        assert chash and chash[0].ok is False and chash[0].warn is True
        assert "4 chunk row(s)" in chash[0].detail  # 2 per table via LEGACY (2 chash-bearing tables post-RDR-191 unify)
        assert state["i"] == 1 + n  # one failed view call + the full legacy set

    def test_debt_over_zero_emits_nongating_warn(self, tmp_path):
        """critic-180-foundation finding 1 coverage: a positive debt count
        surfaces as a WARN under its own label, never gating."""
        # 4 poison statements return 0 (clean; post-RDR-187 set), then 3
        # debt statements return 2 each -> debt 6. Deriving the mock-call
        # count from the registry is ALIGNMENT MECHANICS only — the
        # cardinality pin itself is hardcoded in test_diag_conformance_view
        # (explicit 4-tuple + chash_index-never-returns assertion).
        counts = [0] * len(chash_tables.POISON_CHASH_TABLES) + [2, 2, 2]
        state = {"i": 0}

        def runner(argv, env):
            i = state["i"]; state["i"] += 1
            val = counts[i] if i < len(counts) else 0
            return subprocess.CompletedProcess(argv, 0, stdout=f"{val}\n", stderr="")

        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(160),
            diag_runner=runner,
        )
        debt = [r for r in results if r.label == "Chash legacy debt"]
        assert len(debt) == 1
        assert debt[0].ok is False and debt[0].warn is True and debt[0].fatal is False
        assert "6" in debt[0].detail
        # the poison gate result must stay clean/absent (no cross-count)
        poison = [r for r in results if r.label == "Chunk chash conformance"]
        assert not poison  # zero poison rows -> no poison result emitted

    def test_debt_probe_failure_surfaces_unknown_never_silent(self, tmp_path):
        """critic-180-foundation finding 1: a stale 5-leg view NULLs the debt
        sums (empty psql lines -> int('') ValueError). That must surface as
        an explicit UNKNOWN warn — absence would read as clean."""
        # Mock-alignment mechanics, not the cardinality pin (that lives
        # hardcoded in test_diag_conformance_view).
        n_poison = len(chash_tables.POISON_CHASH_TABLES)  # poison statements fine
        state = {"i": 0}

        def runner(argv, env):
            i = state["i"]; state["i"] += 1
            if i < n_poison:
                return subprocess.CompletedProcess(argv, 0, stdout="0\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="\n", stderr="")  # NULL sum

        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(160),
            diag_runner=runner,
        )
        debt = [r for r in results if r.label == "Chash legacy debt"]
        assert len(debt) == 1
        assert debt[0].warn is True
        assert "UNKNOWN" in debt[0].detail
        assert "clean" in debt[0].detail  # the do-not-read-as-clean instruction

    def test_lint_violation_is_never_retried_against_legacy(self, tmp_path, monkeypatch):
        # A content-reading statement: fails the fail-closed lint pre-DB.
        monkeypatch.setattr(
            chash_tables, "chash_conformance_statements",
            lambda: ("SELECT chash FROM nexus.chash_index",),
        )
        calls = {"n": 0}

        def runner(argv, env):
            calls["n"] += 1
            return subprocess.CompletedProcess(argv, 0, stdout="0\n", stderr="")

        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),  # hermetic: never ambient discovery
            psql_runner=_psql_runner_ok(160),
            diag_runner=runner,
        )
        chash = [r for r in results if "chash" in r.label.lower()]
        assert chash and chash[0].warn is True  # probe-did-not-run WARN
        assert calls["n"] == 0, (
            "a DiagnosticSqlViolation must reach the outer handler without a "
            "single psql invocation - never a silent legacy retry"
        )


def _content_diag_runner(
    *,
    unified_chunks: str = "0",
    direct_unified_chunks: str | None = None,
    catalog_document_chunks: str = "0",
    legacy_chunks_384: str = "0",
    legacy_chunks_768: str = "0",
    legacy_chunks_1024: str = "0",
    debt: str = "0",
    era_answer: str = "1",
    fail_relations: tuple[str, ...] = (),
):
    """A run_diagnostic_sql psql_runner keyed by SQL CONTENT rather than
    call order — robust against exactly how many round-trips a given code
    path issues (whether the debt-leg probe or the era discriminator
    fires is a code-path detail, not something these tests should have to
    predict by counting). Each keyword arg is the response for one
    distinguishable table/leg; ``fail_relations`` names relations whose
    query should fail outright (returncode 1, 'relation ... does not
    exist') to simulate a genuinely absent table — the diag view itself
    is matched via the literal ``"nexus.diag_chash_conformance"``.

    ``direct_unified_chunks`` lets a test give the VIEW-path leg for
    ``nexus.chunks`` a different answer than the DIRECT (bypass-the-view)
    leg for the same table — needed for the mid-migration window where the
    schema has migrated (direct COUNT works) but the deployed view is
    still stale (view-path leg blank). Defaults to ``unified_chunks`` when
    unset, matching every pre-existing caller's single-value behavior."""
    if direct_unified_chunks is None:
        direct_unified_chunks = unified_chunks
    calls: list[str] = []

    def _ok(val: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 0, stdout=f"{val}\n", stderr="")

    def _fail(rel: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            [], 1, stdout="", stderr=f'relation "{rel}" does not exist',
        )

    def runner(argv, env):
        stmt = argv[-1]
        calls.append(stmt)
        # to_regclass FIRST: fail_relations entries like the exact
        # "FROM nexus.chunks " marker below would otherwise false-match
        # the era probe's "to_regclass('nexus.chunks')" substring.
        if "to_regclass" in stmt:
            return _ok(era_answer)
        for rel in fail_relations:
            if rel in stmt:
                return _fail(rel)
        # View-path legs are quoted ('nexus.chunks_384'); direct-fallback
        # legs are unquoted (FROM nexus.chunks_384 ). Order matters: the
        # per-dim checks must run before the bare "nexus.chunks" checks
        # since "nexus.chunks_384" contains "nexus.chunks" as a prefix.
        if "chunks_384" in stmt:
            return _ok(legacy_chunks_384)
        if "chunks_768" in stmt:
            return _ok(legacy_chunks_768)
        if "chunks_1024" in stmt:
            return _ok(legacy_chunks_1024)
        if "catalog_document_chunks" in stmt:
            return _ok(catalog_document_chunks)
        if "topic_assignments" in stmt or "frecency" in stmt or "relevance_log" in stmt:
            return _ok(debt)
        if "nexus.chunks" in stmt:
            # View-path form: "...diag_chash_conformance WHERE table_name
            # = 'nexus.chunks'"; direct form: "...FROM nexus.chunks WHERE
            # octet_length...". Disambiguate on the view relation name.
            if "diag_chash_conformance" in stmt:
                return _ok(unified_chunks)
            return _ok(direct_unified_chunks)
        return _ok("0")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


class TestChashProbeEraStraddle:
    """RDR-191 F14a mirror-direction straddle in the poison gate
    (nexus-o8dil, 2026-08-14, package-upgrade MVV failure): the pre-
    convergence probe's client-side statements already carry the
    POST-unify table names (``nexus.chunks``), but the store's engine may
    not have migrated to that relation yet. The deployed
    ``nexus.diag_chash_conformance`` view then reflects whichever
    chash_tables.py era provisioned it — possibly OLDER than this
    client's working-tree code — so the unified WHERE filter can execute
    successfully yet return a NULL aggregate (blank psql line) for
    ``nexus.chunks`` specifically. Pre-fix this reached
    ``sum(int(c) for c in counts)`` and raised a bare
    ``ValueError: invalid literal for int() with base 10: ''`` that named
    no table, permanently deferring engine convergence (the exact shape
    hit by ``tests/e2e/migration-rehearsal/run.sh --package-upgrade``)."""

    def test_legacy_view_row_blank_retries_legacy_era_and_unblocks(self, tmp_path):
        """The confirmed production shape: the unified view-path query
        succeeds (no exception) but returns a blank aggregate for
        ``nexus.chunks`` (deployed view still per-dim-shaped) and a real
        value for ``nexus.catalog_document_chunks`` (name unchanged across
        the unify) — exactly the ``['', '0']`` observed in the failing
        MVV run. The era discriminator reports "nexus.chunks absent", so
        the probe retries against the legacy per-dim view rows and
        measures a real (non-vacuous) poison count instead of deferring
        forever."""
        runner = _content_diag_runner(
            unified_chunks="", catalog_document_chunks="0",
            era_answer="0",  # nexus.chunks does not exist yet
            legacy_chunks_384="2", legacy_chunks_768="0", legacy_chunks_1024="0",
        )
        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(160),
            diag_runner=runner,
        )
        chash = [r for r in results if r.label == "Chunk chash conformance"]
        assert chash, "the store must be VERIFIED (poisoned), not deferred as unverifiable"
        assert chash[0].warn is True and chash[0].fatal is False
        assert "2 chunk row(s)" in chash[0].detail
        assert "could not probe" not in chash[0].detail

    def test_legacy_view_all_clean_unblocks_convergence_cleanly(self, tmp_path):
        """Same straddle shape as above, but every legacy-era leg is
        clean (0) — convergence must proceed with NO poison result at
        all, not a lingering unverifiable WARN."""
        runner = _content_diag_runner(
            unified_chunks="", catalog_document_chunks="0", era_answer="0",
        )
        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(160),
            diag_runner=runner,
        )
        assert [r.label for r in results] == ["Schema migrations"]

    def test_neither_era_resolves_still_defers_with_named_table(self, tmp_path):
        """NON-VACUITY (requirement 1's fail-safe): if the legacy-era
        retry ALSO comes back blank — genuinely no candidate table
        answers — the store stays UNVERIFIABLE (the existing defer path),
        never a false clean. The surfaced detail now NAMES the specific
        table that could not be parsed, instead of a bare int('')."""
        runner = _content_diag_runner(
            unified_chunks="", catalog_document_chunks="0", era_answer="0",
            legacy_chunks_384="", legacy_chunks_768="", legacy_chunks_1024="",
            # catalog_document_chunks stays "0" in BOTH the unified and
            # legacy legs (name unchanged) — only nexus.chunks itself is
            # unmeasurable in every era tried, which must still defer.
        )
        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(160),
            diag_runner=runner,
        )
        chash = next(r for r in results if r.label == "Chunk chash conformance")
        assert chash.warn is True and chash.fatal is False
        assert "did not run" in chash.detail
        assert "nexus.chunks" in chash.detail  # names the offending table

    def test_unified_schema_present_never_reinterprets_as_legacy(self, tmp_path):
        """Safety rail: a genuinely poisoned CURRENT-era store (unified
        view returns real, non-blank values) must NEVER retry against
        legacy names or otherwise change interpretation — the era
        discriminator is only consulted when a blank is observed."""
        runner = _content_diag_runner(unified_chunks="3", catalog_document_chunks="0")
        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(160),
            diag_runner=runner,
        )
        assert not any("to_regclass" in c for c in runner.calls), (
            "a clean/poisoned unified result must never trigger the era probe"
        )
        chash = next(r for r in results if r.label == "Chunk chash conformance")
        assert "3 chunk row(s)" in chash.detail

    def test_view_absent_and_unified_direct_fallback_absent_retries_legacy_direct(self, tmp_path):
        """The OLDER straddle shape: the diag view itself was never
        provisioned on this box (RuntimeError, 'relation ... does not
        exist'), and the existing direct-table fallback — which still
        assumes the unified table names — ALSO fails because
        nexus.chunks does not exist yet. The probe must retry once more
        against the legacy per-dim direct-table statements before
        deferring."""
        runner = _content_diag_runner(
            # Precise markers, not the bare "nexus.chunks" substring —
            # that would also false-match chunks_384/768/1024 (which
            # must SUCCEED here) and the era probe's own to_regclass call.
            fail_relations=("nexus.diag_chash_conformance", "FROM nexus.chunks "),
            era_answer="0",  # nexus.chunks absent -> retry legacy
        )
        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(160),
            diag_runner=runner,
        )
        assert [r.label for r in results] == ["Schema migrations"]

    def test_schema_migrated_but_view_stale_measures_via_direct_fallback(self, tmp_path):
        """RDR-191 F14a straddle, round-2 review SIGNIFICANT FINDING 1: the
        MID-MIGRATION window. nexus.chunks EXISTS (the engine binary has
        already swapped and Liquibase has run), but the deployed diag view
        is STILL per-dim shaped — view re-provisioning only happens via
        the chash-rekey rung's re-provision step or `nx init --service`,
        never automatically after a bare engine swap. The unified
        view-path leg comes back blank, the era discriminator proves
        nexus.chunks EXISTS (era_answer='1'), so the probe must fall
        through to the DIRECT unified-table statements and MEASURE a real
        count — never a generic 'could not probe... did not run' WARN for
        a store that is provably current-era and provably measurable."""
        runner = _content_diag_runner(
            unified_chunks="",              # view-path leg: stale view, blank
            direct_unified_chunks="4",       # direct leg: schema migrated, real count
            catalog_document_chunks="0",
            era_answer="1",                  # nexus.chunks EXISTS
        )
        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(160),
            diag_runner=runner,
        )
        chash = [r for r in results if r.label == "Chunk chash conformance"]
        assert chash, "a provably current-era, provably measurable store must not defer"
        assert chash[0].warn is True and chash[0].fatal is False
        assert "4 chunk row(s)" in chash[0].detail
        assert "could not probe" not in chash[0].detail
        # the legacy-era view retry must NOT have fired — unified_exists=True
        # skips straight to the direct fallback per the safety rail.
        legacy_leg_calls = [c for c in runner.calls if "chunks_384" in c]
        assert not legacy_leg_calls, "a current-era store must never touch the legacy per-dim view rows"

    def test_schema_migrated_view_stale_and_direct_also_fails_still_defers(self, tmp_path):
        """The companion non-vacuity case: nexus.chunks EXISTS but BOTH the
        stale view leg AND the direct fallback fail to produce a
        measurement (e.g. a genuine grant gap) — the store stays
        unverifiable, the existing fail-safe untouched."""
        runner = _content_diag_runner(
            unified_chunks="", era_answer="1",
            fail_relations=("FROM nexus.chunks ",),  # direct leg also fails
        )
        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(160),
            diag_runner=runner,
        )
        chash = next(r for r in results if r.label == "Chunk chash conformance")
        assert chash.warn is True and chash.fatal is False
        assert "did not run" in chash.detail


class TestParseConformanceSum:
    """MINOR (round-2 review): parse_conformance_sum had zero direct test
    coverage for its non-numeric and length-mismatch branches — every
    existing exercise of it went through the blank-string arm only."""

    def test_sums_valid_counts(self):
        tables = chash_tables.POISON_CHASH_TABLES
        assert chash_tables.parse_conformance_sum(tables, ("3", "5")) == 8

    def test_blank_raises_naming_the_table(self):
        tables = chash_tables.POISON_CHASH_TABLES
        with pytest.raises(ValueError, match="nexus.chunks"):
            chash_tables.parse_conformance_sum(tables, ("", "5"))

    def test_non_numeric_raises_naming_the_table_and_value(self):
        tables = chash_tables.POISON_CHASH_TABLES
        with pytest.raises(ValueError, match="nexus.catalog_document_chunks"):
            chash_tables.parse_conformance_sum(tables, ("3", "not-a-number"))

    def test_length_mismatch_raises(self):
        tables = chash_tables.POISON_CHASH_TABLES
        with pytest.raises(ValueError, match="2.*1|partial scan"):
            chash_tables.parse_conformance_sum(tables, ("3",))


class TestDirectFallbackSafetyRailReRaise:
    """MINOR (round-2 review): the direct-fallback arm's safety-rail
    re-raise (health.py: view absent AND unified direct fallback absent
    AND the era discriminator proves nexus.chunks EXISTS -> re-raise the
    ORIGINAL error rather than retrying under legacy names) had no
    dedicated test — only its legacy-era sibling
    (test_view_absent_and_unified_direct_fallback_absent_retries_legacy_direct)
    was covered."""

    def test_current_era_store_reraises_original_error_not_retried(self, tmp_path):
        """Both the view AND the unified direct fallback fail, but the era
        discriminator proves nexus.chunks EXISTS — this must be a
        genuinely broken current-era store, re-surfaced via the ORIGINAL
        error, never silently retried against the legacy per-dim names."""
        runner = _content_diag_runner(
            fail_relations=("nexus.diag_chash_conformance", "FROM nexus.chunks "),
            era_answer="1",  # nexus.chunks EXISTS — a genuinely broken current-era store
        )
        creds = _make_creds_file(tmp_path)
        results = _check_migration_state(
            creds_path=creds,
            psql_bin=Path("/fake/psql"),
            psql_runner=_psql_runner_ok(160),
            diag_runner=runner,
        )
        chash = next(r for r in results if r.label == "Chunk chash conformance")
        assert chash.warn is True and chash.fatal is False
        assert "did not run" in chash.detail
        # the ORIGINAL direct-fallback error surfaces (nexus.chunks), not a
        # legacy-era retry outcome — and the legacy per-dim names were
        # never touched.
        assert "nexus.chunks" in chash.detail
        legacy_leg_calls = [c for c in runner.calls if "chunks_384" in c]
        assert not legacy_leg_calls, "a current-era store must never retry the legacy per-dim names"


# ── nexus-5xn3k AC5: dangling manifest chashes ───────────────────────────────


# RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15: TestCheckDanglingManifests
# DELETED — health.py's _check_dangling_manifests is retired entirely
# (the manifest-chunk FK makes the dangling state it detected
# unreachable). See TestManifestNullCollectionExclusionIsMechanical below
# for the mechanical pin on what stays.


class TestCheckChashConformanceReport:
    """RDR-180 (bead nexus-du2dw): managed/cloud-mode chash width-conformance
    coverage via the engine route (``nexus.chash_conformance_report(dim)``).

    The LOCAL ``nexus_diag`` psql probe (``nexus.db.diag_connection``) is
    LOCAL-ONLY BY DESIGN (nexus-y3wuu Hal decision) — it shells a local
    ``psql`` at 127.0.0.1 using a local ``pg_credentials`` file, which does
    not exist on managed/cloud installs. This check is the fallback
    observability surface for those installs, via
    ``HttpCatalogClient.chash_conformance_report``.

    KILL-CONTROL NOTE (CORRECTED — code-review fix round, substantive-critic
    CRITICAL nexus-5nrzk, T2 [21458]): the prior claim here ("seeding is
    impossible by construction") was FALSE AS WRITTEN. Seeding a genuine
    width-non-conformant row through the real engine IS possible — but only
    via a superuser CHECK-constraint-drop maneuver at the JAVA test layer
    (``service/src/test/java/dev/nexus/service/ChashConformanceReportIntegrationTest.java``,
    mirroring ``RekeyOpsIntegrationTest``'s ``withChecksDropped`` helper),
    NOT reachable through any client-facing HTTP/Python write path (the
    width CHECK, even NOT VALID, still enforces on every NEW client write —
    see ``rdr180-001-bytea-chash.xml``). This Python engine-integration
    test file (``tests/db/test_du2dw_chash_conformance_report_engine.py``)
    therefore still cannot seed poison itself and continues to prove only
    the clean branch + wiring against a real engine. The actual non-zero
    branch — ``nexus.chash_conformance_report(dim)`` counting a real
    poisoned row and returning its hex in ``sample_chashes`` — is now
    proven end-to-end at the Java layer instead. THIS class proves the same
    failure branch at the FAKE-CATALOG-READER layer (a stub
    ``chash_conformance_report`` returning non-conformant counts) — the
    same layer-test posture ``TestCheckDanglingManifests`` above already
    uses for its analogous engine-route checks
    (``manifest_verify_all``/``manifest_orphans``), now corroborated by a
    real-poison proof one layer down rather than merely asserted absent.
    """

    def _cat(
        self, *, tables_by_dim: dict[int, list[dict]] | None = None,
        raise_status: int | None = None, raise_exc: Exception | None = None,
    ):
        import httpx

        class _Cat:
            def chash_conformance_report(self, dim: int) -> dict:
                if raise_status is not None:
                    request = httpx.Request("GET", "https://engine.example/v1/catalog/chash/conformance")
                    response = httpx.Response(raise_status, request=request)
                    raise httpx.HTTPStatusError(
                        f"{raise_status} error", request=request, response=response,
                    )
                if raise_exc is not None:
                    raise raise_exc
                tables = (tables_by_dim or {}).get(dim, [
                    {"table_name": f"nexus.chunks_{dim}", "total": 5, "non_conformant": 0, "sample_chashes": []},
                    {"table_name": "nexus.catalog_document_chunks", "total": 5, "non_conformant": 0, "sample_chashes": []},
                ])
                return {"dim": dim, "tables": tables}
        return _Cat()

    def _t3(self, *, collection_names: list[str] | None = None, raise_exc: Exception | None = None):
        """Fake T3 handle backing the unroutable-collections probe
        (nexus-4ijv4). ``None`` names -> no collections (the common case for
        the other tests in this class, which don't care about this axis)."""
        class _T3:
            def list_collections(self) -> list[dict]:
                if raise_exc is not None:
                    raise raise_exc
                return [{"name": n} for n in (collection_names or [])]
        return _T3()

    def _run(self, monkeypatch, cat, *, t3=None):
        import nexus.health as h
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: cat, raising=False,
        )
        monkeypatch.setattr(
            "nexus.db.make_t3",
            lambda *a, **k: (t3 if t3 is not None else self._t3()),
            raising=False,
        )
        return h._check_chash_conformance_report()[0]

    def test_clean_store_across_all_dims(self, monkeypatch) -> None:
        cat = self._cat()
        r = self._run(monkeypatch, cat)
        assert r.ok is True
        assert "clean" in r.detail
        assert "3 dim(s) checked" in r.detail, r.detail

    def test_non_conformant_rows_reported_loud(self, monkeypatch) -> None:
        """The failure branch (kill control — see class docstring for why
        this is proven at the fake-reader layer, not via a live seed)."""
        cat = self._cat(tables_by_dim={
            384: [
                {"table_name": "nexus.chunks_384", "total": 5, "non_conformant": 2, "sample_chashes": ["aa", "bb"]},
                {"table_name": "nexus.catalog_document_chunks", "total": 5, "non_conformant": 0, "sample_chashes": []},
            ],
        })
        r = self._run(monkeypatch, cat)
        assert r.ok is False and r.warn is True
        assert "2 chunk row(s)" in r.detail, r.detail
        assert "nexus.chunks_384=2" in r.detail, r.detail
        assert "TENANT-SCOPED" in r.detail, r.detail
        # nexus-lgdel.l1 deleted the chash-rekey rung, so `nx upgrade` is a
        # no-op for this warning and must not be offered as its remedy. The
        # real remedy is re-indexing, which recomputes ids from stored text.
        assert any("nx index repo" in f for f in r.fix_suggestions)

    def test_engine_predating_route_renders_skipped_with_warning(self, monkeypatch) -> None:
        """vw594 F3 / manifest_verify_all precedent: a pre-route engine 404s
        every dim identically — SKIPPED + loud warn, never a false clean."""
        cat = self._cat(raise_status=404)
        r = self._run(monkeypatch, cat)
        assert r.ok is False and r.warn is True
        assert "SKIPPED" in r.detail
        assert "chash-conformance route" in r.detail, r.detail
        assert "NOT a clean-store signal" in r.detail, r.detail

    def test_other_transport_failure_renders_skipped_with_warning_not_clean(self, monkeypatch) -> None:
        cat = self._cat(raise_status=500)
        r = self._run(monkeypatch, cat)
        assert r.ok is False and r.warn is True
        assert "SKIPPED" in r.detail

    def test_unexpected_exception_renders_warn_not_a_false_clean(self, monkeypatch) -> None:
        """substantive-critic (T2 [21458]): this branch used to swallow ANY
        exception — including HttpCatalogClient.chash_conformance_report's
        OWN deliberate fail-closed RuntimeError (a malformed response) —
        into ok=True 'skipped', contradicting this check's own never-a-
        false-clean docstring promise. Must never crash `nx doctor` (still
        true here) AND must never render ok=True."""
        cat = self._cat(raise_exc=RuntimeError("engine unreachable"))
        r = self._run(monkeypatch, cat)
        assert r.ok is False and r.warn is True
        assert "SKIPPED" in r.detail, r.detail

    def test_client_fail_closed_runtimeerror_renders_warn_not_a_false_clean(
        self, monkeypatch,
    ) -> None:
        """The exact concrete shape the prior bug swallowed: HttpCatalogClient
        .chash_conformance_report raises RuntimeError when the response
        carries no `tables` field (its own fail-closed contract, see that
        method's docstring) — this must surface as a loud WARN, never a
        silent clean pass."""
        cat = self._cat(raise_exc=RuntimeError(
            "chash/conformance response carried no `tables` field — cannot "
            "verify chash conformance; refusing a false-clean empty report"
        ))
        r = self._run(monkeypatch, cat)
        assert r.ok is False and r.warn is True
        assert "carried no `tables` field" in r.detail, r.detail

    def test_catalog_unavailable_degrades_to_plain_skip(self, monkeypatch) -> None:
        import nexus.health as h
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: None, raising=False,
        )
        r = h._check_chash_conformance_report()[0]
        assert r.ok is True
        assert "no catalog" in r.detail

    def test_zero_dims_checked_is_not_a_clean_pass(self, monkeypatch) -> None:
        """nexus-5h4ou: ``dims_checked == 0`` used to return a bare ok=True
        "skipped (no dim reachable)" directly under a comment citing the
        nexus-kmo9h non-vacuity rule — healthy-when-it-examined-NOTHING.
        RDR-191 makes the state reachable for real (the window between the
        shard drop and consumer retargeting)."""
        import nexus.health as h
        monkeypatch.setattr(h, "_CHASH_CONFORMANCE_REPORT_DIMS", ())
        r = self._run(monkeypatch, self._cat())
        assert r.ok is False, "zero dims examined must not read as clean"
        assert r.warn is True
        assert "0 dim" in r.detail or "no dim" in r.detail.lower()

    def test_catalog_factory_raising_warns_not_a_false_clean(self, monkeypatch) -> None:
        """nexus-5h4ou second arm: the factory RAISING is "could not check",
        which must be distinguishable from clean — unlike reader-returns-
        None (a benign no-catalog configuration state, pinned ok=True by
        test_catalog_unavailable_degrades_to_plain_skip above)."""
        import nexus.health as h

        def _boom(*a, **k):
            raise RuntimeError("factory exploded")

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", _boom, raising=False,
        )
        r = h._check_chash_conformance_report()[0]
        assert r.ok is False
        assert r.warn is True
        assert "factory exploded" in r.detail

    def test_could_not_check_arms_warn_but_do_not_fail_the_run(
        self, monkeypatch,
    ) -> None:
        """nexus-5h4ou acceptance item 4, DECIDED SCOPE (batch-2 review
        Important-1): the zero-dims and factory-raise arms are warn, NOT
        fatal — matching every other could-not-check arm of this check
        (engine-404, transport failure). Doctor's exit path
        (``format_health_for_cli``'s ``failed`` flag keys on ``fatal and
        not ok``; warns never fail the run, RDR-129 B4) is therefore
        deliberately unchanged; distinguishability from clean lives in the
        rendered warn glyph and the JSON ``ok=false``. This test pins BOTH
        halves so neither the arms silently regain fatal (noisy doctor
        reds on every pre-route box) nor the render path stops
        distinguishing them from clean."""
        import nexus.health as h
        monkeypatch.setattr(h, "_CHASH_CONFORMANCE_REPORT_DIMS", ())
        r = self._run(monkeypatch, self._cat())
        assert (r.ok, r.warn, r.fatal) == (False, True, False)
        rendered, failed = h.format_health_for_cli([r], local_mode=True)
        assert failed is False  # warn convention: never fails the run
        assert "SKIPPED" in rendered and "NOTHING" in rendered  # but never renders clean

    def test_label_distinct_from_gate_matched_local_probe_label(self) -> None:
        """This check must NEVER collide with ``CHASH_CONFORMANCE_LABEL`` —
        the install-binary gate (``commands/daemon.py``) and the convergence
        gate (``upgrade_finish.py``) exact-match on that label and need the
        LOCAL nexus_diag probe's cross-tenant BYPASSRLS visibility
        (nexus-vounk); this engine-route check's tenant-scoped count would
        silently under-report a poisoned store if it fed the same gate."""
        import nexus.health as h
        from nexus.db.chash_tables import CHASH_CONFORMANCE_LABEL

        assert h._CHASH_CONFORMANCE_REPORT_LABEL != CHASH_CONFORMANCE_LABEL

    # ── unroutable-collection surfacing (nexus-4ijv4, T2 [21458]) ──────────

    def test_clean_with_unroutable_collection_is_not_plain_clean(self, monkeypatch) -> None:
        """The core complaint: a tenant with content under an unrecognized
        embedding-model token must NOT read as a plain clean pass — those
        collections were never counted or sampled at all."""
        cat = self._cat()  # every dim clean
        t3 = self._t3(collection_names=[
            "knowledge__x__some-unrecognized-legacy-token-9000__v1",
        ])
        r = self._run(monkeypatch, cat, t3=t3)
        assert r.ok is False and r.warn is True, (
            "clean-with-unroutable must render as a WARN, not ok=True — "
            f"got ok={r.ok} warn={r.warn} detail={r.detail!r}"
        )
        assert "NOT CHECKED" in r.detail, r.detail
        assert "some-unrecognized-legacy-token-9000" in r.detail, r.detail

    def test_dirty_with_unroutable_collection_carries_both_notes(self, monkeypatch) -> None:
        """The non_conformant>0 branch must ALSO carry the unroutable note —
        the two findings are orthogonal (one is about rows the probe
        checked and found bad, the other is about content the probe never
        reached at all) and neither should hide the other."""
        cat = self._cat(tables_by_dim={
            384: [
                {"table_name": "nexus.chunks_384", "total": 5, "non_conformant": 1, "sample_chashes": ["aa"]},
                {"table_name": "nexus.catalog_document_chunks", "total": 5, "non_conformant": 0, "sample_chashes": []},
            ],
        })
        t3 = self._t3(collection_names=[
            "knowledge__x__some-unrecognized-legacy-token-9000__v1",
        ])
        r = self._run(monkeypatch, cat, t3=t3)
        assert r.ok is False and r.warn is True
        assert "1 chunk row(s)" in r.detail, r.detail
        assert "NOT CHECKED" in r.detail, r.detail
        assert "some-unrecognized-legacy-token-9000" in r.detail, r.detail

    def test_fully_routable_collections_still_render_plain_clean(self, monkeypatch) -> None:
        """Regression guard: a tenant whose collections ALL route to a known
        dim must still get the plain clean pass — the new probe must not
        false-positive on ordinary, fully-covered content."""
        cat = self._cat()
        t3 = self._t3(collection_names=[
            "knowledge__x__bge-base-en-v15-768__v1",
            "code__y__voyage-code-3__v1",
        ])
        r = self._run(monkeypatch, cat, t3=t3)
        assert r.ok is True
        assert "clean" in r.detail
        assert "NOT CHECKED" not in r.detail, r.detail

    def test_unroutable_probe_failure_does_not_hide_primary_clean_result(
        self, monkeypatch,
    ) -> None:
        """Best-effort: the unroutable-collection enrichment failing must
        never crash this check or suppress the primary (clean) verdict —
        it just loses the enrichment, silently to the operator's detriment
        but never to a crash."""
        cat = self._cat()
        t3 = self._t3(raise_exc=RuntimeError("t3 unreachable"))
        r = self._run(monkeypatch, cat, t3=t3)
        assert r.ok is True
        assert "clean" in r.detail

    def test_unroutable_probe_failure_does_not_hide_primary_dirty_result(
        self, monkeypatch,
    ) -> None:
        cat = self._cat(tables_by_dim={
            384: [
                {"table_name": "nexus.chunks_384", "total": 5, "non_conformant": 3, "sample_chashes": ["aa"]},
                {"table_name": "nexus.catalog_document_chunks", "total": 5, "non_conformant": 0, "sample_chashes": []},
            ],
        })
        t3 = self._t3(raise_exc=RuntimeError("t3 unreachable"))
        r = self._run(monkeypatch, cat, t3=t3)
        assert r.ok is False and r.warn is True
        assert "3 chunk row(s)" in r.detail, r.detail


class TestCheckGcAuditNonEmptyAfterPurge:
    """nexus-sybbh (client half): ``nexus.gc_audit`` was found completely
    empty on the live store despite real purges having run. This check
    cross-references a LOCAL breadcrumb of ``nx catalog purge-trash``
    executions (``nexus.gc_purge_marker``) against the engine's
    ``gc_audit/list`` route, degrading honestly (RDR-129 B4 house style —
    same shape as ``TestCheckChashConformanceReport`` above, its closest
    sibling) rather than ever rendering a false clean pass.
    """

    def _markers(self, n: int = 1) -> list[dict]:
        return [
            {"ts": "2026-08-19T00:00:00+00:00", "older_than_days": 30,
             "result": {"documents_purged": 1}}
            for _ in range(n)
        ]

    def _cat(
        self, *, entries: list[dict] | None = None,
        raise_status: int | None = None, raise_exc: Exception | None = None,
    ):
        import httpx

        class _Cat:
            def gc_audit_list(self, *, operation: str | None = None, **kwargs) -> list[dict]:  # noqa: ANN003
                if raise_status is not None:
                    request = httpx.Request("GET", "https://engine.example/v1/catalog/gc_audit/list")
                    response = httpx.Response(raise_status, request=request)
                    raise httpx.HTTPStatusError(
                        f"{raise_status} error", request=request, response=response,
                    )
                if raise_exc is not None:
                    raise raise_exc
                rows = entries if entries is not None else []
                # Mirror CatalogRepository#listGcAudit's exact-match
                # operation filter server-side, so a fake with an
                # unrelated-operation row behaves like the real engine.
                if operation is not None:
                    rows = [e for e in rows if e.get("operation") == operation]
                return rows
        return _Cat()

    def _run(self, monkeypatch, *, markers=None, cat=None):
        import nexus.health as h
        monkeypatch.setattr(
            "nexus.gc_purge_marker.read_recent_purge_markers",
            lambda **k: (markers if markers is not None else []), raising=False,
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: (cat if cat is not None else self._cat()), raising=False,
        )
        return h._check_gc_audit_non_empty_after_purge()[0]

    def test_no_local_purge_evidence_is_a_named_skip_not_a_warn(self, monkeypatch) -> None:
        r = self._run(monkeypatch, markers=[])
        assert r.ok is True
        assert r.warn is False
        assert "no local" in r.detail.lower()
        assert "nothing to cross-check" in r.detail

    def test_purge_evidence_and_gc_audit_empty_warns_loud(self, monkeypatch) -> None:
        """The defect this check exists to catch: a purge ran, and
        ``gc_audit`` has NOTHING for it — never a silent/false clean."""
        r = self._run(monkeypatch, markers=self._markers(2), cat=self._cat(entries=[]))
        assert r.ok is False and r.warn is True
        assert "2 local" in r.detail
        assert "gc_audit is EMPTY" in r.detail, r.detail
        assert "nexus-sybbh" in r.detail
        assert any("purge-trash" in f for f in r.fix_suggestions)

    def test_purge_evidence_and_gc_audit_populated_is_clean(self, monkeypatch) -> None:
        r = self._run(
            monkeypatch, markers=self._markers(1),
            cat=self._cat(entries=[{
                "id": 1, "operation": "purge_trash",
                "created_at": "2026-08-19T01:00:00+00:00",
            }]),
        )
        assert r.ok is True
        assert r.warn is False
        assert "audited" in r.detail

    def test_gc_audit_row_for_unrelated_operation_does_not_false_clean(self, monkeypatch) -> None:
        """critique 2026-08-19 (both reviewers): a gc_audit row from a
        DIFFERENT operation (e.g. the routine sweep_superseded_chunks
        producer, now wired for all 4 producers) must not make this check
        report 'audited' for a purge_trash execution it never covered.
        Pre-fix, ``gc_audit_list(limit=1)`` had no ``operation`` filter, so
        ANY historical row (any operation, any time) false-cleaned forever."""
        r = self._run(
            monkeypatch, markers=self._markers(1),
            cat=self._cat(entries=[{
                "id": 5, "operation": "sweep_superseded_chunks",
                "created_at": "2026-08-19T00:10:00+00:00",
            }]),
        )
        assert r.ok is False and r.warn is True
        assert "gc_audit is EMPTY" in r.detail, r.detail

    def test_gc_audit_purge_trash_row_older_than_local_evidence_does_not_false_clean(
        self, monkeypatch,
    ) -> None:
        """A purge_trash row that predates the local marker (e.g. a stale
        row from before the writer-side bug regressed again) must not be
        credited as auditing THIS purge — no created_at-vs-marker-ts
        comparison pre-fix meant any past purge_trash row cleared the check
        forever, regardless of recency."""
        r = self._run(
            monkeypatch,
            markers=[{
                "ts": "2026-08-19T12:00:00+00:00", "older_than_days": 30,
                "result": {"documents_purged": 1},
            }],
            cat=self._cat(entries=[{
                "id": 3, "operation": "purge_trash",
                "created_at": "2026-08-18T00:00:00+00:00",
            }]),
        )
        assert r.ok is False and r.warn is True

    def test_writer_regression_after_a_prior_good_audit_is_caught(
        self, monkeypatch,
    ) -> None:
        """critique 2026-08-19 round 2 (both reviewers): with multiple
        markers in the 7-day window, anchoring on the OLDEST marker let a
        stale gc_audit row from an earlier, WORKING audit satisfy the check
        even though the writer regressed for a later purge in the same
        window — a rolling blind spot. Must anchor on the NEWEST marker so
        a regression after a prior good audit is still caught."""
        r = self._run(
            monkeypatch,
            markers=[
                {"ts": "2026-08-10T00:00:00+00:00", "older_than_days": 30,
                 "result": {"documents_purged": 1}},
                {"ts": "2026-08-19T00:00:00+00:00", "older_than_days": 30,
                 "result": {"documents_purged": 1}},
            ],
            cat=self._cat(entries=[{
                "id": 9, "operation": "purge_trash",
                "created_at": "2026-08-10T00:05:00+00:00",
            }]),
        )
        assert r.ok is False and r.warn is True
        assert "does not cover it" in r.detail, r.detail

    def test_healthy_no_op_purge_does_not_false_alarm(self, monkeypatch) -> None:
        """critique 2026-08-19 round 2: a marker records every REAL purge
        invocation regardless of whether anything was actually purged, but
        ``nexus.purge_trash``'s own audit INSERT is gated on nonzero effect
        (``v_chunk_count > 0 OR v_count > 0``, catalog-033-1) and writes NO
        row on a genuine no-op — the common case, since most real purges
        find nothing newly eligible. Must not warn 'gc_audit is EMPTY' for
        a marker whose own stored result shows zero effect."""
        r = self._run(
            monkeypatch,
            markers=[{
                "ts": "2026-08-19T00:00:00+00:00", "older_than_days": 30,
                "result": {
                    "dry_run": False, "documents_purged": 0,
                    "documents_eligible": 0,
                    "chunks_384_stranded": 0, "chunks_768_stranded": 0,
                    "chunks_1024_stranded": 0,
                },
            }],
            cat=self._cat(entries=[]),
        )
        assert r.ok is True
        assert r.warn is False
        assert "no-op" in r.detail.lower() or "zero-effect" in r.detail.lower()

    def test_effective_markers_ignore_no_op_siblings_in_the_window(
        self, monkeypatch,
    ) -> None:
        """A no-op marker alongside a real-effect marker in the same window
        must not corrupt the anchor — only the effectful marker counts."""
        r = self._run(
            monkeypatch,
            markers=[
                {"ts": "2026-08-19T00:00:00+00:00", "older_than_days": 30,
                 "result": {"documents_purged": 0, "chunks_384_stranded": 0}},
                {"ts": "2026-08-15T00:00:00+00:00", "older_than_days": 30,
                 "result": {"documents_purged": 2}},
            ],
            cat=self._cat(entries=[{
                "id": 11, "operation": "purge_trash",
                "created_at": "2026-08-15T00:02:00+00:00",
            }]),
        )
        assert r.ok is True and r.warn is False
        assert "1 local" in r.detail, r.detail

    def test_engine_predating_gc_audit_route_renders_skipped_with_warning(
        self, monkeypatch,
    ) -> None:
        r = self._run(monkeypatch, markers=self._markers(1), cat=self._cat(raise_status=404))
        assert r.ok is False and r.warn is True
        assert "SKIPPED" in r.detail
        assert "gc_audit/list route" in r.detail, r.detail
        assert "NOT a clean signal" in r.detail, r.detail

    def test_other_transport_failure_renders_warn_not_clean(self, monkeypatch) -> None:
        r = self._run(monkeypatch, markers=self._markers(1), cat=self._cat(raise_status=500))
        assert r.ok is False and r.warn is True
        assert "SKIPPED" in r.detail

    def test_unexpected_exception_renders_warn_not_a_false_clean(self, monkeypatch) -> None:
        r = self._run(
            monkeypatch, markers=self._markers(1),
            cat=self._cat(raise_exc=RuntimeError("engine unreachable")),
        )
        assert r.ok is False and r.warn is True
        assert "SKIPPED" in r.detail, r.detail

    def test_catalog_unavailable_degrades_to_warn_not_clean(self, monkeypatch) -> None:
        """Unlike the chash-conformance check's reader-returns-None branch
        (a benign no-catalog-configured state), local purge evidence
        already exists here — a catalog we cannot reach to cross-check it
        against is a could-not-check state, never ok=True."""
        import nexus.health as h
        monkeypatch.setattr(
            "nexus.gc_purge_marker.read_recent_purge_markers",
            lambda **k: self._markers(1), raising=False,
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: None, raising=False,
        )
        r = h._check_gc_audit_non_empty_after_purge()[0]
        assert r.ok is False and r.warn is True
        assert "no catalog reader" in r.detail.lower()

    def test_catalog_factory_raising_warns_not_a_false_clean(self, monkeypatch) -> None:
        import nexus.health as h

        def _boom(*a, **k):
            raise RuntimeError("factory exploded")

        monkeypatch.setattr(
            "nexus.gc_purge_marker.read_recent_purge_markers",
            lambda **k: self._markers(1), raising=False,
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", _boom, raising=False,
        )
        r = h._check_gc_audit_non_empty_after_purge()[0]
        assert r.ok is False and r.warn is True
        assert "factory exploded" in r.detail

    def test_never_fails_the_run(self, monkeypatch) -> None:
        """RDR-129 B4: warn never marks the doctor run failed."""
        import nexus.health as h
        r = self._run(monkeypatch, markers=self._markers(1), cat=self._cat(entries=[]))
        rendered, failed = h.format_health_for_cli([r], local_mode=True)
        assert failed is False
        assert "gc_audit is EMPTY" in rendered


class TestGcPurgeMarker:
    """``nexus.gc_purge_marker`` — the local breadcrumb file
    :func:`nexus.health._check_gc_audit_non_empty_after_purge` cross-
    references. Exercises the module directly (not through the CLI verb):
    round-trip, staleness cutoff, and honest degradation on a missing or
    corrupt file.
    """

    def test_round_trip_records_and_reads_back(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        import nexus.gc_purge_marker as m

        m.record_purge_marker({"documents_purged": 3}, older_than_days=30)
        markers = m.read_recent_purge_markers(within_days=7)
        assert len(markers) == 1
        assert markers[0]["result"]["documents_purged"] == 3
        assert markers[0]["older_than_days"] == 30

    def test_missing_file_reads_as_no_evidence(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        import nexus.gc_purge_marker as m

        assert m.read_recent_purge_markers(within_days=7) == []

    def test_markers_older_than_cutoff_are_excluded(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        import nexus.gc_purge_marker as m
        from datetime import UTC, datetime, timedelta

        path = tmp_path / "gc_purge_markers.jsonl"
        old_ts = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        path.write_text(json.dumps({
            "ts": old_ts, "older_than_days": 30, "result": {},
        }) + "\n")
        assert m.read_recent_purge_markers(within_days=7) == []

    def test_corrupt_line_is_skipped_not_fatal(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        import nexus.gc_purge_marker as m

        path = tmp_path / "gc_purge_markers.jsonl"
        path.write_text("not json at all\n")
        assert m.read_recent_purge_markers(within_days=7) == []

    def test_marker_path_resolves_config_dir_at_call_time(self, tmp_path, monkeypatch) -> None:
        """The module must NOT capture ``nexus_config_dir`` by value at import.

        Regression, 2026-08-20: three tests in this class failed in a full
        ``-n auto`` run, each reading back the round-trip test's marker
        despite pointing ``NEXUS_CONFIG_DIR`` at its own tmp_path. Cause:
        ``tests/test_doctor_cmd.py``'s autouse fixture patched
        ``nexus.config.nexus_config_dir`` with a lambda, and the doctor run
        under that patch FIRST-imported this module (deferred import at
        ``health.py`` ``_check_gc_audit_non_empty_after_purge``). A
        module-level ``from nexus.config import nexus_config_dir`` captured
        the lambda; monkeypatch teardown restored ``nexus.config`` but not
        this module's copy, pinning every later marker read/write in that
        worker process to one dead test's tmp dir. Identical class to the
        one recorded at ``tests/test_false_clean_diagnostics_service_mode``
        (v7.11.0, PR #1467) — that site was fixed, this consumer was not
        hardened, so the next leaking fixture re-broke it.

        This pins the consumer side: resolve through the module so a
        first-import inside ANY patched window cannot outlive the patch.
        """
        import importlib

        import nexus.gc_purge_marker as m

        poisoned = tmp_path / "poisoned"
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("nexus.config.nexus_config_dir", lambda: poisoned)
            importlib.reload(m)  # first-import inside the patched window
        # Patch is undone here — the module must follow the env again.
        try:
            monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "live"))
            assert m._marker_path() == tmp_path / "live" / "gc_purge_markers.jsonl"
        finally:
            importlib.reload(m)

    def test_record_failure_is_swallowed_not_raised(self, tmp_path, monkeypatch) -> None:
        """Best-effort: a marker-write failure must never break the purge
        itself, which has already happened by the time this is called."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "does" / "not" / "exist"))
        import nexus.gc_purge_marker as m

        # Blocked by a FILE occupying the parent path component so
        # mkdir(parents=True) raises — proves the swallow, not just the
        # happy path.
        blocker = tmp_path / "does"
        blocker.write_text("i am a file, not a directory")
        m.record_purge_marker({"documents_purged": 1}, older_than_days=30)  # must not raise


class TestManifestNullCollectionExclusionIsMechanical:
    """RDR-191 Phase 6 (bead nexus-o8dil.33) MECHANICAL EXCLUSION pin.

    Decision item 4 retires ``manifest_orphans``, ``manifest_verify``/
    ``manifest_verify_all``, ``manifest_backfill``, and
    ``_check_dangling_manifests`` — but EXPLICITLY, BY NAME, does NOT retire
    ``_check_manifest_null_collection`` / ``manifest_null_collection_report``:
    the manifest-chunk FK does not cover NULL-collection rows (PostgreSQL
    ``MATCH SIMPLE`` exempts any row with a NULL key column from enforcement
    entirely), so this census remains the ONLY visibility into a population
    that stays permanently unenforced.

    The RDR's own Gate Critical 1 records that this exact exclusion was
    ALREADY LOST ONCE — "the C1 correction was written in prose and never
    applied" to an earlier draft of Decision item 4. A comment saying "do
    not delete" is exactly what failed last time. This test makes the
    exclusion MECHANICAL: it asserts both symbols still exist as live,
    callable, non-stub code, so a future editor who deletes them (following
    a literal read of "retire the manifest-verify apparatus" without
    re-reading this carve-out) reds a real test, not a comment they can
    skip past.
    """

    def test_check_manifest_null_collection_exists_and_is_callable(self) -> None:
        import inspect

        import nexus.health as h

        assert hasattr(h, "_check_manifest_null_collection"), (
            "nexus.health._check_manifest_null_collection must still exist — "
            "RDR-191 Decision item 4's explicit carve-out, not retired "
            "alongside _check_dangling_manifests"
        )
        assert callable(h._check_manifest_null_collection)
        assert not inspect.iscoroutinefunction(h._check_manifest_null_collection)

    def test_check_manifest_null_collection_still_functions(self, monkeypatch) -> None:
        """Not just importable — actually runs and returns a real result."""
        import nexus.health as h

        class _Cat:
            def manifest_null_collection_report(self) -> dict:
                return {"total": 0, "backfillable": 0, "unavailable": False}

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: _Cat(), raising=False,
        )
        results = h._check_manifest_null_collection()
        assert isinstance(results, list) and len(results) == 1
        assert results[0].ok is True

    def test_manifest_null_collection_report_exists_and_is_callable(self) -> None:
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        assert hasattr(HttpCatalogClient, "manifest_null_collection_report"), (
            "HttpCatalogClient.manifest_null_collection_report must still "
            "exist — RDR-191 Decision item 4's explicit carve-out, not "
            "retired alongside manifest_verify/manifest_verify_all/"
            "manifest_orphans/manifest_backfill"
        )
        assert callable(HttpCatalogClient.manifest_null_collection_report)

    def test_doctor_still_registers_the_null_collection_check(self) -> None:
        """The exclusion must be mechanical end-to-end, not just at the
        function definition — a still-defined-but-unregistered check would
        be dead code wearing the carve-out's name."""
        import inspect

        import nexus.health as h

        source = inspect.getsource(h.run_health_checks) if hasattr(h, "run_health_checks") else None
        if source is None:
            # Fall back to scanning the whole module for the registration
            # call if the sweep entrypoint's name ever changes — the
            # invariant under test is "somewhere, doctor calls this",
            # not the entrypoint's own name.
            source = inspect.getsource(h)
        assert "_check_manifest_null_collection()" in source, (
            "nx doctor must still invoke _check_manifest_null_collection() "
            "somewhere in its sweep — found in nexus.health source: "
            f"{'_check_manifest_null_collection()' in source}"
        )


class TestCheckManifestNullCollection:
    """Substantive critique finding 1 (T2
    nexus/chroma-residue-C2-durability-critique-2026-08-10):
    ``_check_manifest_null_collection``'s ``unavailable`` branch used to
    render an unconditional loud WARN, which broke
    ``tests/e2e/fresh-install-mvv.sh`` on every virgin box today — no
    engine tag ships GET /v1/catalog/manifest/null_collection yet
    (``REQUIRED_ENGINE_VERSION`` pinned at ``(0,1,69)``, before the route).
    These tests pin the fix: severity GATED on the live
    ``REQUIRED_ENGINE_VERSION`` constant, not a static allowlist entry.
    """

    def _cat(self, *, report: dict | None = None, raise_exc: Exception | None = None):
        class _Cat:
            def manifest_null_collection_report(self) -> dict:
                if raise_exc is not None:
                    raise raise_exc
                return report if report is not None else {"total": 0, "backfillable": 0, "unavailable": False}
        return _Cat()

    def _run(self, monkeypatch, cat):
        import nexus.health as h
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: cat, raising=False,
        )
        return h._check_manifest_null_collection()[0]

    def test_unavailable_at_or_below_route_floor_renders_informational_not_warn(
        self, monkeypatch,
    ) -> None:
        """THE FIX: today's real state (every shipped engine predates the
        route) must render ok=True informational, never a WARN — otherwise
        fresh-install-mvv.sh's empty-by-design allowlist fails on every
        virgin box."""
        import nexus.engine_version as ev

        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", (0, 1, 69))
        cat = self._cat(report={"total": 0, "backfillable": 0, "unavailable": True})
        r = self._run(monkeypatch, cat)
        assert r.ok is True, f"expected informational (ok=True), got ok={r.ok} warn={r.warn}"
        assert r.warn is not True
        assert "informational" in r.detail, r.detail
        assert "EXPECTED" in r.detail, r.detail

    def test_unavailable_below_route_floor_also_renders_informational(
        self, monkeypatch,
    ) -> None:
        import nexus.engine_version as ev

        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", (0, 1, 65))
        cat = self._cat(report={"total": 0, "backfillable": 0, "unavailable": True})
        r = self._run(monkeypatch, cat)
        assert r.ok is True
        assert "informational" in r.detail, r.detail

    def test_unavailable_above_route_floor_renders_loud_warn(self, monkeypatch) -> None:
        """Once the floor moves past the route's ship version, "unavailable"
        is genuinely wrong (every servable engine should carry the route) —
        must flip to a loud WARN, matching the established fail-open-but-
        loud contract used elsewhere in this module."""
        import nexus.engine_version as ev

        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", (0, 1, 70))
        cat = self._cat(report={"total": 0, "backfillable": 0, "unavailable": True})
        r = self._run(monkeypatch, cat)
        assert r.ok is False and r.warn is True, (
            f"expected loud WARN once the floor has moved past the route's "
            f"ship version, got ok={r.ok} warn={r.warn}"
        )
        assert "UNKNOWN" in r.detail, r.detail

    def test_client_without_the_method_degrades_to_the_same_gated_branch(
        self, monkeypatch,
    ) -> None:
        """An older client / test double lacking the method entirely must
        take the exact same severity-gated path as an engine 404, not a
        different (unconditional) one."""
        import nexus.engine_version as ev

        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", (0, 1, 69))

        class _NoMethodCat:
            pass

        r = self._run(monkeypatch, _NoMethodCat())
        assert r.ok is True
        assert "informational" in r.detail, r.detail

    def test_available_report_with_rows_still_renders_loud_warn_regardless_of_floor(
        self, monkeypatch,
    ) -> None:
        """The floor-gating applies ONLY to the `unavailable` branch — a
        successful read reporting real pre-backfill rows must still WARN,
        independent of REQUIRED_ENGINE_VERSION."""
        import nexus.engine_version as ev

        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", (0, 1, 69))
        cat = self._cat(report={"total": 3, "backfillable": 2, "unavailable": False})
        r = self._run(monkeypatch, cat)
        assert r.ok is False and r.warn is True
        assert "3 manifest row(s)" in r.detail, r.detail

    def test_clean_report_of_zero_still_renders_plain_clean(self, monkeypatch) -> None:
        cat = self._cat(report={"total": 0, "backfillable": 0, "unavailable": False})
        r = self._run(monkeypatch, cat)
        assert r.ok is True
        assert r.detail == "none"


# RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15: TestManifestOrphanReportCompleteness
# DELETED — health.py's manifest_orphan_report (and the manifest_orphans
# client method it wired) are retired entirely alongside
# _check_dangling_manifests.


class TestCheckStaleIndexingRuns:
    """nexus-5xn3k.6 bead-text amendment: documents stranded in
    index_state='indexing' beyond a threshold. Distinct axis from
    manifest_verify_all — a fence that never cleared, not a missing-chunk
    aggregate.
    """

    def _entry(
        self, *, index_state, index_started_at="", source_uri="", tumbler="",
        index_state_reported=True, indexed_at="",
    ):
        return type("E", (), {
            "index_state": index_state,
            "index_started_at": index_started_at,
            "source_uri": source_uri,
            "tumbler": tumbler,
            # nexus-vw594 F3: defaults to True (matches CatalogEntry's own
            # default) — a fence-aware engine reporting the key. Pass
            # False to simulate a genuinely pre-fence engine that never
            # sends the key at all.
            "index_state_reported": index_state_reported,
            "indexed_at": indexed_at,
        })()

    def _cat(self, entries):
        class _Cat:
            def all_documents(self, limit=0):
                return list(entries)
        return _Cat()

    def _run(self, monkeypatch, entries):
        import nexus.health as h
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: self._cat(entries), raising=False,
        )
        return h._check_stale_indexing_runs()[0]

    def _iso_hours_ago(self, hours: float) -> str:
        from datetime import UTC, datetime, timedelta
        return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()

    def test_stale_indexing_document_is_reported(self, monkeypatch) -> None:
        entries = [self._entry(
            index_state="indexing",
            index_started_at=self._iso_hours_ago(10),
            source_uri="file:///a.pdf",
        )]
        r = self._run(monkeypatch, entries)
        assert r.ok is False and r.warn is True
        assert "file:///a.pdf" in r.detail
        assert "1 document(s)" in r.detail

    def test_recent_indexing_document_is_not_stale(self, monkeypatch) -> None:
        entries = [self._entry(
            index_state="indexing",
            index_started_at=self._iso_hours_ago(0.5),
            source_uri="file:///a.pdf",
        )]
        r = self._run(monkeypatch, entries)
        assert r.ok is True
        assert "none" in r.detail

    def test_complete_document_is_never_flagged(self, monkeypatch) -> None:
        entries = [self._entry(
            index_state="complete",
            index_started_at=self._iso_hours_ago(1000),
            source_uri="file:///a.pdf",
        )]
        r = self._run(monkeypatch, entries)
        assert r.ok is True
        assert "none" in r.detail

    def test_checked_zero_is_skipped_not_clean(self, monkeypatch) -> None:
        """NON-VACUITY: an engine that predates the fence never sends the
        index_state KEY at all (index_state_reported=False); that must
        never render as a clean 'none (0 checked)' pass.

        nexus-vw594 F3 / nexus-biq4x: this is now the genuinely-unsupported
        case, distinct from a fence-aware engine reporting the key with a
        NULL value (see TestDoctorFenceCoverageDistinction below) — the
        two used to be indistinguishable through dict.get() and collapsed
        onto this exact message for both, which was nexus-biq4x's bug.
        """
        entries = [self._entry(index_state=None, index_state_reported=False)]
        r = self._run(monkeypatch, entries)
        assert r.ok is True
        assert "skipped" in r.detail
        assert "predates the index-run fence" in r.detail
        assert "none (" not in r.detail

    def test_catalog_unavailable_degrades_to_skip(self, monkeypatch) -> None:
        import nexus.health as h
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: None, raising=False,
        )
        r = h._check_stale_indexing_runs()[0]
        assert r.ok is True
        assert "no catalog" in r.detail


class TestDoctorFenceCoverageDistinction:
    """nexus-vw594 F3 (production test #4, root cause of nexus-biq4x):
    ``_check_stale_indexing_runs`` must distinguish "index_state reported
    but NULL on every row" (a fence-aware engine that simply has nothing
    stamped) from "index_state key never sent" (a genuinely pre-fence
    engine) — ``dict.get("index_state")`` alone cannot tell these apart
    (both read as Python ``None``), which is exactly what let a fully
    fence-aware, 100%-uncovered production install (the nexus-vw594
    incident) render as a green pre-fence skip.

    KILL CONTROL (documented per the task's TDD contract, not a separate
    test — verified manually during implementation, 2026-08-04): reverting
    the ``index_state_reported`` disambiguation in ``_to_entry``
    (http_catalog_client.py) — i.e. dropping the
    ``index_state_reported="index_state" in d`` line so the field always
    defaults ``True`` regardless of wire shape — collapses this test back
    onto the OLD behaviour: ``test_reported_null_after_fence_release_warns``
    goes from WARN to a false "skipped ... predates the fence" (still
    passes as a *string* match against the wrong branch, which is why the
    stronger regression signal is `test_checked_zero_is_skipped_not_clean`
    above going RED — with the revert, an ``index_state_reported=False``
    fixture entry no longer has any way to reach ``not_reported`` at all,
    since the flag is gone from the wire-parsing path; that fixture then
    falls into ``reported_null`` and this file's WARN test starts
    asserting on the SAME code path as the pre-fence test, which is the
    coverage-gap regression this test exists to catch).
    """

    def _entry(self, *, index_state, indexed_at="", index_state_reported=True,
               source_uri="", tumbler="", index_run_id=""):
        return type("E", (), {
            "index_state": index_state,
            "index_started_at": "",
            "indexed_at": indexed_at,
            "index_state_reported": index_state_reported,
            "source_uri": source_uri,
            "tumbler": tumbler,
            # nexus-2sa6w: _fence_begin mints a uuid4 PER DOCUMENT;
            # _fence_begin_many shares ONE across a flush. A run id on 2+
            # documents is proof of the begin-many route (v7.3.0+).
            "index_run_id": index_run_id,
        })()

    def _batch(self, run_id, indexed_at, *names):
        """A begin-many flush: N documents sharing ONE run id."""
        return [
            self._entry(
                index_state="complete", index_state_reported=True,
                indexed_at=indexed_at, index_run_id=run_id,
                source_uri=f"chroma://code__nexus/{n}",
            )
            for n in names
        ]

    def _baseline(self, indexed_at="2026-08-07T17:00:00+00:00"):
        """nexus-oiu1t: a STAMPED document — the install's own evidence that it
        has run a fully-fenced client. Without one in the fixture the check can
        no longer attribute anything, by design, so every producer-regression
        test must establish a baseline first."""
        return self._entry(
            index_state="complete", index_state_reported=True,
            indexed_at=indexed_at,
            source_uri="chroma://code__nexus/baseline.py",
        )

    def _cat(self, entries):
        class _Cat:
            def all_documents(self, limit=0):
                return list(entries)
        return _Cat()

    def _run(self, monkeypatch, entries):
        return self._run_all(monkeypatch, entries)[0]

    def _run_all(self, monkeypatch, entries):
        import nexus.health as h
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: self._cat(entries), raising=False,
        )
        return h._check_stale_indexing_runs()

    def test_reported_null_after_fence_release_warns(self, monkeypatch) -> None:
        """The producer-regression signature: index_state reported-but-NULL,
        with at least one document indexed AFTER FULL PRODUCER COVERAGE
        shipped (v7.3.0, 2026-08-07T16:00:35Z) — an unfenced producer wrote
        it. Must WARN, never ok=True, and must NOT reuse the pre-fence
        "predates" wording (that wording asserts something false: this
        engine DOES report the fence field).

        nexus-apig6: this fixture's date used to be 2026-08-04, which is
        BEFORE full coverage shipped and is therefore no longer the
        signature at all — see
        ``test_indexed_between_first_fence_and_full_coverage_does_not_warn``.
        """
        entries = [
            self._baseline(),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-10T16:03:00+00:00",
                source_uri="chroma://code__nexus/foo.py",
            ),
        ]
        r = self._run(monkeypatch, entries)
        assert r.ok is False
        assert r.warn is True
        assert "predates the index-run fence" not in r.detail
        assert "Two explanations fit" in r.detail
        # nexus-apig6: the offending document is NAMED, so the operator can
        # scope the remedy instead of re-embedding the corpus.
        assert "chroma://code__nexus/foo.py" in r.detail

    def test_indexed_between_first_fence_and_full_coverage_does_not_warn(
        self, monkeypatch,
    ) -> None:
        """nexus-apig6 REGRESSION PIN — the false positive that reached a
        downstream install (460 of 462 documents flagged, filed as an open
        upstream bug).

        The threshold used to be v7.1.0's 2026-08-02T22:26Z, the tag time of
        the FIRST client fence — which stamped at exactly 4 PDF/md/dt ingest
        sites. Every other producer (repo-index ``_batch_flush``,
        ``store_put``, code/prose indexers, memory, MCP) was legitimately
        unfenced until f55435eb, public at v7.3.0 on 2026-08-07T16:00:35Z.

        A document written anywhere in that five-day window by one of those
        producers is expected, permanent no-backfill debt. It must NOT be
        reported as a producer regression, and must NOT draw a
        whole-corpus-re-embed remedy.
        """
        entries = [
            self._baseline(),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-04T16:03:00+00:00",
                source_uri="chroma://code__nexus/foo.py",
            ),
        ]
        r = self._run(monkeypatch, entries)
        assert r.ok is True, (
            "a document indexed inside the 08-02..08-07 partial-coverage "
            f"window is not a producer regression: {r.detail}"
        )
        assert r.warn is not True
        assert "Two explanations fit" not in r.detail
        assert "chroma://code__nexus/foo.py" not in r.detail, (
            "a pre-coverage document must not be NAMED as a regression: "
            f"{r.detail}"
        )

    def test_regression_just_after_v7_3_0_is_not_absorbed_as_legacy(
        self, monkeypatch,
    ) -> None:
        """nexus-apig6 ROUND-2 PIN (code-review-expert, 2026-08-11).

        The first fix attempt anchored on v7.5.0 (2026-08-09T22:45:30Z) on the
        unverified claim that full producer coverage shipped there. It did
        not: f55435eb is an ancestor of v7.3.0 (2026-08-07T16:00:35Z), two
        releases earlier —

            git tag --contains f55435eb --sort=creatordate | grep '^v' | head -1
            git merge-base --is-ancestor f55435eb v7.2.0   # non-zero

        so a genuine producer regression landing in the 08-07..08-09 gap was
        silently absorbed into the "legacy, no action needed" bucket. Same
        defect class the bead exists to close, merely narrower — and NO
        fixture in this class exercised that window, which is exactly why the
        wrong anchor survived a green suite.

        This document is post-coverage and MUST WARN.
        """
        entries = [
            self._baseline(indexed_at="2026-08-07T17:00:00+00:00"),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-08T12:00:00+00:00",
                source_uri="chroma://code__nexus/in_the_gap.py",
            ),
        ]
        r = self._run(monkeypatch, entries)
        assert r.ok is False and r.warn is True, (
            "a document indexed 2026-08-08 is AFTER full producer coverage "
            f"(v7.3.0, 2026-08-07T16:00:35Z) and must warn: {r.detail}"
        )
        assert "Two explanations fit" in r.detail
        assert "chroma://code__nexus/in_the_gap.py" in r.detail

    def test_undated_reported_null_is_not_claimed_as_pre_coverage(
        self, monkeypatch,
    ) -> None:
        """nexus-apig6 (code-review-expert, 2026-08-11): a reported-but-NULL
        document with NO usable ``indexed_at`` cannot be attributed to either
        side of the coverage boundary. The summary must not fold it into the
        "indexed before full producer coverage shipped, no action needed"
        claim — that asserts something no evidence supports, which is the
        exact overclaiming class this whole bead is about.
        """
        entries = [
            self._entry(
                index_state=None, index_state_reported=True, indexed_at="",
                source_uri="chroma://code__nexus/undated.py",
            ),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="not-a-timestamp",
                source_uri="chroma://code__nexus/garbage_date.py",
            ),
        ]
        r = self._run(monkeypatch, entries)
        assert r.ok is True
        assert "fence live" in r.detail
        assert "2 carry no usable indexed_at" in r.detail
        assert "cannot be attributed" in r.detail
        assert "0 indexed before" in r.detail, (
            "with both documents undated, ZERO are attributable to the "
            f"pre-coverage side: {r.detail}"
        )

    def test_late_upgrader_docs_after_the_release_but_before_its_own_baseline(
        self, monkeypatch,
    ) -> None:
        """nexus-oiu1t — THE CORE CASE. A release-tag anchor assumes the user
        upgraded the day the release shipped. This install did not: it kept
        writing with a pre-coverage client until 2026-09-15, when it finally
        upgraded and its first document got stamped.

        Its documents from 2026-08-20 and 2026-09-01 are dated well AFTER full
        producer coverage shipped (2026-08-07) and carry no stamp — under a
        pure release-tag anchor every one of them is reported as a producer
        regression with a re-embed prescribed. That is the exact false
        positive nexus-apig6 was filed for, displaced onto a later population.

        The install's OWN earliest stamp is the honest anchor: before it,
        nothing here had a fenced client.
        """
        entries = [
            self._baseline(indexed_at="2026-09-15T10:00:00+00:00"),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-20T00:00:00+00:00",
                source_uri="chroma://code__nexus/late_a.py",
            ),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-09-01T00:00:00+00:00",
                source_uri="chroma://code__nexus/late_b.py",
            ),
        ]
        r = self._run(monkeypatch, entries)
        assert r.ok is True, (
            "documents written before this install's own first stamp were "
            f"written by a pre-coverage client, not a broken one: {r.detail}"
        )
        assert "Two explanations fit" not in r.detail
        assert "late_a.py" not in r.detail and "late_b.py" not in r.detail

    def test_late_upgrader_still_catches_a_real_regression_after_baseline(
        self, monkeypatch,
    ) -> None:
        """nexus-oiu1t NON-VACUITY CONTROL for the test above. Moving the
        anchor to install-local evidence must not blind the check: the SAME
        late-upgrader install, with one additional unstamped document written
        AFTER its baseline, must still WARN and name only that document.

        Without this, the previous test could be satisfied by a check that
        simply never warns.
        """
        entries = [
            self._baseline(indexed_at="2026-09-15T10:00:00+00:00"),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-09-01T00:00:00+00:00",
                source_uri="chroma://code__nexus/late_b.py",
            ),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-09-20T00:00:00+00:00",
                source_uri="chroma://code__nexus/real_regression.py",
            ),
        ]
        r = self._run(monkeypatch, entries)
        assert r.ok is False and r.warn is True
        assert "1 document(s) report index_state" in r.detail, (
            f"only the post-baseline document is a regression: {r.detail}"
        )
        assert "real_regression.py" in r.detail
        assert "late_b.py" not in r.detail

    def test_pre_coverage_document_is_never_a_candidate(
        self, monkeypatch,
    ) -> None:
        """nexus-oiu1t: the coverage FLOOR is applied when candidates are
        gathered, and it is load-bearing independently of the install-local
        anchor.

        Here the install's own earliest stamp is dated 2026-07-01 — impossible,
        since no client could stamp before the code to stamp existed, so it is
        clock skew, a restored backup, or a hand-edited row. If the floor were
        dropped from the walk, that bogus stamp would become the anchor and
        every document in the 08-02..08-07 partial-coverage window would be
        reported as a regression again, resurrecting nexus-apig6's false
        positive through a different door.

        Kill control (run 2026-08-11): deleting the
        ``ia_dt > _PRODUCER_FENCE_RELEASE_DT`` guard from the walk turns this
        test RED. Note the ORIGINAL form of this test targeted a ``max()`` in
        the anchor derivation instead and passed with that max removed — it
        was vacuous, because the walk-level floor already subsumed it. The
        max() was deleted as dead code and this test re-pointed at the guard
        that actually carries the floor.
        """
        entries = [
            self._baseline(indexed_at="2026-07-01T00:00:00+00:00"),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-04T00:00:00+00:00",
                source_uri="chroma://code__nexus/in_partial_window.py",
            ),
        ]
        r = self._run(monkeypatch, entries)
        assert r.ok is True, (
            "an impossible pre-release stamp must not lower the anchor below "
            f"the coverage floor: {r.detail}"
        )
        assert "in_partial_window.py" not in r.detail

    def test_early_tier_stamp_does_not_let_the_warn_assert_a_regression(
        self, monkeypatch,
    ) -> None:
        """nexus-2sa6w — the compound scenario BOTH reviewers reproduced
        independently (code-review-expert Critical + substantive-critic
        Critical, 2026-08-11), and which no other fixture here covers.

        The fence rolled out in TWO tiers. v7.1.0 (4b0c5fb5) fenced 4 sites,
        all PDF/md/DEVONthink ingest; the remaining producers (code_indexer,
        prose_indexer, store_put, ...) only at f55435eb / v7.3.0. Nothing on a
        catalog row records WHICH producer or client version stamped it.

        So an install still on v7.1.0/v7.2.x can run one PDF ingest, get a
        stamp, and establish an anchor — while its repo-index and store_put
        writes stay LEGITIMATELY unfenced. Here: stamp at 2026-08-05, a
        legitimate unfenced write at 2026-08-09.

        The anchor is real, so this WARNs — that part is correct and must not
        be silenced (a genuine regression looks identical from here). What it
        must NOT do is assert the accusatory explanation, which is what the
        first cut of oiu1t did ("this is a NEW coverage regression — find the
        producer"). It must name BOTH explanations and point at the cheap
        discriminator, the client version.

        A real discriminator (per-document producer/client provenance, or
        restricting anchor evidence to the late-fenced producer family) is
        tracked as nexus-2sa6w. Until then this check states what it knows.
        """
        entries = [
            self._entry(
                index_state="complete", index_state_reported=True,
                indexed_at="2026-08-05T00:00:00+00:00",
                source_uri="chroma://knowledge__nexus/early_tier_pdf.pdf",
            ),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-09T00:00:00+00:00",
                source_uri="chroma://code__nexus/legit_unfenced.py",
            ),
        ]
        r = self._run(monkeypatch, entries)
        assert r.ok is False and r.warn is True
        assert "legit_unfenced.py" in r.detail
        # The accusation must be offered as ONE of two readings, never asserted.
        assert "Two explanations fit" in r.detail
        assert "v7.1.0-v7.2.x" in r.detail, (
            "the partial-coverage-client explanation must be named, not "
            f"implied: {r.detail}"
        )
        assert any("nx --version" in s for s in (r.fix_suggestions or [])), (
            "the cheap discriminator must be the first thing suggested: "
            f"{r.fix_suggestions}"
        )

    def test_proven_anchor_supersedes_an_early_tier_poisoned_anchor(
        self, monkeypatch,
    ) -> None:
        """nexus-2sa6w — THE FIX. The compound scenario, plus the evidence that
        resolves it.

        Same poisoned setup as the test above: a v7.1.0-era client stamps a PDF
        on 2026-08-05 (unshared run id, an early-tier stamp), and writes an
        unfenced repo-index document on 2026-08-09 while still partially
        covered. A bare-earliest-stamp anchor lands on 08-05 and flags the
        08-09 document.

        Then the install upgrades and runs a batched index on 2026-09-01 — two
        documents sharing ONE run id, which only `_fence_begin_many` produces,
        and that route exists only from f55435eb/v7.3.0. THAT is the earliest
        moment full coverage is PROVEN, so it becomes the anchor and the 08-09
        write falls before it: correctly unflagged, no longer a false positive.
        """
        entries = [
            self._entry(
                index_state="complete", index_state_reported=True,
                indexed_at="2026-08-05T00:00:00+00:00",
                index_run_id="early-tier-solo-run",
                source_uri="chroma://knowledge__nexus/early_tier.pdf",
            ),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-09T00:00:00+00:00",
                source_uri="chroma://code__nexus/legit_unfenced.py",
            ),
        ] + self._batch(
            "batched-flush-run", "2026-09-01T00:00:00+00:00", "a.py", "b.py",
        )
        r = self._run(monkeypatch, entries)
        assert r.ok is True, (
            "the proven anchor (2026-09-01) supersedes the poisoned early-tier "
            f"anchor (2026-08-05), so the 08-09 write is not flagged: {r.detail}"
        )
        assert "legit_unfenced.py" not in r.detail

    def test_proven_anchor_still_flags_a_later_write_and_asserts_it(
        self, monkeypatch,
    ) -> None:
        """nexus-2sa6w NON-VACUITY CONTROL. Same install, same proven anchor —
        one more unstamped document, written AFTER it.

        With full coverage PROVEN by a shared run id, the partial-coverage
        explanation is eliminated by evidence, so here (and only here) the
        check may state the regression rather than hedging. Without this test,
        the one above could be satisfied by a check that never flags anything.
        """
        entries = [
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-09T00:00:00+00:00",
                source_uri="chroma://code__nexus/legit_unfenced.py",
            ),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-09-10T00:00:00+00:00",
                source_uri="chroma://code__nexus/real_regression.py",
            ),
        ] + self._batch(
            "batched-flush-run", "2026-09-01T00:00:00+00:00", "a.py", "b.py",
        )
        r = self._run(monkeypatch, entries)
        assert r.ok is False and r.warn is True
        assert "real_regression.py" in r.detail
        assert "legit_unfenced.py" not in r.detail, (
            f"the pre-anchor write is not a regression: {r.detail}"
        )
        # Proven coverage: state it, do not hedge with the v7.1.0 alternative.
        assert "coverage regression worth finding" in r.detail
        assert "v7.1.0-v7.2.x" not in r.detail, (
            "the partial-coverage explanation is eliminated by a shared run "
            f"id and must not be offered: {r.detail}"
        )
        assert "Two explanations fit" not in r.detail
        assert not any("nx --version" in s for s in (r.fix_suggestions or [])), (
            "the client-version check is pointless once coverage is proven: "
            f"{r.fix_suggestions}"
        )

    def test_single_document_flush_shares_no_run_id_so_proves_nothing(
        self, monkeypatch,
    ) -> None:
        """nexus-2sa6w: the discriminator is EVIDENCE-POSITIVE ONLY. A
        begin-many flush containing exactly one document shares its run id with
        nothing, and is indistinguishable from a per-document `_fence_begin`
        stamp. Absence of sharing must therefore prove nothing and fall back to
        the hedged wording — never be read as "not full coverage".
        """
        entries = [
            self._entry(
                index_state="complete", index_state_reported=True,
                indexed_at="2026-08-05T00:00:00+00:00",
                index_run_id="lonely-run",
                source_uri="chroma://code__nexus/only.py",
            ),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-09T00:00:00+00:00",
                source_uri="chroma://code__nexus/later.py",
            ),
        ]
        r = self._run(monkeypatch, entries)
        assert r.ok is False and r.warn is True
        assert "Two explanations fit" in r.detail, (
            f"an unshared run id proves nothing; stay hedged: {r.detail}"
        )
        assert "v7.1.0-v7.2.x" in r.detail

    def test_crashed_batch_with_stale_indexed_at_cannot_prove_coverage(
        self, monkeypatch,
    ) -> None:
        """nexus-2sa6w round 2 — CRITICAL found by code-review-expert with a
        standalone repro (scratchpad/test_stale_ledger_poison.py), 2026-08-11.

        Verified in service/src/main/java/dev/nexus/service/db/
        CatalogRepository.java: beginIndexRun writes INDEX_STATE / INDEX_RUN_ID
        / INDEX_STARTED_AT; completeIndexRun writes INDEX_STATE /
        INDEX_CONTENT_HASH / CHUNK_COUNT. NEITHER writes INDEXED_AT — only the
        manifest-write/register path does.

        So a row stuck at 'indexing' carries an indexed_at from its PREVIOUS,
        unrelated successful index. Here a begin-many batch crashed mid-run:
        two rows share a run id and carry stale 2026-08-07T16:0x dates. Read
        naively that is "proof of full coverage at 08-07", which then flags a
        2026-08-09 write AND asserts it as a regression with the hedge and the
        `nx --version` suggestion both dropped. Nothing here is a regression —
        those two documents just had a crashed re-index.

        Only 'complete' rows may date the anchor. Kill control: removing the
        `if state != "complete": continue` guard turns this test RED.
        """
        entries = [
            self._entry(
                index_state="indexing", index_state_reported=True,
                indexed_at="2026-08-07T16:05:00+00:00",
                index_run_id="crashed-batch",
                source_uri="chroma://code__nexus/old_a.py",
            ),
            self._entry(
                index_state="indexing", index_state_reported=True,
                indexed_at="2026-08-07T16:06:00+00:00",
                index_run_id="crashed-batch",
                source_uri="chroma://code__nexus/old_b.py",
            ),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-09T00:00:00+00:00",
                source_uri="chroma://code__nexus/legit_unfenced.py",
            ),
        ]
        results = self._run_all(monkeypatch, entries)
        fence = [r for r in results if "cannot attribute" in r.detail
                 or "fence baseline" in r.detail]
        assert fence, f"expected a fence-attribution result: {results}"
        r = fence[0]
        assert "coverage regression worth finding" not in r.detail, (
            "a CRASHED begin-many batch proves nothing — its indexed_at "
            f"belongs to a previous index: {r.detail}"
        )
        assert "Two explanations fit" not in r.detail or (
            "v7.1.0-v7.2.x" in r.detail
        )

    def test_run_id_ledger_cap_reports_when_it_drops_evidence(
        self, monkeypatch,
    ) -> None:
        """nexus-2sa6w round 2 — substantive-critic Significant + code-review-
        expert Important, 2026-08-11. The ledger fills in WALK order, so any
        _MAX_TRACKED_RUN_IDS distinct run ids seen before a genuine
        multi-document batch cause that batch's proof to be dropped. Solo ids
        from interactive `nx store put` / `nx memory put` each mint their own
        uuid4, so a dogfooded install is a plausible way to hit it.

        It must fail SAFE (fall back to hedged wording, never a false
        accusation) and must SAY SO — a silent fallback here is
        indistinguishable from "no evidence exists", which is a different
        claim. Cap is monkeypatched rather than building a 5000-row fixture.
        """
        import nexus.health as h
        monkeypatch.setattr(h, "_MAX_TRACKED_RUN_IDS", 2, raising=True)
        entries = [
            self._entry(
                index_state="complete", index_state_reported=True,
                indexed_at="2026-08-08T00:00:00+00:00",
                index_run_id=f"solo-{i}",
                source_uri=f"chroma://knowledge__nexus/note{i}.md",
            )
            for i in range(2)
        ] + self._batch(
            "batched-run-past-the-cap", "2026-09-01T00:00:00+00:00",
            "a.py", "b.py",
        ) + [
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-20T00:00:00+00:00",
                source_uri="chroma://code__nexus/later.py",
            ),
        ]
        r = self._run(monkeypatch, entries)
        assert r.ok is False and r.warn is True
        # Fell back to hedged wording — the safe direction.
        assert "Two explanations fit" in r.detail
        # ...and said why, rather than implying no evidence existed.
        assert "ledger" in r.detail and "may have been missed" in r.detail, (
            f"cap-induced fallback must not be silent: {r.detail}"
        )

    def test_no_stamped_document_anywhere_cannot_attribute(
        self, monkeypatch,
    ) -> None:
        """nexus-oiu1t: with NO stamped document in the corpus, nothing
        establishes that this install has ever run a fully-fenced client. A
        late upgrader and a genuine producer regression are indistinguishable
        from here, and the release date cannot tell them apart.

        This still WARNS — staying silent here would hide a genuinely unfenced
        producer on an install that has never once stamped, which is the exact
        shape the vw594 incident had (and is pinned live by
        tests/test_cotmr_cli_store_fence.py::TestAcquireGateJourneyDoctorClean
        ::test_artificially_unfenced_document_still_warns). What nexus-apig6
        was filed for was not the existence of a warning but its CONTENT: an
        asserted producer bug it could not prove, remedied by re-embedding the
        whole corpus. So the WARN must state the ambiguity, must NOT assert a
        regression, and must prescribe ONE document.
        """
        entries = [
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-20T00:00:00+00:00",
                source_uri="chroma://code__nexus/unknown.py",
            ),
        ]
        r = self._run(monkeypatch, entries)
        assert r.ok is False and r.warn is True
        assert "cannot attribute" in r.detail
        assert "no document in this corpus has ever been stamped" in r.detail
        assert "a producer is unfenced (a regression worth finding)" not in r.detail, (
            f"must not ASSERT a producer regression it cannot prove: {r.detail}"
        )
        assert any("any ONE document" in s for s in (r.fix_suggestions or [])), (
            r.fix_suggestions
        )

    def test_warn_counts_only_post_coverage_docs_and_excuses_the_rest(
        self, monkeypatch,
    ) -> None:
        """nexus-apig6: a real corpus is MIXED — a large pre-coverage legacy
        population plus one genuine regression. The WARN must count and name
        only the regression, and must say out loud that the legacy rows need
        no action, or the operator reads the total as the blast radius (which
        is precisely what happened: 460 vs the 2 that mattered).
        """
        entries = [
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-07-01T00:00:00+00:00",
                source_uri=f"chroma://code__nexus/legacy{i}.py",
            )
            for i in range(5)
        ] + [
            self._baseline(),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-10T09:00:00+00:00",
                source_uri="chroma://code__nexus/regressed.py",
            ),
        ]
        r = self._run(monkeypatch, entries)
        assert r.ok is False and r.warn is True
        assert "1 document(s) report index_state" in r.detail, (
            f"must count only the post-coverage document, not all 6: {r.detail}"
        )
        assert "chroma://code__nexus/regressed.py" in r.detail
        assert "legacy0.py" not in r.detail
        assert "other 5 reported-but-NULL document(s)" in r.detail
        assert "need no action" in r.detail

    def test_reported_null_before_fence_release_stays_ok_with_honest_message(self, monkeypatch) -> None:
        """Quiescent case (nexus-biq4x's own prescribed fix): the fence is
        live but nothing has run through it yet (fresh install / stable
        corpus). Stays ok=True — but with the "fence live" signal, never
        the misleading pre-fence message."""
        entries = [
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-07-01T00:00:00+00:00",
                source_uri="chroma://code__nexus/foo.py",
            ),
        ]
        r = self._run(monkeypatch, entries)
        assert r.ok is True
        assert "predates the index-run fence" not in r.detail
        assert "fence live" in r.detail

    def test_mixed_corpus_checked_and_reported_null_both_warn(self, monkeypatch) -> None:
        """CRITICAL regression (substantive-critic, T2
        nexus/vw594-critique-2026-08-05 [21445], repro:
        scratchpad/verify_mixed.py): the vw594-signature check used to be
        nested inside ``if checked == 0``, so a mixed corpus — one
        document with a genuine fenced run (checked > 0, e.g. the first
        ``nx index repo`` after deploy) PLUS a second, unrelated document
        that reports index_state but was never stamped after the fence
        shipped — fell through to the generic ok=True "none (N checked)"
        branch without the reported_null population ever being
        inspected. That is nexus-biq4x's silent-green bug reborn under a
        NEW trigger (checked > 0 instead of "engine predates the fence").
        This fixture is deliberately NON-uniform (one checked doc, one
        reported_null doc) — every fixture before this one in this class
        was uniform, which is exactly why 7/7 green was false confidence.
        """
        entries = [
            self._entry(
                index_state="complete", index_state_reported=True,
                indexed_at="2026-08-03T00:00:00+00:00",
                source_uri="chroma://code__nexus/checked.py",
            ),
            self._entry(
                index_state=None, index_state_reported=True,
                indexed_at="2026-08-10T16:03:00+00:00",  # nexus-apig6: post-v7.5.0
                source_uri="chroma://code__nexus/unfenced.py",
            ),
        ]
        results = self._run_all(monkeypatch, entries)
        warns = [r for r in results if r.warn]
        assert warns, (
            f"expected at least one WARN result from a mixed checked+"
            f"reported_null corpus, got: {results}"
        )
        assert any(r.ok is False for r in warns)
        assert not any("none (" in r.detail and r.ok for r in results), (
            "the checked>0 summary must not silently supersede the "
            f"reported_null WARN: {results}"
        )
        assert any(
            "predates the index-run fence" not in r.detail
            and "Two explanations fit" in r.detail
            for r in warns
        )


# ── nexus-0ehwe item 4: tumbler allocator drift ──────────────────────────────


class TestCheckNextSeqDrift:
    """Owners whose allocator has fallen behind their own children.

    The engine now floors past drift, so a drifted owner SELF-HEALS on its next
    registration. This check exists because that healing is SILENT: without it
    the blast radius of the original wedge (nexus-pbawi) stays guessed, and an
    owner never written to again sits drifted indefinitely.
    """

    def _cat(self, owners, tumblers):
        cat = MagicMock()
        cat.list_owners.return_value = owners
        cat.all_documents.return_value = [
            type("E", (), {"tumbler": t})() for t in tumblers
        ]
        return cat

    def _run(self, monkeypatch, owners, tumblers):
        import nexus.health as h
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: self._cat(owners, tumblers), raising=False,
        )
        return h._check_next_seq_drift()[0]

    def test_corpus_walked_once_regardless_of_owner_count(self, monkeypatch) -> None:
        """The quadratic-scan tripwire (nexus-ohxzu, 2026-08-02).

        The per-owner helper re-walked all_documents PER OWNER — 65 owners x
        ~22k docs = ~1.4M records over the managed API, measured at 218s of a
        224s doctor. The single-pass rewrite computes every owner's max in one
        walk; this pins call-count == 1 so the loop can never regress back
        inside the owner iteration.
        """
        owners = [
            {"tumbler_prefix": f"1.{i}", "next_seq": 1} for i in range(1, 26)
        ]
        cat = self._cat(owners, ["1.1.1", "1.25.1"])
        import nexus.health as h
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: cat, raising=False,
        )
        h._check_next_seq_drift()
        assert cat.all_documents.call_count == 1, (
            f"all_documents called {cat.all_documents.call_count}x for "
            f"{len(owners)} owners — the O(owners x corpus) scan is back"
        )

    def test_multiple_owners_disambiguated_in_one_shared_walk(self, monkeypatch) -> None:
        """Cross-owner correctness under the shared-dict pass (critic,
        e265d1b8 review): one drifted owner and one clean owner computed from
        the SAME walk must each report correctly — bucket contamination
        between owners is the regression class the per-owner architecture
        could not have and this one can.
        """
        import nexus.health as h
        r = self._run(
            monkeypatch,
            [
                {"tumbler_prefix": "1.12", "next_seq": 3},   # drifted (high=7)
                {"tumbler_prefix": "1.14", "next_seq": 9},   # clean  (high=9)
            ],
            ["1.12.1", "1.12.7", "1.14.2", "1.14.9"],
        )
        assert r.ok is False and r.warn is True
        assert "1.12" in r.detail and "highest child=7" in r.detail
        assert "1.14" not in r.detail, (
            f"clean owner leaked into the drift report: {r.detail}"
        )

    def test_scan_failure_retries_once_then_skips_loudly(self, monkeypatch) -> None:
        """The shared walk's blast radius (reviewer Important, e265d1b8): a
        mid-walk failure blanks drift visibility for EVERY owner, so the
        check retries once and the concession names the scope. Pins
        call_count == 2 (exactly one retry) and the all-owners detail text.
        """
        import nexus.health as h
        cat = MagicMock()
        cat.list_owners.return_value = [{"tumbler_prefix": "1.12", "next_seq": 3}]
        cat.all_documents.side_effect = RuntimeError("mid-walk blip")
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: cat, raising=False,
        )
        r = h._check_next_seq_drift()[0]
        assert r.ok is True and "ANY owner" in r.detail
        assert cat.all_documents.call_count == 2

    def test_drifted_owner_is_named(self, monkeypatch) -> None:
        r = self._run(
            monkeypatch,
            [{"tumbler_prefix": "1.12", "next_seq": 3}],
            ["1.12.1", "1.12.7"],
        )
        assert r.ok is False and r.warn is True
        assert "1.12" in r.detail and "next_seq=3" in r.detail
        assert "highest child=7" in r.detail

    def test_healthy_owner_is_clean(self, monkeypatch) -> None:
        r = self._run(
            monkeypatch,
            [{"tumbler_prefix": "1.12", "next_seq": 9}],
            ["1.12.1", "1.12.7"],
        )
        assert r.ok is True and "none" in r.detail

    def test_equality_is_the_healthy_steady_state_not_drift(
        self, monkeypatch,
    ) -> None:
        """next_seq == highest child is NORMAL, and this is the boundary the
        original ``<=`` predicate got wrong (nexus-k5sdi).

        ``next_seq`` holds the LAST CLAIMED sequence, not the next to hand out:
        claimNextSeq computes ``max(next_seq, high_water) + 1`` and stores the
        claim, so equality holds after EVERY successful registration. The old
        predicate therefore flagged every owner that had ever been written to —
        including both owners a virgin install creates, which is how a fresh
        box failed its own MVV with a warning describing correct behaviour.
        """
        r = self._run(
            monkeypatch,
            [{"tumbler_prefix": "1.12", "next_seq": 7}],
            ["1.12.1", "1.12.7"],
        )
        assert r.ok is True, f"equality must not read as drift: {r.detail}"
        assert "none" in r.detail

    def test_one_below_high_water_is_still_drift(self, monkeypatch) -> None:
        """The tightened predicate must not blunt the real detection.

        next_seq one below the high-water mark is the nexus-pbawi wedge in its
        smallest form; narrowing ``<=`` to ``<`` must keep catching it.
        """
        r = self._run(
            monkeypatch,
            [{"tumbler_prefix": "1.12", "next_seq": 6}],
            ["1.12.1", "1.12.7"],
        )
        assert r.ok is False and r.warn is True
        assert "next_seq=6" in r.detail and "highest child=7" in r.detail

    def test_fresh_owner_with_its_first_document_is_clean(
        self, monkeypatch,
    ) -> None:
        """The exact virgin-install shape from the failing MVV: an owner whose
        first registration claimed 1, leaving next_seq=1 and one child at 1."""
        r = self._run(
            monkeypatch,
            [{"tumbler_prefix": "1.1", "next_seq": 1}],
            ["1.1.1"],
        )
        assert r.ok is True, f"a brand-new owner must be clean: {r.detail}"

    def test_engine_without_next_seq_reads_as_skipped_not_clean(
        self, monkeypatch,
    ) -> None:
        """NON-VACUITY: an engine predating the nexus-0ehwe change omits the
        field, and every owner would then look drift-free. That must render as
        SKIPPED — a check whose silent-pass mode is 'the data was absent' is
        the failure this whole class is about."""
        r = self._run(monkeypatch, [{"tumbler_prefix": "1.12"}], ["1.12.7"])
        assert r.ok is True
        assert "skipped" in r.detail and "next_seq" in r.detail
        assert "none (" not in r.detail

    def test_deeper_addresses_are_not_mistaken_for_children(
        self, monkeypatch,
    ) -> None:
        """'1.12.3.4' is a chunk address, not a child sequence of 1.12."""
        r = self._run(
            monkeypatch,
            [{"tumbler_prefix": "1.12", "next_seq": 2}],
            ["1.12.1", "1.12.3.4"],
        )
        assert r.ok is True, f"deeper address counted as a child: {r.detail}"
