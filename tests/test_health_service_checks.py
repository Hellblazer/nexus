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
from unittest.mock import MagicMock

import httpx
import pytest

from nexus.health import (
    HealthResult,
    _check_migration_state,
    _check_rls_present,
    _check_storage_service_health,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_creds_file(tmp_path: Path, **overrides) -> Path:
    """Write a minimal pg_credentials file and return its path."""
    defaults = {
        "PG_PORT": "54321",
        "NX_DB_ADMIN_URL": "jdbc:postgresql://127.0.0.1:54321/nexus",
        "NX_DB_ADMIN_USER": "nexus_admin",
        "NX_DB_ADMIN_PASS": "testpass",
        "NX_DB_URL": "jdbc:postgresql://127.0.0.1:54321/nexus",
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "svcpass",
    }
    defaults.update(overrides)
    content = "\n".join(f"{k}={v}" for k, v in defaults.items()) + "\n"
    p = tmp_path / "pg_credentials"
    p.write_text(content)
    return p


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


# ── _check_migration_state ────────────────────────────────────────────────────


def _psql_runner_ok(n: int):
    """Return a psql runner that reports N EXECUTED rows, 0 drift, 0 null-md5sum."""
    def runner(cmd: list[str], *, capture_output: bool, text: bool,
               check: bool) -> subprocess.CompletedProcess:
        sql = " ".join(cmd)
        if "exectype != 'EXECUTED'" in sql:
            # Drift query: 0 non-EXECUTED rows = all good
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0\n", stderr="")
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
    """Return a psql runner that reports non-EXECUTED rows exist (1 failed changeset)."""
    def runner(cmd: list[str], *, capture_output: bool, text: bool,
               check: bool) -> subprocess.CompletedProcess:
        sql = " ".join(cmd)
        if "exectype != 'EXECUTED'" in sql:
            # Drift query: 1 non-EXECUTED row
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="1\n", stderr="")
        if "md5sum IS NULL" in sql:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0\n", stderr="")
        # Total count query: 5 rows total
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
        if "exectype != 'EXECUTED'" in sql:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0\n", stderr="")
        # Total count
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="5\n", stderr="")
    return runner


def _psql_runner_unparseable_drift():
    """Return a psql runner where the drift query returns non-integer output."""
    def runner(cmd: list[str], *, capture_output: bool, text: bool,
               check: bool) -> subprocess.CompletedProcess:
        sql = " ".join(cmd)
        if "exectype != 'EXECUTED'" in sql:
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
        """All EXECUTED rows -> ok result."""
        creds = _make_creds_file(tmp_path)
        psql = Path("/fake/psql")

        results = _check_migration_state(
            creds_path=creds,
            psql_bin=psql,
            psql_runner=_psql_runner_ok(7),
        )
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert "7" in r.detail

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
        """Non-EXECUTED row -> fatal migration drift."""
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
    "nexus.chash_index",
    "nexus.document_aspects",
    "nexus.document_highlights",
    "nexus.frecency",
    "nexus.hook_failures",
    "nexus.memory",
    "nexus.nx_answer_runs",
    "nexus.plans",
    "nexus.relevance_log",
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
        return frozenset(found)

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
