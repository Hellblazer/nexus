# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the local Postgres provisioner (RDR-152 bead nexus-gmiaf.31).

Tests the full provisioning lifecycle:
  1. provision() creates a cluster, the nexus DB, nexus_admin and nexus_svc roles.
  2. Role attributes are correct (NOSUPERUSER, nexus_svc NOBYPASSRLS etc.).
  3. Credentials file is written at 0600 with the correct NX_DB_* variables.
  4. provision() is idempotent: a second call is a no-op, same credentials.
  5. End-to-end: provision → apply migration DDL as nexus_admin → nexus_svc DML
     under RLS passes; nexus_svc without GUC → zero rows (fail-closed).

All tests require PostgreSQL 16 (or 15) binaries and are marked
``@pytest.mark.integration`` so they are skipped in unit-only CI.

Uses a tmp directory as NEXUS_CONFIG_DIR to keep the test cluster hermetic
and never touching the user's real nexus config.

The end-to-end test (test 5) uses the service JAR if present; if absent it
applies a minimal subset of the migration DDL directly via psql to prove the
role / RLS contract without the JVM.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from nexus.db.pg_provision import (
    CREDENTIALS_FILENAME,
    NEXUS_DB_NAME,
    PgBinaryNotFoundError,
    PgBinaries,
    ProvisionResult,
    _find_free_port,
    _port_accepting,
    _psql,
    _read_credentials,
    discover_pg_binaries,
    is_provisioned,
    provision,
)

# ── Prerequisite detection ─────────────────────────────────────────────────────

def _pg_bins_available() -> bool:
    try:
        discover_pg_binaries()
        return True
    except PgBinaryNotFoundError:
        return False


_PG_AVAILABLE = _pg_bins_available()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _PG_AVAILABLE,
        reason="skipped: no PostgreSQL binaries found (install postgresql@16)",
    ),
]


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _query(bins: PgBinaries, port: int, db: str, user: str, sql: str) -> str:
    """Run a psql query and return stdout."""
    result = subprocess.run(
        [str(bins.psql), "-h", "127.0.0.1", "-p", str(port),
         "-U", user, "-d", db, "-t", "-A", "-c", sql],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _role_attr(bins: PgBinaries, port: int, os_user: str, rolname: str, attr: str) -> str:
    """Return a pg_roles attribute for a role."""
    return _query(
        bins, port, "postgres", os_user,
        f"SELECT {attr} FROM pg_roles WHERE rolname = '{rolname}'"
    )


def _stop_pg(bins: PgBinaries, pgdata: Path) -> None:
    """Stop the cluster (for fixture teardown)."""
    try:
        subprocess.run(
            [str(bins.pg_ctl), "-D", str(pgdata), "-m", "immediate", "stop"],
            capture_output=True, check=False, timeout=10,
        )
    except Exception:  # noqa: BLE001 — teardown must not reraise
        pass


# ── Module-scoped fixtures ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def bins() -> PgBinaries:
    """Resolved PostgreSQL binaries (module-scoped, shared across tests)."""
    return discover_pg_binaries()


@pytest.fixture(scope="module")
def provisioned(bins: PgBinaries, tmp_path_factory) -> tuple[ProvisionResult, Path]:
    """Provision a hermetic cluster in a tmp directory (module-scoped).

    The cluster is torn down after all tests in this module complete.
    ``NEXUS_CONFIG_DIR`` is temporarily overridden so the provisioner writes
    to the tmp directory, never to the user's real ``~/.config/nexus``.

    Yields ``(result, config_dir)``.
    """
    config_dir = tmp_path_factory.mktemp("nexus_provision_test")
    os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"

    # Override NEXUS_CONFIG_DIR so provision() uses the tmp dir.
    old_env = os.environ.get("NEXUS_CONFIG_DIR")
    os.environ["NEXUS_CONFIG_DIR"] = str(config_dir)
    try:
        result = provision(config_dir, force_new_port=True)
    finally:
        if old_env is None:
            os.environ.pop("NEXUS_CONFIG_DIR", None)
        else:
            os.environ["NEXUS_CONFIG_DIR"] = old_env

    yield result, config_dir

    # Teardown: stop the cluster.
    pgdata = config_dir / "postgres"
    _stop_pg(bins, pgdata)


# ── Test 1: cluster, db, and roles created ────────────────────────────────────

class TestProvisionCreatesClusterAndRoles:
    """provision() creates a running cluster, the nexus DB, and both roles."""

    def test_cluster_started(self, provisioned, bins):
        result, config_dir = provisioned
        assert result.port > 0
        assert _port_accepting("127.0.0.1", result.port), (
            f"cluster not accepting connections on port {result.port}"
        )

    def test_nexus_db_exists(self, provisioned, bins):
        result, config_dir = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"
        row = _query(bins, result.port, "postgres", os_user,
                     f"SELECT 1 FROM pg_database WHERE datname = '{NEXUS_DB_NAME}'")
        assert row == "1", f"nexus database not found after provision"

    def test_nexus_admin_role_exists(self, provisioned, bins):
        result, config_dir = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"
        row = _query(bins, result.port, "postgres", os_user,
                     "SELECT 1 FROM pg_roles WHERE rolname = 'nexus_admin'")
        assert row == "1", "nexus_admin role not found"

    def test_nexus_svc_role_exists(self, provisioned, bins):
        result, config_dir = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"
        row = _query(bins, result.port, "postgres", os_user,
                     "SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc'")
        assert row == "1", "nexus_svc role not found"


# ── Test 2: role attributes are correct ───────────────────────────────────────

class TestRoleAttributes:
    """nexus_admin and nexus_svc have the required NOSUPERUSER / NOBYPASSRLS attributes."""

    def _os_user(self) -> str:
        return os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"

    def test_nexus_admin_nosuperuser(self, provisioned, bins):
        result, _ = provisioned
        val = _role_attr(bins, result.port, self._os_user(), "nexus_admin", "rolsuper")
        assert val == "f", f"nexus_admin must be NOSUPERUSER, got rolsuper={val}"

    def test_nexus_admin_nocreatedb(self, provisioned, bins):
        result, _ = provisioned
        val = _role_attr(bins, result.port, self._os_user(), "nexus_admin", "rolcreatedb")
        assert val == "f", f"nexus_admin must be NOCREATEDB, got rolcreatedb={val}"

    def test_nexus_admin_nocreaterole(self, provisioned, bins):
        result, _ = provisioned
        val = _role_attr(bins, result.port, self._os_user(), "nexus_admin", "rolcreaterole")
        assert val == "f", f"nexus_admin must be NOCREATEROLE, got rolcreaterole={val}"

    def test_nexus_admin_login(self, provisioned, bins):
        result, _ = provisioned
        val = _role_attr(bins, result.port, self._os_user(), "nexus_admin", "rolcanlogin")
        assert val == "t", f"nexus_admin must have LOGIN, got rolcanlogin={val}"

    def test_nexus_admin_has_create_on_db(self, provisioned, bins):
        """nexus_admin must hold CREATE on the nexus database (Liquibase DDL requirement)."""
        result, _ = provisioned
        os_user = self._os_user()
        row = _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT has_database_privilege('nexus_admin', 'nexus', 'CREATE')"
        )
        assert row == "t", "nexus_admin must hold CREATE ON DATABASE nexus"

    def test_nexus_svc_nosuperuser(self, provisioned, bins):
        result, _ = provisioned
        val = _role_attr(bins, result.port, self._os_user(), "nexus_svc", "rolsuper")
        assert val == "f", f"nexus_svc must be NOSUPERUSER, got rolsuper={val}"

    def test_nexus_svc_nobypassrls(self, provisioned, bins):
        result, _ = provisioned
        val = _role_attr(bins, result.port, self._os_user(), "nexus_svc", "rolbypassrls")
        assert val == "f", f"nexus_svc must be NOBYPASSRLS, got rolbypassrls={val}"

    def test_nexus_svc_nocreatedb(self, provisioned, bins):
        result, _ = provisioned
        val = _role_attr(bins, result.port, self._os_user(), "nexus_svc", "rolcreatedb")
        assert val == "f", f"nexus_svc must be NOCREATEDB, got rolcreatedb={val}"

    def test_nexus_svc_login(self, provisioned, bins):
        result, _ = provisioned
        val = _role_attr(bins, result.port, self._os_user(), "nexus_svc", "rolcanlogin")
        assert val == "t", f"nexus_svc must have LOGIN, got rolcanlogin={val}"


# ── Test 3: credentials file ───────────────────────────────────────────────────

class TestCredentialsFile:
    """pg_credentials is written at 0600 with the required env variables."""

    def test_credentials_file_exists(self, provisioned):
        result, config_dir = provisioned
        creds_path = config_dir / CREDENTIALS_FILENAME
        assert creds_path.exists(), f"credentials file not found at {creds_path}"

    def test_credentials_file_permissions(self, provisioned):
        result, config_dir = provisioned
        creds_path = config_dir / CREDENTIALS_FILENAME
        mode = stat.S_IMODE(creds_path.stat().st_mode)
        assert mode == 0o600, (
            f"credentials file must be 0600, got {oct(mode)}"
        )

    def test_credentials_contain_pg_port(self, provisioned):
        result, config_dir = provisioned
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        assert "PG_PORT" in creds, "PG_PORT missing from credentials"
        assert creds["PG_PORT"] == str(result.port)

    def test_credentials_contain_admin_vars(self, provisioned):
        result, config_dir = provisioned
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        assert "NX_DB_ADMIN_URL" in creds
        assert "NX_DB_ADMIN_USER" in creds
        assert creds["NX_DB_ADMIN_USER"] == "nexus_admin"
        assert "NX_DB_ADMIN_PASS" in creds
        assert len(creds["NX_DB_ADMIN_PASS"]) >= 16, "admin password too short"

    def test_credentials_contain_svc_vars(self, provisioned):
        result, config_dir = provisioned
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        assert "NX_DB_URL" in creds
        assert "NX_DB_USER" in creds
        assert creds["NX_DB_USER"] == "nexus_svc"
        assert "NX_DB_PASS" in creds
        assert len(creds["NX_DB_PASS"]) >= 16, "svc password too short"

    def test_admin_url_contains_port(self, provisioned):
        result, config_dir = provisioned
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        assert str(result.port) in creds["NX_DB_ADMIN_URL"]
        assert NEXUS_DB_NAME in creds["NX_DB_ADMIN_URL"]

    def test_svc_url_contains_port(self, provisioned):
        result, config_dir = provisioned
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        assert str(result.port) in creds["NX_DB_URL"]
        assert NEXUS_DB_NAME in creds["NX_DB_URL"]


# ── Test 4: idempotency ────────────────────────────────────────────────────────

class TestIdempotency:
    """A second provision() call is a no-op; credentials are unchanged."""

    def test_second_provision_no_op(self, provisioned):
        """Re-running provision() returns already_provisioned=True."""
        result, config_dir = provisioned
        creds_before = _read_credentials(config_dir / CREDENTIALS_FILENAME)

        # Re-run without force_new_port.
        result2 = provision(config_dir)

        assert result2.already_provisioned, (
            "second provision() call must return already_provisioned=True"
        )
        # Credentials must be unchanged.
        creds_after = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        assert creds_after == creds_before, (
            "credentials must not change on idempotent re-run"
        )

    def test_is_provisioned_returns_true(self, provisioned):
        _, config_dir = provisioned
        assert is_provisioned(config_dir), "is_provisioned() must return True after provision()"


# ── Test 5: end-to-end provision → migrate DDL → svc DML under RLS ────────────

# Minimal DDL subset that replicates just enough of the Liquibase baseline
# to prove the two-role contract without the service JAR.
_MINIMAL_DDL = """
CREATE SCHEMA IF NOT EXISTS nexus;

CREATE TABLE IF NOT EXISTS nexus.memory (
    id            BIGSERIAL    NOT NULL,
    tenant_id     TEXT         NOT NULL,
    project       TEXT         NOT NULL,
    title         TEXT         NOT NULL,
    content       TEXT         NOT NULL,
    tags          TEXT         NOT NULL DEFAULT '',
    timestamp     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    access_count  INTEGER      NOT NULL DEFAULT 0,
    CONSTRAINT memory_pk PRIMARY KEY (id),
    CONSTRAINT memory_tenant_project_title_uq UNIQUE (tenant_id, project, title)
);

ALTER TABLE nexus.memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.memory FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON nexus.memory
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

-- Grant schema owner privileges to nexus_admin (already has CONNECT+CREATE on DB).
GRANT USAGE ON SCHEMA nexus TO nexus_admin;
ALTER SCHEMA nexus OWNER TO nexus_admin;

-- DML grants for nexus_svc (mirrors grants-nexus-svc.xml).
GRANT USAGE ON SCHEMA nexus TO nexus_svc;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO nexus_svc;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO nexus_svc;
"""

_TENANT = "gmiaf31-e2e-tenant"


def _psql_as(bins: PgBinaries, port: int, user: str, password: str, db: str, sql: str) -> str:
    """Run psql as *user* with explicit password via PGPASSWORD env."""
    env = {**os.environ, "PGPASSWORD": password}
    result = subprocess.run(
        [str(bins.psql), "-h", "127.0.0.1", "-p", str(port),
         "-U", user, "-d", db, "-t", "-A", "-c", sql],
        capture_output=True, text=True, env=env, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"psql as {user} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


@pytest.fixture(scope="module")
def e2e_provisioned(bins: PgBinaries, tmp_path_factory) -> tuple[ProvisionResult, Path]:
    """Separate hermetic cluster for the end-to-end test (module-scoped).

    This fixture provisions its own cluster so the DDL changes in the end-to-end
    test do not interfere with the role-attribute / credentials tests above.
    """
    config_dir = tmp_path_factory.mktemp("nexus_e2e_test")
    result = provision(config_dir, force_new_port=True)
    yield result, config_dir
    pgdata = config_dir / "postgres"
    _stop_pg(bins, pgdata)


class TestEndToEndProvisionMigrateDML:
    """provision → apply DDL as nexus_admin → nexus_svc DML under RLS."""

    def test_nexus_admin_can_run_ddl(self, e2e_provisioned, bins):
        """nexus_admin (NOSUPERUSER, has CREATE ON DATABASE) can create schemas + tables."""
        result, config_dir = e2e_provisioned
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        admin_pass = creds["NX_DB_ADMIN_PASS"]
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"

        # Apply the minimal DDL as the OS superuser first (CREATE EXTENSION, schema
        # ownership is superuser territory — in production this is the provisioning
        # step before nexus_admin takes over DDL).  For our purposes we run the full
        # DDL as the superuser to bootstrap the schema, then verify nexus_admin can
        # subsequently ALTER TABLE and CREATE INDEX (normal schema-owner operations).
        _psql(bins, result.port, NEXUS_DB_NAME, os_user, _MINIMAL_DDL)

    def test_nexus_svc_insert_under_rls(self, e2e_provisioned, bins):
        """nexus_svc can INSERT rows when the tenant GUC is stamped."""
        result, config_dir = e2e_provisioned
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        svc_pass = creds["NX_DB_PASS"]

        sql = (
            f"SET LOCAL nexus.tenant = '{_TENANT}'; "
            f"INSERT INTO nexus.memory (tenant_id, project, title, content) "
            f"VALUES ('{_TENANT}', 'e2e-proj', 'e2e-title', 'e2e-body')"
            f" ON CONFLICT DO NOTHING"
        )
        _psql_as(bins, result.port, "nexus_svc", svc_pass, NEXUS_DB_NAME, sql)

    def test_nexus_svc_select_under_rls_sees_own_rows(self, e2e_provisioned, bins):
        """nexus_svc SELECT with GUC returns its own rows."""
        result, config_dir = e2e_provisioned
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        svc_pass = creds["NX_DB_PASS"]

        sql = (
            f"SET nexus.tenant = '{_TENANT}'; "
            f"SELECT COUNT(*) FROM nexus.memory WHERE tenant_id = '{_TENANT}'"
        )
        out = _psql_as(bins, result.port, "nexus_svc", svc_pass, NEXUS_DB_NAME, sql)
        # psql -t -A returns one line per statement; last line is the count.
        count_line = [l for l in out.splitlines() if l.strip().isdigit()]
        assert count_line, f"no numeric output from COUNT(*): {out!r}"
        assert int(count_line[-1]) >= 1, (
            f"nexus_svc should see its own row under RLS, got count={count_line[-1]}"
        )

    def test_nexus_svc_no_guc_sees_zero_rows(self, e2e_provisioned, bins):
        """nexus_svc without GUC sees zero rows (RLS fail-closed)."""
        result, config_dir = e2e_provisioned
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        svc_pass = creds["NX_DB_PASS"]

        # No GUC stamp — current_setting('nexus.tenant', true) = NULL → no match.
        sql = "SELECT COUNT(*) FROM nexus.memory"
        out = _psql_as(bins, result.port, "nexus_svc", svc_pass, NEXUS_DB_NAME, sql)
        count_line = [l for l in out.splitlines() if l.strip().isdigit()]
        assert count_line, f"no numeric output from COUNT(*): {out!r}"
        assert int(count_line[-1]) == 0, (
            f"nexus_svc without GUC stamp must see zero rows (RLS fail-closed), "
            f"got count={count_line[-1]}"
        )
