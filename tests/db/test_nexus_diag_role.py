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
import re
import socket
import os
import subprocess
from pathlib import Path

import pytest

import nexus.health as h

from tests._child_process import integration_pg_conf, pg_data_tempdir, stop_and_remove_cluster
from tests.db._service_fixture import pg_bin_dir


# THE GATE RESOLVES THROUGH THE SELF-PROVISIONING SEAM, NEVER AMBIENT DISCOVERY.
# Locked policy: nexus ALWAYS uses the PostgreSQL it BUILDS — never Homebrew,
# never a host install (T2 always-install-pg-bundle-no-fallback). pg_bin_dir()
# downloads the sigstore-verified nexus-pg bundle for PINNED_SERVICE_TAG into
# substrate_cache_root()/<tag>/ (XDG_CACHE_HOME, else ~/.cache) and returns a
# nonexistent sentinel ONLY
# when self-provisioning ITSELF fails.
#
# This module previously gated on discover_pg_binaries(), which asks whether the
# BOX happens to carry a PostgreSQL — a property of the machine, not of the
# substrate these tests need. Every fixture below already resolved its binaries
# through pg_bin_dir(), so the gate and the body disagreed: all 15 tests skipped
# on any box with no host install, while the bundle sat available in the cache
# (observed 2026-07-27 on a box whose cache already held the pinned tag).
# Mechanically enforced for every module by tests/db/test_pg_gate_is_self_provisioning.py.
#
# max-skip guard (testval-182 Low): still a clean SKIP rather than an ERROR from
# a fixture invoking a nonexistent initdb.
from nexus._install.layout_core import exe_name  # noqa: E402

_PG_BIN = pg_bin_dir()
# initdb.exe on Windows: a bare "initdb" never exists there, so the gate
# skipped every test on the one host the Windows paths need (nexus-ja4pq).
_INITDB = _PG_BIN / exe_name("initdb")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _INITDB.exists(),
        reason=f"skipped: nexus-pg bundle self-provisioning failed (no {_INITDB}). "
               "NOT a missing host PostgreSQL — these tests never use one.",
    ),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def diag_cluster(request):
    """Scratch cluster + provisioned roles + a FORCE-RLS tenant table."""
    from nexus.db.pg_provision import (
        PgBinaries,
        _create_roles,
        _init_cluster,
        _start_cluster,
        _configure_cluster,
        _create_db,
        superuser_auth,
    )

    bins = PgBinaries.from_dir(pg_bin_dir())
    # The cluster dir itself, not a child of one: the substrate's crash sweep reads
    # postmaster.pid at its top level (nexus-942h9). pytest's tmp_path_factory would
    # put the cluster on disk; this lands on the run's tmpfs root on Linux (nexus-szyun).
    pgdata = Path(pg_data_tempdir("nexus_diag_pg_"))
    request.addfinalizer(lambda: stop_and_remove_cluster(bins.pg_ctl, pgdata))
    port = _free_port()
    os_user = getpass.getuser()

    su_pw = "su-pw"  # nexus-ja4pq: the cluster demands a superuser password
    _init_cluster(bins, pgdata, os_user, superuser_password=su_pw)
    _configure_cluster(pgdata, port)
    with open(pgdata / "postgresql.conf", "a", encoding="utf-8") as conf:
        conf.write(integration_pg_conf(str(pgdata)))
    _start_cluster(bins, pgdata, port)
    with superuser_auth(su_pw):
        _create_db(bins, port, os_user)
        created = _create_roles(
            bins, port, os_user, "admin-pw", "svc-pw", "diag-pw"
        )
    assert created.diag_created is True  # non-vacuity: the role really was made

    def su(sql: str) -> str:
        """Run sql as the cluster superuser (os_user)."""
        proc = subprocess.run(
            [str(bins.psql), "-w", "-h", "127.0.0.1", "-p", str(port), "-U", os_user,
             "-d", "nexus", "-v", "ON_ERROR_STOP=1", "-tAc", sql],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            env={**os.environ, "PGPASSWORD": su_pw},
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
            [str(bins.psql), "-w", "-h", "127.0.0.1", "-p", str(port),
             "-U", "nexus_diag", "-d", "nexus", "-v", "ON_ERROR_STOP=1",
             "-tAc", sql],
            capture_output=True, text=True, encoding="utf-8", timeout=30, env=env,
        )

    yield {"su": su, "diag": diag}


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
        from nexus.db.pg_provision import PgBinaries, _create_roles, superuser_auth

        # Second run: nothing newly created, no error, passwords re-synced.
        # (Uses the same live cluster; _create_roles is skip-if-exists.)
        bins = PgBinaries.from_dir(pg_bin_dir())
        port = int(diag_cluster["su"]("SELECT inet_server_port()"))
        with superuser_auth("su-pw"):  # nexus-ja4pq: the fixture's superuser password
            created = _create_roles(
                bins, port, getpass.getuser(), "admin-pw", "svc-pw", "diag-pw"
            )
        assert created.diag_created is False
        assert diag_cluster["diag"]("SELECT 1").returncode == 0


class TestViewEraChangesetLive:
    """RDR-182 Amendment A6 / nexus-46yy3 (live-reproduced P0): the view-era
    revoke changeset must survive the heterogeneous-ownership topology it is
    designed for — nexus_admin-owned tables PLUS a SUPERUSER-owned counts
    view in the same schema. The pre-fix bulk REVOKE ... ON ALL TABLES IN
    SCHEMA hard-errored on the foreign-owned view (permission denied),
    aborting the DO block and crash-looping every engine boot. This runs the
    REAL changeset SQL (parsed from the XML, never a copy) as nexus_admin.
    """

    def _changeset_2_sql(self) -> str:
        xml = (
            Path(__file__).resolve().parents[2]
            / "service/src/main/resources/db/changelog/grants-nexus-diag.xml"
        ).read_text()
        # The second changeset's <sql> body (grants-nexus-diag-2). bead
        # nexus-lhuhe (2026-08-17) added a THIRD changeset,
        # grants-nexus-diag-3 (era-independent staging grants + the
        # taxonomy-011-8 content-boundary re-grant fix) — appended AFTER
        # -1/-2 in file order, so blocks[1] is still changeset-2's body
        # unchanged; only the expected total block count moved 2 -> 3.
        # d9b917fb2 (2026-08-19) appended a FOURTH changeset,
        # grants-nexus-diag-4 (audit-table SELECT grants for the forensics
        # path) — again after all prior ones, so blocks[1] is still
        # changeset-2's body; count moved 3 -> 4.
        # 50a354fa3 (2026-09-11, nexus-f1pbh) appended a FIFTH changeset,
        # grants-nexus-diag-5 (SELECT on the three RDR-205 tuple-space
        # tables) — after all prior ones, blocks[1] unchanged; count 4 -> 5.
        blocks = re.findall(
            r"<sql\s[^>]*>(.*?)</sql>", xml, re.DOTALL,
        )
        assert len(blocks) == 5, "expected exactly the five grants-nexus-diag changesets"
        return blocks[1]

    def test_revoke_changeset_survives_superuser_owned_view(self, diag_cluster):
        su, diag = diag_cluster["su"], diag_cluster["diag"]

        # The A6 topology: a SUPERUSER-owned counts view (definer semantics
        # give cross-tenant counts) alongside the admin-owned FORCE-RLS table.
        # Production topology: schema t1 exists before the grants changesets
        # run (t1-001 baseline precedes them in the master changelog).
        su("CREATE SCHEMA IF NOT EXISTS t1 AUTHORIZATION nexus_admin")
        # ...as does the Liquibase journal (created by the migration
        # connection, i.e. owned by nexus_admin, before any changeset).
        su(
            "SET ROLE nexus_admin; "
            "CREATE TABLE IF NOT EXISTS public.databasechangelog (id TEXT); "
            "CREATE TABLE IF NOT EXISTS public.databasechangeloglock (id INT)"
        )
        su(
            "CREATE OR REPLACE VIEW nexus.diag_chash_conformance AS "
            "SELECT 'nexus.diag_probe' AS table_name, count(*) AS non_conformant "
            "FROM nexus.diag_probe WHERE length(chash) <> 32"
        )
        su("GRANT SELECT ON nexus.diag_chash_conformance TO nexus_diag")

        # THE P0 assertion: the real changeset body, run as nexus_admin
        # (SET ROLE drops superuser privileges), must NOT error.
        su("SET ROLE nexus_admin; " + self._changeset_2_sql())

        # The boundary it establishes: counts by construction...
        r = diag("SELECT non_conformant FROM nexus.diag_chash_conformance")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "1"  # the short-chash row, cross-tenant
        # ...and NO direct table read.
        r = diag("SELECT chash FROM nexus.diag_probe")
        assert r.returncode != 0
        assert "permission denied" in r.stderr


class TestProvisionBestEffortGrantsEra:
    """nexus-edchb: the provisioning path's best-effort grants (the SQL both
    ``_create_roles`` and ``_backfill_diag_role`` run) must not hand this
    BYPASSRLS role table-level SELECT once the counts view exists: the engine
    grants it only the columns the view reads (grants-nexus-diag-3). The bulk
    grant stays for the legacy era, when no view exists to read through."""

    def test_bulk_table_grant_only_before_the_view(self, diag_cluster):
        from nexus.db.pg_provision import _diag_best_effort_grants_sql

        su = diag_cluster["su"]

        def can_select() -> str:
            return su("SELECT has_table_privilege('nexus_diag', 'nexus.diag_probe', 'SELECT')")

        had_view = su(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'nexus' AND c.relname = 'diag_chash_conformance'"
        ) == "1"
        try:
            su("CREATE OR REPLACE VIEW nexus.diag_chash_conformance AS "
               "SELECT 'nexus.diag_probe' AS table_name, count(*) AS non_conformant "
               "FROM nexus.diag_probe WHERE length(chash) <> 32")
            su("REVOKE SELECT ON nexus.diag_probe FROM nexus_diag")
            assert can_select() == "f"  # non-vacuity: the grant really is gone first
            su(_diag_best_effort_grants_sql())
            assert can_select() == "f", "view era: provisioning must not re-grant whole tables"

            su("DROP VIEW nexus.diag_chash_conformance")
            su(_diag_best_effort_grants_sql())
            assert can_select() == "t", "legacy era: the bulk grant still applies"
        finally:
            su("GRANT SELECT ON nexus.diag_probe TO nexus_diag")
            if not had_view:
                su("DROP VIEW IF EXISTS nexus.diag_chash_conformance")
            else:
                su("CREATE OR REPLACE VIEW nexus.diag_chash_conformance AS "
                   "SELECT 'nexus.diag_probe' AS table_name, count(*) AS non_conformant "
                   "FROM nexus.diag_probe WHERE length(chash) <> 32")
                su("GRANT SELECT ON nexus.diag_chash_conformance TO nexus_diag")


def test_both_provisioning_paths_run_the_era_gated_grants():
    """nexus-edchb: ``_create_roles`` and ``_backfill_diag_role`` both run
    ``_diag_best_effort_grants_sql()``, not an inline bulk grant of their own."""
    import inspect

    from nexus.db import pg_provision

    for fn in (pg_provision._create_roles, pg_provision._backfill_diag_role):
        src = inspect.getsource(fn)
        assert "_diag_best_effort_grants_sql()" in src, fn.__name__
        assert "ALL TABLES IN SCHEMA" not in src, fn.__name__


def test_view_era_grants_land_on_the_view_input_columns(diag_cluster):
    """nexus-edchb: in the view era the provisioning grants give nexus_diag
    SELECT on the view's input column of a chash table and on none of its
    content columns. A scratch nexus.chunks stands in for the engine's."""
    from nexus.db.pg_provision import _diag_best_effort_grants_sql

    su = diag_cluster["su"]
    had_view = su(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'nexus' AND c.relname = 'diag_chash_conformance'"
    ) == "1"
    had_chunks = su("SELECT to_regclass('nexus.chunks') IS NOT NULL") == "t"
    if had_chunks:
        pytest.skip("this fixture's cluster already has a nexus.chunks of its own")
    try:
        su("SET ROLE nexus_admin; CREATE TABLE nexus.chunks (chash bytea, chunk_text text, metadata jsonb)")
        su("CREATE OR REPLACE VIEW nexus.diag_chash_conformance AS "
           "SELECT 'nexus.diag_probe' AS table_name, count(*) AS non_conformant "
           "FROM nexus.diag_probe WHERE length(chash) <> 32")
        su(_diag_best_effort_grants_sql())

        def col(c: str) -> str:
            return su(f"SELECT has_column_privilege('nexus_diag', 'nexus.chunks', '{c}', 'SELECT')")

        assert su("SELECT has_table_privilege('nexus_diag', 'nexus.chunks', 'SELECT')") == "f"
        assert col("chash") == "t"
        assert col("chunk_text") == "f"
        assert col("metadata") == "f"
        assert su("SELECT has_table_privilege('nexus_diag', 'nexus.diag_chash_conformance', 'SELECT')") == "t"
    finally:
        su("DROP TABLE IF EXISTS nexus.chunks")
        if not had_view:
            su("DROP VIEW IF EXISTS nexus.diag_chash_conformance")


def test_provisioning_column_grants_match_the_engine_changesets():
    """nexus-edchb: provisioning grants nexus_diag the same column lists on
    gc_audit and nexus.tuples as grants-nexus-diag-4 and -5, never a content
    column (gc_audit.details, a tuple's keys, dims or body)."""
    from nexus.db.pg_provision import DIAG_COLUMN_GRANTS, DIAG_TABLE_GRANTS

    xml = (
        Path(__file__).resolve().parents[2]
        / "service/src/main/resources/db/changelog/grants-nexus-diag.xml"
    ).read_text()
    assert DIAG_COLUMN_GRANTS, "no column grants: the pin checks nothing"
    for table, cols in DIAG_COLUMN_GRANTS:
        m = re.search(
            rf"GRANT SELECT \(([^)]*)\)\s+ON {re.escape(table)}\s+TO nexus_diag", xml
        )
        assert m, f"the changelog grants no column list on {table}"
        engine = {c.strip() for c in m.group(1).split(",")}
        assert set(cols) == engine, f"{table}: provisioning {sorted(cols)} vs engine {sorted(engine)}"
        assert not set(cols) & {"details", "keys", "dims", "body"}, table
    assert DIAG_TABLE_GRANTS, "no table grants: the pin checks nothing"
    for table in DIAG_TABLE_GRANTS:
        assert re.search(rf"GRANT SELECT ON {re.escape(table)}\s+TO nexus_diag", xml), (
            f"provisioning grants {table} whole but the engine does not"
        )
    engine_tuple_tables = set(re.findall(r"GRANT SELECT ON (nexus\.tuple_\w+)\s+TO nexus_diag", xml))
    assert engine_tuple_tables, "the changelog grants no tuple-space table whole: the reverse pin checks nothing"
    assert engine_tuple_tables <= set(DIAG_TABLE_GRANTS), (
        f"the engine grants {sorted(engine_tuple_tables - set(DIAG_TABLE_GRANTS))} whole; provisioning does not"
    )


def test_view_era_grants_land_on_the_gc_audit_and_tuples_columns(diag_cluster):
    """nexus-edchb: in the view era provisioning grants the DIAG_COLUMN_GRANTS
    columns and no content column. A table missing one of its columns (an older
    engine) skips only its own grant: the view grant and the other table's
    columns still land."""
    from nexus.db.pg_provision import _diag_best_effort_grants_sql

    su = diag_cluster["su"]
    if su("SELECT to_regclass('nexus.tuples') IS NOT NULL OR to_regclass('nexus.gc_audit') IS NOT NULL "
          "OR to_regclass('nexus.tuple_claim_log') IS NOT NULL "
          "OR to_regclass('nexus.tuple_tenants') IS NOT NULL") == "t":
        pytest.skip("this fixture's cluster already has an engine tuple-space or gc_audit table")
    had_view = su(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'nexus' AND c.relname = 'diag_chash_conformance'"
    ) == "1"
    try:
        su("SET ROLE nexus_admin; CREATE TABLE nexus.tuples (id bytea, tenant_id text, subspace text, "
           "claimant text, claim_id text, claim_state text, consumed_at timestamptz, "
           "lease_until timestamptz, keys jsonb, dims jsonb, body text)")
        su("SET ROLE nexus_admin; CREATE TABLE nexus.tuple_claim_log (log_id bigint, tenant_id text, "
           "subspace text, template text, tuple_id bytea, claim_id text, claimant text, transition text, "
           "at timestamptz, expires_at timestamptz)")
        su("SET ROLE nexus_admin; CREATE TABLE nexus.tuple_tenants (tenant_id text, first_seen timestamptz, "
           "last_seen timestamptz, last_swept_at timestamptz)")
        lock_sql = h._lock_claim_started_sql("lock/x", "claimant")
        before = diag_cluster["diag"](lock_sql)
        assert before.returncode != 0 and "permission denied" in before.stderr, (
            "non-vacuity: before provisioning the lock query must be refused"
        )
        # No chashes column: the older-engine shape whose grant must skip on its own.
        su("SET ROLE nexus_admin; CREATE TABLE nexus.gc_audit (id bigint, tenant_id text, operation text, "
           "collection text, actor text, dry_run boolean, chash_count int, created_at timestamptz, details jsonb)")
        su("CREATE OR REPLACE VIEW nexus.diag_chash_conformance AS "
           "SELECT 'nexus.diag_probe' AS table_name, count(*) AS non_conformant "
           "FROM nexus.diag_probe WHERE length(chash) <> 32")
        su(_diag_best_effort_grants_sql())

        def col(table: str, c: str) -> str:
            return su(f"SELECT has_column_privilege('nexus_diag', '{table}', '{c}', 'SELECT')")

        assert su("SELECT has_table_privilege('nexus_diag', 'nexus.tuples', 'SELECT')") == "f"
        for c in ("id", "tenant_id", "subspace", "claimant", "claim_id", "claim_state", "consumed_at", "lease_until"):
            assert col("nexus.tuples", c) == "t", c
        for c in ("keys", "dims", "body"):
            assert col("nexus.tuples", c) == "f", c
        assert col("nexus.gc_audit", "id") == "f", "a missing column skips the whole table's grant"
        assert col("nexus.gc_audit", "details") == "f"
        assert su("SELECT has_table_privilege('nexus_diag', 'nexus.diag_chash_conformance', 'SELECT')") == "t"
        after = diag_cluster["diag"](lock_sql)
        assert after.returncode == 0, f"nx doctor's lock query as nexus_diag: {after.stderr}"
        assert su("SELECT has_table_privilege('nexus_diag', 'nexus.tuple_tenants', 'SELECT')") == "t"

        su("SET ROLE nexus_admin; ALTER TABLE nexus.gc_audit ADD COLUMN chashes jsonb")
        su(_diag_best_effort_grants_sql())
        assert col("nexus.gc_audit", "id") == "t"
        assert col("nexus.gc_audit", "chashes") == "t"
        assert col("nexus.gc_audit", "details") == "f"
    finally:
        su("DROP TABLE IF EXISTS nexus.tuples")
        su("DROP TABLE IF EXISTS nexus.tuple_claim_log")
        su("DROP TABLE IF EXISTS nexus.tuple_tenants")
        su("DROP TABLE IF EXISTS nexus.gc_audit")
        if not had_view:
            su("DROP VIEW IF EXISTS nexus.diag_chash_conformance")
