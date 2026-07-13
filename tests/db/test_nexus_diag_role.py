# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 P2.1 (nexus-ykzbj.8): the ``nexus_diag`` SELECT-only diagnostic role.

Read-only-BY-CONSTRUCTION for the product diagnostic path: SELECT + BYPASSRLS,
zero write privileges. BYPASSRLS is load-bearing, not a convenience — every
``nexus.*`` tenant table is ENABLE+FORCE RLS with the fail-closed
``tenant_isolation`` policy, so a plain SELECT-only role (or nexus_admin — see
nexus-vounk, demonstrated 0-vs-9 on a real store) silently counts ZERO rows
without the tenant GUC: integrity diagnostics must see what Liquibase VALIDATE
sees (cross-tenant), or the chash-poison gate reports false-clean on exactly
the store it exists to block. BYPASSRLS grants visibility, never writes.

Real PG, no mocks (integration-over-mocks): the test SELF-PROVISIONS a scratch
cluster via the product's own binary discovery + ``_create_roles``, builds a
FORCE-RLS tenant table, and asserts the full privilege matrix. Skips cleanly
only when no PG binaries are discoverable (`pg_bin_dir` policy: a MISCONFIGURED
NEXUS_PG_BIN fails loud, never mass-skips).
"""
from __future__ import annotations

import getpass
import socket
import subprocess

import pytest

from tests.db._service_fixture import pg_bin_dir

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def diag_cluster(tmp_path_factory):
    """Scratch cluster + provisioned roles + a FORCE-RLS tenant table."""
    from nexus.db.pg_provision import (
        PgBinaries,
        _create_roles,
        _init_cluster,
        _start_cluster,
        _configure_cluster,
        _create_db,
    )

    bins = PgBinaries.from_dir(pg_bin_dir())
    pgdata = tmp_path_factory.mktemp("diag-pg") / "data"
    port = _free_port()
    os_user = getpass.getuser()

    _init_cluster(bins, pgdata, os_user)
    _configure_cluster(pgdata, port)
    _start_cluster(bins, pgdata, port)
    _create_db(bins, port, os_user)

    created = _create_roles(
        bins, port, os_user, "admin-pw", "svc-pw", "diag-pw"
    )
    assert created.diag_created is True  # non-vacuity: the role really was made

    def su(sql: str) -> str:
        """Run sql as the cluster superuser (os_user)."""
        proc = subprocess.run(
            [str(bins.psql), "-h", "127.0.0.1", "-p", str(port), "-U", os_user,
             "-d", "nexus", "-v", "ON_ERROR_STOP=1", "-tAc", sql],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    # A FORCE-RLS tenant table exactly like the shipped changelog's shape,
    # owned by nexus_admin (as Liquibase-created tables are), with one row
    # under tenant 'default' inserted WITH the GUC (WITH CHECK requires it).
    su("GRANT CREATE ON DATABASE nexus TO nexus_admin")  # idempotent re-grant
    su("CREATE SCHEMA IF NOT EXISTS nexus AUTHORIZATION nexus_admin")
    su(
        "SET ROLE nexus_admin; "
        "CREATE TABLE IF NOT EXISTS nexus.diag_probe ("
        "  id BIGSERIAL PRIMARY KEY, chash TEXT NOT NULL, tenant_id TEXT NOT NULL); "
        "ALTER TABLE nexus.diag_probe ENABLE ROW LEVEL SECURITY; "
        "ALTER TABLE nexus.diag_probe FORCE ROW LEVEL SECURITY; "
        "DROP POLICY IF EXISTS tenant_isolation ON nexus.diag_probe; "
        "CREATE POLICY tenant_isolation ON nexus.diag_probe "
        "  USING (tenant_id = current_setting('nexus.tenant', true)) "
        "  WITH CHECK (tenant_id = current_setting('nexus.tenant', true)); "
        "SELECT set_config('nexus.tenant', 'default', false); "
        "INSERT INTO nexus.diag_probe (chash, tenant_id) "
        "  VALUES ('short-chash', 'default'); "
        "GRANT USAGE ON SCHEMA nexus TO nexus_diag; "
        "GRANT SELECT ON ALL TABLES IN SCHEMA nexus TO nexus_diag;"
    )

    def diag(sql: str) -> subprocess.CompletedProcess:
        """Run sql as nexus_diag (no GUC, no special session state)."""
        import os as _os
        env = dict(_os.environ, PGPASSWORD="diag-pw")
        return subprocess.run(
            [str(bins.psql), "-h", "127.0.0.1", "-p", str(port),
             "-U", "nexus_diag", "-d", "nexus", "-v", "ON_ERROR_STOP=1",
             "-tAc", sql],
            capture_output=True, text=True, timeout=30, env=env,
        )

    yield {"su": su, "diag": diag}

    subprocess.run(
        [str(bins.pg_ctl), "-D", str(pgdata), "stop", "-m", "immediate"],
        capture_output=True, text=True, timeout=30,
    )


class TestRoleAttributes:
    def test_role_shape_is_select_only_bypassrls(self, diag_cluster):
        row = diag_cluster["su"](
            "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, "
            "rolbypassrls FROM pg_roles WHERE rolname = 'nexus_diag'"
        )
        assert row == "t|f|f|f|t"  # LOGIN, no super/createrole/createdb, BYPASSRLS


class TestVisibility:
    def test_sees_force_rls_rows_without_tenant_guc(self, diag_cluster):
        """The nexus-vounk lesson, locked: the diagnostic connection counts
        rows on a FORCE-RLS table with NO tenant GUC set — what VALIDATE sees."""
        proc = diag_cluster["diag"]("SELECT count(*) FROM nexus.diag_probe")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "1"  # exact: sees THE row, not zero

    def test_can_read_system_catalogs_and_changelog_side(self, diag_cluster):
        proc = diag_cluster["diag"](
            "SELECT count(*) FROM pg_catalog.pg_class WHERE relname = 'diag_probe'"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "1"


class TestMutationsRefuse:
    @pytest.mark.parametrize("sql", [
        "INSERT INTO nexus.diag_probe (chash, tenant_id) VALUES ('x', 'default')",
        "UPDATE nexus.diag_probe SET chash = 'y'",
        "DELETE FROM nexus.diag_probe",
        "TRUNCATE nexus.diag_probe",
        "DROP TABLE nexus.diag_probe",
        "ALTER TABLE nexus.diag_probe ADD COLUMN evil TEXT",
        "CREATE TABLE nexus.evil (id int)",
    ])
    def test_write_attempts_refuse_at_the_db(self, diag_cluster, sql):
        proc = diag_cluster["diag"](sql)
        assert proc.returncode != 0  # refused BY POSTGRES, not by convention
        err = proc.stderr.lower()
        assert "permission denied" in err or "must be owner" in err, proc.stderr

    def test_read_only_transaction_defense_in_depth(self, diag_cluster):
        """SET TRANSACTION READ ONLY refuses writes even before privilege
        checks — the defense-in-depth layer the diagnostic connection sets."""
        proc = diag_cluster["diag"](
            "BEGIN; SET TRANSACTION READ ONLY; "
            "INSERT INTO nexus.diag_probe (chash, tenant_id) "
            "VALUES ('x', 'default'); COMMIT;"
        )
        assert proc.returncode != 0
        assert "read-only" in proc.stderr.lower() or "permission denied" in proc.stderr.lower()


class TestDiagConnectionHelperLive:
    """run_diagnostic_sql end-to-end against the live cluster: the product
    choke point (lint -> READ ONLY txn -> psql as nexus_diag) really counts
    FORCE-RLS rows and really refuses content/mutation before DB contact."""

    def test_helper_counts_rls_rows_end_to_end(self, diag_cluster):
        from nexus.db.diag_connection import DiagCredentials, run_diagnostic_sql
        from tests.db._service_fixture import pg_bin_dir

        port = int(diag_cluster["su"]("SELECT inet_server_port()"))
        creds = DiagCredentials(port=port, user="nexus_diag", password="diag-pw")
        out = run_diagnostic_sql(
            ["SELECT count(*) FROM nexus.diag_probe"],
            creds, psql_bin=pg_bin_dir() / "psql",
        )
        assert out == ["1"]

    def test_helper_refuses_content_and_mutation_without_db_contact(self, diag_cluster):
        from nexus.db.diag_connection import DiagCredentials, run_diagnostic_sql
        from nexus.remediation.sql_lint import DiagnosticSqlViolation
        from tests.db._service_fixture import pg_bin_dir

        port = int(diag_cluster["su"]("SELECT inet_server_port()"))
        creds = DiagCredentials(port=port, user="nexus_diag", password="diag-pw")
        for bad in ("SELECT chash FROM nexus.diag_probe",
                    "DELETE FROM nexus.diag_probe"):
            with pytest.raises(DiagnosticSqlViolation):
                run_diagnostic_sql([bad], creds, psql_bin=pg_bin_dir() / "psql")
        # the row is still there — nothing executed
        assert diag_cluster["su"](
            "SELECT set_config('nexus.tenant','default',false); "
            "SELECT count(*) FROM nexus.diag_probe"
        ).splitlines()[-1] == "1"


class TestIdempotency:
    def test_reprovision_is_a_clean_noop_with_password_sync(self, diag_cluster):
        from nexus.db.pg_provision import PgBinaries, _create_roles

        # Second run: nothing newly created, no error, passwords re-synced.
        # (Uses the same live cluster; _create_roles is skip-if-exists.)
        bins = PgBinaries.from_dir(pg_bin_dir())
        port = int(diag_cluster["su"]("SELECT inet_server_port()"))
        created = _create_roles(
            bins, port, getpass.getuser(), "admin-pw", "svc-pw", "diag-pw"
        )
        assert created.diag_created is False
        assert diag_cluster["diag"]("SELECT 1").returncode == 0
