# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the three storage-service health checks introduced in bead nexus-gmiaf.33.

Tests are FAST (no subprocesses, no real PG, no network) — psql runner and HTTP
client are injected as callables so unit tests exercise all parsing/result logic
in-process.

Integration tests (marked @pytest.mark.integration) live in
tests/db/test_health_service_integration.py and require the real JAR + PG16.
"""
from __future__ import annotations

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
    _check_storage_service_health,
    _check_t2_launchagent_stray,
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
        """No pg_credentials file -> soft warn, not fatal."""
        missing_creds = tmp_path / "pg_credentials"  # does not exist

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


class TestCheckT2LaunchagentStray:
    """nx doctor backstop for the automatic stray-com.nexus.t2-LaunchAgent
    removal — surfaces the condition even outside a version transition."""

    def test_local_mode_yields_no_result(self):
        from nexus.db.storage_mode import StorageBackend

        with patch(
            "nexus.db.storage_mode.storage_backend_for",
            return_value=StorageBackend.SQLITE,
        ), patch("nexus.commands.daemon._autostart_unit_installed") as probe:
            results = _check_t2_launchagent_stray()
        assert results == []
        probe.assert_not_called()

    def test_service_mode_no_agent_returns_ok(self):
        from nexus.db.storage_mode import StorageBackend

        with patch(
            "nexus.db.storage_mode.storage_backend_for",
            return_value=StorageBackend.SERVICE,
        ), patch(
            "nexus.commands.daemon._autostart_unit_installed", return_value=None,
        ):
            results = _check_t2_launchagent_stray()
        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].fatal is False

    def test_service_mode_with_agent_returns_soft_warn(self, tmp_path):
        from nexus.db.storage_mode import StorageBackend

        dest = tmp_path / "com.nexus.t2.plist"
        with patch(
            "nexus.db.storage_mode.storage_backend_for",
            return_value=StorageBackend.SERVICE,
        ), patch(
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

    def test_probe_failure_degrades_silently(self):
        with patch(
            "nexus.db.storage_mode.storage_backend_for",
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
        """nexus-pnwu0 / GH #1390: non-32-char chash rows -> a WARNING with
        the do-not-upgrade + runbook remediation, plus the still-ok Schema
        migrations result. Never fatal (the current engine serves fine).
        The count is SUMMED across the chash-bearing tables via nexus_diag."""
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
        assert any("Do NOT upgrade" in s for s in chash.fix_suggestions)
        assert any("§8.1" in s for s in chash.fix_suggestions)
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
    "nexus.chash_alias",
    "nexus.catalog_links",
    "nexus.catalog_meta",
    "nexus.catalog_owners",
    # ("nexus.chash_index" removed — RDR-187/nexus-piwya.9: dropped table,
    # mirrors health._RLS_TENANT_TABLES)
    "nexus.chash_remap",
    "nexus.claude_assisted_remediation_consents",
    "nexus.document_aspects",
    "nexus.document_highlights",
    "nexus.frecency",
    "nexus.hook_failures",
    "nexus.ladder_completions",
    "nexus.memory",
    "nexus.migration_jobs",
    "nexus.nx_answer_runs",
    "nexus.pdf_chunks",
    "nexus.pdf_pages",
    "nexus.pdf_pipeline",
    "nexus.plans",
    "nexus.relevance_log",
    "nexus.retention_markers",
    "nexus.search_telemetry",
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
        assert "8 chunk row(s)" in chash[0].detail  # 2 per table via LEGACY (4 poison tables post-RDR-187)
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
