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

# THE GATE RESOLVES THROUGH THE SELF-PROVISIONING SEAM, NEVER AMBIENT DISCOVERY.
# Locked policy: nexus ALWAYS uses the PostgreSQL it BUILDS — never Homebrew,
# never a host install. This module TESTS discover_pg_binaries (imported above,
# exercised in the body), but whether it RUNS must not depend on the box
# happening to carry a PostgreSQL; it depends on our own bundle being
# obtainable. Gating on discovery made the two identical, and the old skip
# reason ("install postgresql@16") told the reader to do the one thing this
# project never does. Enforced by tests/db/test_pg_gate_is_self_provisioning.py.
from tests.db._service_fixture import pg_bin_dir  # noqa: E402 — gate needs it here

_PG_BIN = pg_bin_dir()
_INITDB = _PG_BIN / "initdb"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _INITDB.exists(),
        reason=f"skipped: nexus-pg bundle self-provisioning failed (no {_INITDB}). "
               "NOT a missing host PostgreSQL — these tests never use one.",
    ),
]


@pytest.fixture(scope="module", autouse=True)
def _pin_discovery_to_the_built_bundle():
    """Point the product's OWN discovery at the PG we BUILD, for this module.

    Fixing the skip gate alone was not enough. The gate decided whether to run;
    the FIXTURES still resolved binaries through ``discover_pg_binaries()``, and
    ``provision()`` discovers internally with no bin-dir parameter to pass. On a
    box with no host PostgreSQL the module went straight from "silently skipped"
    to "40 errors, install postgresql@17" — the module-wide skip had been hiding
    the deeper coupling all along.

    ``NEXUS_PG_BIN`` is the sanctioned seam for this: it is carve-out #1 of the
    always-install policy and the highest-priority leg of discovery, so pointing
    it at the self-provisioned bundle makes the product's real discovery path
    resolve to the PostgreSQL we build, rather than to whatever the box happens
    to carry. That is what the nightly gate already does.

    An explicit operator-set ``NEXUS_PG_BIN`` wins untouched, matching product
    policy (a misconfigured explicit override is a user error and must fail
    loud, never be silently replaced). Module-scoped and undone afterwards so
    nothing leaks into modules that exercise discovery without an override.
    """
    if os.environ.get("NEXUS_PG_BIN", "").strip():
        yield
        return
    mp = pytest.MonkeyPatch()
    mp.setenv("NEXUS_PG_BIN", str(_PG_BIN))
    try:
        yield
    finally:
        mp.undo()


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


# ── Test 1b: pgvector extension created at provision time (nexus-jdpn9 item 3) ─

class TestVectorExtensionProvisioned:
    """provision() creates the pgvector 'vector' extension in the nexus DB.

    Regression for nexus-jdpn9 item 3: nexus_admin is NOSUPERUSER, so the Java
    service's Liquibase vectors-001 changeset cannot create the extension. It
    must be created at provision time (the only superuser context).
    """

    def test_vector_extension_exists_in_nexus_db(self, provisioned, bins):
        result, _ = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"
        row = _query(bins, result.port, NEXUS_DB_NAME, os_user,
                     "SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        assert row == "1", "pgvector 'vector' extension not created in nexus DB"

    def test_result_flags_extension_created_on_first_run(self, provisioned, bins):
        result, _ = provisioned
        assert result.vector_extension_created is True, (
            "fresh provision must report vector_extension_created=True"
        )

    def test_nexus_admin_can_use_vector_type(self, provisioned, bins):
        """The runtime role (nexus_admin, NOSUPERUSER) can use the vector type.

        This is the actual capability the original failure blocked: the Java
        service's Liquibase vectors-001 changeset creates a table with a
        ``vector`` column AS nexus_admin. Proving the extension row exists is
        not enough; prove the type is usable by the non-superuser role.
        """
        result, config_dir = provisioned
        admin_pass = _read_credentials(
            config_dir / CREDENTIALS_FILENAME
        )["NX_DB_ADMIN_PASS"]
        # CREATE TABLE with a vector column, then drop it — as nexus_admin.
        _psql_as(
            bins, result.port, "nexus_admin", admin_pass, NEXUS_DB_NAME,
            "CREATE TABLE _vector_probe (v vector(3)); DROP TABLE _vector_probe",
        )


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

    def test_nexus_svc_noinherit(self, provisioned, bins):
        """nexus-v80f2: local must match the cloud posture (measured
        rolinherit=FALSE, conexus relay [22485]) — granted-role privileges
        (pg_monitor via grants-004) must never be ambient on a plain
        nexus_svc session; callers SET ROLE explicitly (src/nexus/db/
        svc_monitor.py). A regression here would silently make every
        role nexus_svc is ever granted membership in ambiently usable
        again, diverging local from cloud exactly as nexus-bb5c8 found."""
        result, _ = provisioned
        val = _role_attr(bins, result.port, self._os_user(), "nexus_svc", "rolinherit")
        assert val == "f", f"nexus_svc must be NOINHERIT, got rolinherit={val}"


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

    def test_credentials_contain_pg_data(self, provisioned):
        """PG_DATA must be in credentials so the daemon (.30) can run pg_ctl."""
        result, config_dir = provisioned
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        assert "PG_DATA" in creds, "PG_DATA missing from credentials (required by bead .30 daemon)"
        expected_pgdata = str(config_dir / "postgres")
        assert creds["PG_DATA"] == expected_pgdata, (
            f"PG_DATA must be {expected_pgdata!r}, got {creds['PG_DATA']!r}"
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
        # The extension already exists, so the backfill is a no-op.
        assert result2.vector_extension_created is False, (
            "clean idempotent re-run must report vector_extension_created=False"
        )

    def test_is_provisioned_returns_true(self, provisioned):
        _, config_dir = provisioned
        assert is_provisioned(config_dir), "is_provisioned() must return True after provision()"

    def test_idempotent_rerun_backfills_dropped_vector_extension(self, provisioned, bins):
        """A re-run repairs a cluster whose 'vector' extension is missing.

        This is the original nexus-jdpn9 failure mode: the cluster is already
        up (fast idempotency path) but the extension was never created. Drop it,
        re-run provision(), and assert it is recreated.

        NOTE: mutates the module-scoped ``provisioned`` cluster. It restores the
        extension before returning, so sibling tests that assume ``vector``
        exists are safe regardless of collection order.
        """
        result, config_dir = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"
        # Drop the extension to simulate a pre-fix cluster.
        _psql(bins, result.port, NEXUS_DB_NAME, os_user, "DROP EXTENSION vector")
        assert _query(bins, result.port, NEXUS_DB_NAME, os_user,
                      "SELECT 1 FROM pg_extension WHERE extname = 'vector'") == "", (
            "extension drop precondition failed"
        )

        result2 = provision(config_dir)

        assert result2.already_provisioned, "repair re-run must hit the fast path"
        assert result2.vector_extension_created is True, (
            "fast-path re-run must backfill the missing vector extension"
        )
        assert _query(bins, result.port, NEXUS_DB_NAME, os_user,
                      "SELECT 1 FROM pg_extension WHERE extname = 'vector'") == "1", (
            "vector extension not recreated by idempotent repair re-run"
        )

    def test_idempotent_rerun_backfills_missing_diag_role(self, provisioned, bins):
        """RDR-182 P2.1 (review-foundations High): the fast idempotency path
        must backfill nexus_diag on an ALREADY-RUNNING cluster — the steady
        state for every existing install, and exactly what guided-upgrade
        re-runs hit. Simulate a pre-P2.1 cluster (drop the role, strip the
        credentials keys), re-run provision(), assert role + credentials are
        restored via the fast path.

        NOTE: mutates the module-scoped ``provisioned`` cluster; restores the
        role via the very backfill under test, so sibling order is safe.
        """
        result, config_dir = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"
        creds_path = result.credentials_path

        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "DROP ROLE IF EXISTS nexus_diag")
        assert _query(bins, result.port, NEXUS_DB_NAME, os_user,
                      "SELECT 1 FROM pg_roles WHERE rolname = 'nexus_diag'") == "", (
            "role drop precondition failed"
        )
        # Strip the diag keys to simulate a pre-P2.1 credentials file.
        stripped = "\n".join(
            line for line in creds_path.read_text().splitlines()
            if not line.startswith("NX_DB_DIAG_")
        ) + "\n"
        creds_path.write_text(stripped)
        creds_path.chmod(0o600)

        result2 = provision(config_dir)

        assert result2.already_provisioned, "repair re-run must hit the fast path"
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = 'nexus_diag'",
        ) == "t", "nexus_diag not recreated (with BYPASSRLS) by the fast-path backfill"
        from nexus.db.pg_provision import _read_credentials
        creds = _read_credentials(creds_path)
        assert creds.get("NX_DB_DIAG_USER") == "nexus_diag", (
            "diag credentials not backfilled into pg_credentials on the fast path"
        )
        assert creds.get("NX_DB_DIAG_PASS"), "diag password missing after backfill"

    def test_idempotent_rerun_backfills_missing_pg_monitor_admin_option(self, provisioned, bins):
        """nexus-hzhgl round 2 (code-review-expert Critical, 2026-08-13): the
        fast idempotency path must backfill nexus_admin's ADMIN OPTION on
        pg_monitor on an ALREADY-RUNNING cluster -- the steady state for every
        existing install, and exactly what an engine-service upgrade re-run
        hits. Without this, the runAlways grants-004-monitor-wal-visibility
        Liquibase changeset (which grants pg_monitor onward to nexus_svc)
        fails PERMANENTLY at every subsequent engine-service boot, since
        Main.java calls System.exit(1) on any MigrationException -- a total
        local-engine outage on upgrade, not a degraded WAL-visibility
        feature. Simulate a pre-round-2 cluster (revoke the grant), re-run
        provision(), assert nexus_admin's ADMIN OPTION is restored via the
        fast path.

        NOTE: mutates the module-scoped ``provisioned`` cluster; restores the
        grant via the very backfill under test, so sibling order is safe.
        """
        result, config_dir = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"

        # Simulate a pre-round-2 install: nexus_admin holds no pg_monitor
        # membership at all (a from-scratch _create_roles run only ever grants
        # it WITH ADMIN OPTION, so a plain REVOKE cleanly reproduces the
        # "never granted" precondition -- there is no bare-membership state to
        # distinguish it from here).
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "REVOKE pg_monitor FROM nexus_admin")
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT pg_has_role('nexus_admin', 'pg_monitor', 'member')",
        ) == "f", "revoke precondition failed"

        result2 = provision(config_dir)

        assert result2.already_provisioned, "repair re-run must hit the fast path"
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT pg_has_role('nexus_admin', 'pg_monitor', 'member')",
        ) == "t", (
            "nexus_admin's pg_monitor membership not restored by the fast-path backfill -- "
            "this is exactly what makes grants-004-monitor-wal-visibility crash-loop the "
            "engine service on every already-provisioned local install"
        )
        # Must be WITH ADMIN OPTION specifically -- plain membership is not
        # enough, since nexus_admin must be able to grant pg_monitor ONWARD to
        # nexus_svc (that is the entire point of the backfill).
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT m.admin_option FROM pg_auth_members m "
            "JOIN pg_roles r ON r.oid = m.roleid AND r.rolname = 'pg_monitor' "
            "JOIN pg_roles g ON g.oid = m.member AND g.rolname = 'nexus_admin'",
        ) == "t", "nexus_admin's pg_monitor grant not restored WITH ADMIN OPTION"

    def test_idempotent_rerun_converges_svc_role_to_noinherit(self, provisioned, bins):
        """nexus-v80f2 (2026-08-15): the fast idempotency path must converge
        an ALREADY-RUNNING cluster's nexus_svc to NOINHERIT -- the steady
        state for every existing local install provisioned before this fix,
        which is exactly the population that measurably diverged from the
        cloud posture (rolinherit=FALSE, conexus relay [22485]). Simulate a
        pre-fix cluster (ALTER ROLE ... INHERIT), re-run provision(), assert
        NOINHERIT is restored via the fast path.

        NOTE: mutates the module-scoped ``provisioned`` cluster; restores the
        attribute via the very backfill under test, so sibling order is safe.
        """
        result, config_dir = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"

        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "ALTER ROLE nexus_svc INHERIT")
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT rolinherit FROM pg_roles WHERE rolname = 'nexus_svc'",
        ) == "t", "INHERIT precondition failed"

        result2 = provision(config_dir)

        assert result2.already_provisioned, "repair re-run must hit the fast path"
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT rolinherit FROM pg_roles WHERE rolname = 'nexus_svc'",
        ) == "f", (
            "nexus_svc not converged back to NOINHERIT by the fast-path backfill -- "
            "this is exactly the local/cloud posture divergence nexus-v80f2 found"
        )


# ── heal_diag_view_grants_and_ownership (nexus-cfgo9, GH #1402 2nd symptom) ────


class TestHealDiagViewGrantsAndOwnership:
    """The narrowly-scoped GRANT/ALTER OWNER repair for
    ``nexus.diag_chash_conformance`` — no view-creation DDL (that stays
    provisioning's job), just the two independent drift classes GH #1402
    exposed: a missing SELECT grant, and ownership fragmentation."""

    # Drives PostgreSQL directly via psql; never launches the JVM service
    # jar, so exempt from tests/db/conftest.py's jar-freshness gate.
    pytestmark = pytest.mark.no_service_jar

    @pytest.fixture()
    def diag_view(self, provisioned, bins):
        """A minimal stand-in view, created correctly (superuser-owned,
        granted) — the healthy starting state each test mutates and repairs.
        Not RDR-182's real chash-count DDL (this fixture's cluster has no
        chash tables); a trivial view is sufficient to exercise ownership/
        grant repair, which does not care about the view's SELECT list.
        """
        result, config_dir = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "CREATE SCHEMA IF NOT EXISTS nexus")
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "CREATE OR REPLACE VIEW nexus.diag_chash_conformance AS "
              "SELECT 'stub'::text AS table_name, 0::bigint AS non_conformant")
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              f'ALTER VIEW nexus.diag_chash_conformance OWNER TO "{os_user}"')
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "GRANT SELECT ON nexus.diag_chash_conformance TO nexus_diag")
        yield result, config_dir, os_user
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "DROP VIEW IF EXISTS nexus.diag_chash_conformance")

    def test_absent_view_is_a_noop(self, provisioned, bins):
        """No view at all (this fixture's fresh cluster has no chash tables,
        so provisioning never created one) -- nothing to heal, and no DDL is
        issued to create it (that would violate the bead's explicit scope)."""
        from nexus.db.pg_provision import heal_diag_view_grants_and_ownership

        result, config_dir = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT 1 FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'nexus' "
            "AND c.relname = 'diag_chash_conformance'",
        ) == "", "precondition: view must be absent in the fresh cluster"

        actions = heal_diag_view_grants_and_ownership(bins, result.port, os_user)

        assert actions == []

    def test_already_healthy_is_silent(self, diag_view, bins):
        from nexus.db.pg_provision import heal_diag_view_grants_and_ownership

        result, config_dir, os_user = diag_view

        actions = heal_diag_view_grants_and_ownership(bins, result.port, os_user)

        assert actions == []

    def test_missing_grant_is_healed(self, diag_view, bins):
        from nexus.db.pg_provision import heal_diag_view_grants_and_ownership

        result, config_dir, os_user = diag_view
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "REVOKE SELECT ON nexus.diag_chash_conformance FROM nexus_diag")
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT has_table_privilege('nexus_diag', "
            "'nexus.diag_chash_conformance', 'SELECT')",
        ) == "f", "precondition: grant must be absent"

        actions = heal_diag_view_grants_and_ownership(bins, result.port, os_user)

        assert len(actions) == 1
        assert "missing-grant class" in actions[0]
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT has_table_privilege('nexus_diag', "
            "'nexus.diag_chash_conformance', 'SELECT')",
        ) == "t", "grant was not repaired"

    def test_non_exempt_ownership_is_healed(self, diag_view, bins):
        """Simulates the documented bring-your-own-Postgres workaround
        (docs/configuration.md §3) run without genuine superuser access: the
        view ends up owned by nexus_admin (NOSUPERUSER NOBYPASSRLS) --
        RLS-exempt is false, the nexus-vounk false-clean class."""
        from nexus.db.pg_provision import heal_diag_view_grants_and_ownership

        result, config_dir, os_user = diag_view
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "ALTER VIEW nexus.diag_chash_conformance OWNER TO nexus_admin")
        owner_sql = (
            "SELECT r.rolname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_roles r ON r.oid = c.relowner "
            "WHERE n.nspname = 'nexus' AND c.relname = 'diag_chash_conformance'"
        )
        assert _query(bins, result.port, NEXUS_DB_NAME, os_user, owner_sql) == "nexus_admin", (
            "precondition: view must be owned by nexus_admin"
        )

        actions = heal_diag_view_grants_and_ownership(bins, result.port, os_user)

        assert any("ownership fragmentation" in a for a in actions)
        assert _query(bins, result.port, NEXUS_DB_NAME, os_user, owner_sql) == os_user, (
            "ownership was not reassigned back to the superuser bootstrap role"
        )

    def test_no_diag_role_is_a_noop(self, diag_view, bins):
        """Pre-P2.1 install: nexus_diag does not exist at all. Role creation
        is _backfill_diag_role's job, not this function's narrower scope --
        it must degrade to no-op, never error."""
        from nexus.db.pg_provision import heal_diag_view_grants_and_ownership

        result, config_dir, os_user = diag_view
        # The fixture granted nexus_diag SELECT on the view; drop that
        # dependency first or the role drop fails ("privileges for view...").
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "REVOKE SELECT ON nexus.diag_chash_conformance FROM nexus_diag")
        _psql(bins, result.port, NEXUS_DB_NAME, os_user, "DROP ROLE IF EXISTS nexus_diag")
        try:
            assert _query(
                bins, result.port, NEXUS_DB_NAME, os_user,
                "SELECT 1 FROM pg_roles WHERE rolname = 'nexus_diag'",
            ) == "", "precondition: nexus_diag role must be absent"

            actions = heal_diag_view_grants_and_ownership(bins, result.port, os_user)

            assert actions == []
        finally:
            # Restore nexus_diag for sibling tests / the module-scoped cluster.
            diag_pass = _read_credentials(result.credentials_path).get(
                "NX_DB_DIAG_PASS", "restored-in-test"
            )
            _psql(
                bins, result.port, NEXUS_DB_NAME, os_user,
                f"CREATE ROLE nexus_diag NOSUPERUSER NOCREATEDB NOCREATEROLE "
                f"BYPASSRLS LOGIN PASSWORD '{diag_pass}'",
            )

    def test_security_invoker_view_ownership_is_never_touched(self, diag_view, bins):
        """RDR-194 P3c companion fix (nexus-v5lk3, 2026-08-17), REGRESSION
        TEST: the generation guard. A ``WITH (security_invoker = true)``
        view owned by ``nexus_admin`` (NOSUPERUSER, no BYPASSRLS — the
        taxonomy-011-8 generation) must NEVER have its ownership reassigned
        back to the superuser, even though ``nexus_admin`` structurally
        fails the OLD ``rolsuper OR rolbypassrls`` exemption test this
        function used unconditionally before this fix. Left unguarded, this
        function would keep reassigning a correctly-functioning view back
        to the superuser on every finish pass FOREVER, permanently
        defeating changeset 8's own "closes the reprovisioning gap
        permanently" claim."""
        from nexus.db.pg_provision import heal_diag_view_grants_and_ownership

        result, config_dir, os_user = diag_view
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "CREATE OR REPLACE VIEW nexus.diag_chash_conformance "
              "WITH (security_invoker = true) AS "
              "SELECT 'stub'::text AS table_name, 0::bigint AS non_conformant")
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "ALTER VIEW nexus.diag_chash_conformance OWNER TO nexus_admin")
        owner_sql = (
            "SELECT r.rolname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_roles r ON r.oid = c.relowner "
            "WHERE n.nspname = 'nexus' AND c.relname = 'diag_chash_conformance'"
        )
        assert _query(bins, result.port, NEXUS_DB_NAME, os_user, owner_sql) == "nexus_admin", (
            "precondition: view must be nexus_admin-owned (the new generation)"
        )

        actions = heal_diag_view_grants_and_ownership(bins, result.port, os_user)

        assert not any("ownership fragmentation" in a for a in actions), (
            f"the generation guard must skip the ownership repair entirely "
            f"for a security_invoker view: {actions!r}"
        )
        assert _query(bins, result.port, NEXUS_DB_NAME, os_user, owner_sql) == "nexus_admin", (
            "ownership must NOT have been reassigned away from nexus_admin"
        )
        # The grant repair is UNCHANGED and independent — still runs for
        # both generations, since a missing nexus_diag grant is real either
        # way. The fixture already granted it, so no action is expected
        # here, but this positively confirms the OTHER branch was not
        # accidentally short-circuited by the generation guard too.
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT has_table_privilege('nexus_diag', "
            "'nexus.diag_chash_conformance', 'SELECT')",
        ) == "t"


class TestReassignDiagViewOwnerBeforeRestart:
    """RDR-194 P3c companion fix (nexus-v5lk3, 2026-08-17): the proactive
    ownership transfer wired into ``converge_engine``'s own restart-
    triggering call sites, which closes the crash-loop bead nexus-7ec4i's
    own critical-fix round introduced for essentially every existing local
    install — see ``reassign_diag_view_owner_before_restart``'s own
    docstring for the full derivation."""

    # Drives PostgreSQL directly via psql; never launches the JVM service
    # jar, so exempt from tests/db/conftest.py's jar-freshness gate.
    pytestmark = pytest.mark.no_service_jar

    @pytest.fixture()
    def superuser_owned_view(self, provisioned, bins):
        """The STEADY STATE this fix closes: a view created (as every
        pre-taxonomy-011-8 local install's provisioning always has)
        entirely by the superuser, never touched by anything nexus_admin-
        owned."""
        result, config_dir = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "CREATE SCHEMA IF NOT EXISTS nexus")
        # In production nexus_admin OWNS this schema (it creates it via
        # Liquibase's very first changeset), so USAGE is implicit. This
        # fixture creates the schema directly as the superuser instead —
        # grant USAGE explicitly so nexus_admin's own DROP VIEW attempt
        # below fails on OWNERSHIP (the real crash-loop precondition this
        # test proves), not on schema-level permission denied first.
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "GRANT USAGE ON SCHEMA nexus TO nexus_admin")
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "CREATE OR REPLACE VIEW nexus.diag_chash_conformance AS "
              "SELECT 'stub'::text AS table_name, 0::bigint AS non_conformant")
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              f'ALTER VIEW nexus.diag_chash_conformance OWNER TO "{os_user}"')
        yield result, config_dir, os_user
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "DROP VIEW IF EXISTS nexus.diag_chash_conformance")

    def test_absent_view_is_a_noop(self, provisioned, bins):
        from nexus.db.pg_provision import reassign_diag_view_owner_before_restart

        result, config_dir = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT 1 FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'nexus' "
            "AND c.relname = 'diag_chash_conformance'",
        ) == "", "precondition: view must be absent"

        actions = reassign_diag_view_owner_before_restart(bins, result.port, os_user)

        assert actions == []

    def test_already_admin_owned_is_a_noop(self, superuser_owned_view, bins):
        """The steady state AFTER a successful taxonomy-011 walk: reassigning
        to the SAME owner would be a harmless Postgres no-op, but is
        skipped here for a clean, honest action-line log instead of
        reporting a healed-nothing 'fix'."""
        from nexus.db.pg_provision import reassign_diag_view_owner_before_restart

        result, config_dir, os_user = superuser_owned_view
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "ALTER VIEW nexus.diag_chash_conformance OWNER TO nexus_admin")

        actions = reassign_diag_view_owner_before_restart(bins, result.port, os_user)

        assert actions == []

    def test_superuser_owned_view_is_reassigned_and_the_crash_loop_is_closed(
        self, superuser_owned_view, bins,
    ):
        """THE END-TO-END CRASH-LOOP PROOF (nexus-v5lk3). Before this fix,
        ``nexus_admin`` attempting ``DROP VIEW nexus.diag_chash_
        conformance`` against a superuser-owned view — exactly what
        ``taxonomy-011-1``'s own guard does on every real migration walk —
        raises ``insufficient_privilege``. Reproduce that failure as the
        PRECONDITION (proving this fixture really is the crash-loop
        shape, not merely asserted to be), run the reassignment, then
        prove ``nexus_admin``'s own drop attempt (the guard's REAL
        statement, run by the REAL migration role) now succeeds where it
        failed a moment ago."""
        from nexus.db.pg_provision import reassign_diag_view_owner_before_restart

        result, config_dir, os_user = superuser_owned_view
        creds = _read_credentials(result.credentials_path)
        admin_pass = creds["NX_DB_ADMIN_PASS"]

        # PRECONDITION: nexus_admin cannot drop the superuser-owned view —
        # this IS the crash-loop taxonomy-011-1's own guard would hit,
        # reproduced directly against a real cluster.
        with pytest.raises(RuntimeError, match="insufficient_privilege|must be owner"):
            _psql_as(bins, result.port, "nexus_admin", admin_pass, NEXUS_DB_NAME,
                     "DROP VIEW nexus.diag_chash_conformance")

        actions = reassign_diag_view_owner_before_restart(bins, result.port, os_user)

        assert len(actions) == 1
        assert "nexus_admin" in actions[0]
        owner_sql = (
            "SELECT r.rolname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_roles r ON r.oid = c.relowner "
            "WHERE n.nspname = 'nexus' AND c.relname = 'diag_chash_conformance'"
        )
        assert _query(bins, result.port, NEXUS_DB_NAME, os_user, owner_sql) == "nexus_admin"

        # THE PROOF: nexus_admin's own DROP VIEW (taxonomy-011-1's exact
        # guarded statement) now succeeds where it raised above.
        _psql_as(bins, result.port, "nexus_admin", admin_pass, NEXUS_DB_NAME,
                 "DROP VIEW IF EXISTS nexus.diag_chash_conformance")
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT 1 FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'nexus' "
            "AND c.relname = 'diag_chash_conformance'",
        ) == ""


class TestProvisionFastPathReassignsDiagView:
    """RDR-194 P3c critical-fix round 4 (nexus-rkn3i, 2026-08-17): both
    round-3 reviewers (code-review-expert + substantive-critic,
    independently) found ``reassign_diag_view_owner_before_restart`` wired
    ONLY into ``converge_engine``'s two restart-triggering call sites —
    unreachable from ``nx daemon service install-binary``'s own documented
    ``stop && start`` remedy, and from OS-level launchd/systemd autostart,
    both of which go ``service_start_cmd`` -> ``_start_locked`` ->
    ``_ensure_pg_running`` -> ``_backfill_provision_grants`` ->
    ``pg_provision.provision()``'s fast path, which never called it. This
    class proves the round-4 fix: the call is now wired into ``provision()``
    itself, so it is reachable from EVERY service start, and a previously
    wedged install self-heals on its very next ``nx daemon service start``
    with no operator intervention beyond that ordinary restart.

    ``TestReassignDiagViewOwnerBeforeRestart`` above proves the target
    function's own behaviour (calling it directly); this class proves it is
    actually WIRED into the fast path real service starts traverse — the
    distinction round 3's gap turned on.
    """

    # Drives PostgreSQL directly via psql / provision()'s fast path; never
    # launches the JVM service jar.
    pytestmark = pytest.mark.no_service_jar

    @pytest.fixture()
    def superuser_owned_view(self, provisioned, bins):
        """Same steady-state precondition as
        ``TestReassignDiagViewOwnerBeforeRestart.superuser_owned_view``: a
        view created entirely by the superuser, exactly what every
        pre-taxonomy-011-8 local install's provisioning has produced."""
        result, config_dir = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "CREATE SCHEMA IF NOT EXISTS nexus")
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "GRANT USAGE ON SCHEMA nexus TO nexus_admin")
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "CREATE OR REPLACE VIEW nexus.diag_chash_conformance AS "
              "SELECT 'stub'::text AS table_name, 0::bigint AS non_conformant")
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              f'ALTER VIEW nexus.diag_chash_conformance OWNER TO "{os_user}"')
        yield result, config_dir, os_user
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "DROP VIEW IF EXISTS nexus.diag_chash_conformance")

    def test_daemon_start_path_reassigns_via_provision_fast_path(
        self, superuser_owned_view, bins,
    ):
        """REACHABILITY (round 4, test 1): call ``provision(config_dir)`` —
        the exact function ``_backfill_provision_grants`` calls at Step 1 of
        EVERY ``_start_locked``, i.e. what ``nx daemon service start``, the
        printed remedy after ``nx daemon service install-binary``, and OS
        autostart units all funnel through — WITHOUT calling
        ``reassign_diag_view_owner_before_restart`` directly. Assert the
        view is reassigned anyway: proof the wiring, not just the function,
        works.
        """
        result, config_dir, os_user = superuser_owned_view
        owner_sql = (
            "SELECT r.rolname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_roles r ON r.oid = c.relowner "
            "WHERE n.nspname = 'nexus' AND c.relname = 'diag_chash_conformance'"
        )
        assert _query(bins, result.port, NEXUS_DB_NAME, os_user, owner_sql) == os_user, (
            "precondition: view must still be superuser-owned before the "
            "fast path runs"
        )

        result2 = provision(config_dir)

        assert result2.already_provisioned, (
            "this must hit provision()'s fast idempotency path (the "
            "steady state for every already-provisioned install) — a fresh "
            "provision would not exercise the same code path a real "
            "service restart does"
        )
        assert _query(bins, result.port, NEXUS_DB_NAME, os_user, owner_sql) == "nexus_admin", (
            "provision()'s fast path did not reassign the diag view's "
            "owner — the round-4 wiring into provision() is missing or "
            "broken, reopening the install-binary/autostart reachability "
            "gap (nexus-rkn3i)"
        )

    def test_wedged_install_self_heals_on_next_service_start(
        self, superuser_owned_view, bins,
    ):
        """RECOVERY (round 4, test 2): reproduce the actual wedged state —
        nexus_admin's own DROP VIEW (taxonomy-011-1's exact guarded
        statement) genuinely fails against the foreign-owned view — then
        run ``provision(config_dir)`` (simulating the wedged box's very
        next ``nx daemon service start``, BEFORE Step 2 spawns the service
        binary), then prove nexus_admin's identical DROP VIEW attempt now
        succeeds where it failed a moment ago. This is the automated-
        recovery half of round 4: a previously-wedged install needs no
        operator intervention beyond an ordinary restart.
        """
        result, config_dir, os_user = superuser_owned_view
        creds = _read_credentials(result.credentials_path)
        admin_pass = creds["NX_DB_ADMIN_PASS"]

        # PRECONDITION: reproduce the crash loop for real — nexus_admin
        # cannot drop the superuser-owned view.
        with pytest.raises(RuntimeError, match="insufficient_privilege|must be owner"):
            _psql_as(bins, result.port, "nexus_admin", admin_pass, NEXUS_DB_NAME,
                     "DROP VIEW nexus.diag_chash_conformance")

        # THE RECOVERY STEP: exactly what a wedged install's next
        # `nx daemon service start` runs at Step 1, before the service
        # binary (and therefore Liquibase) is ever spawned.
        result2 = provision(config_dir)
        assert result2.already_provisioned, "recovery re-run must hit the fast path"

        # THE PROOF: the identical DROP VIEW that raised above now
        # succeeds — taxonomy-011-1's guard would proceed cleanly on the
        # next Liquibase walk (Step 2, which this test never needs to run).
        _psql_as(bins, result.port, "nexus_admin", admin_pass, NEXUS_DB_NAME,
                 "DROP VIEW IF EXISTS nexus.diag_chash_conformance")
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT 1 FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'nexus' "
            "AND c.relname = 'diag_chash_conformance'",
        ) == ""


class TestProvisionDiagConformanceViewDefersToExisting:
    """RDR-194 P3c companion fix (nexus-v5lk3, 2026-08-17): create-only-if-
    absent, against a REAL cluster where the DDL gate's own precondition
    (every chash-bearing table exists) genuinely holds — proving deferral,
    not merely a lucky no-op from the gate condition never firing (which is
    all the pure-unit tests in tests/db/test_pg_provision_diag_view_non_
    vacuity.py can show, since they monkeypatch the DDL away entirely)."""

    pytestmark = pytest.mark.no_service_jar

    @pytest.fixture()
    def chash_tables(self, provisioned, bins):
        """Stub chash-bearing tables. The DDL GATE (the ``IF (SELECT
        count(*)...)`` existence check) only checks EXISTENCE by name, but
        the VIEW DEFINITION itself — ``CREATE ... AS <the real UNION ALL
        query>`` — type-checks every expression at CREATE time, so the
        stub column TYPES must match what the generator expects:
        ``ChashBearingTable.bytea`` decides bytea vs text per column, same
        as the real schema (e.g. the debt legs compare ``nexus.chunks``'s
        bytea ``chash`` against either a bytea column directly or
        ``decode(text_column, 'hex')``, never bytea against a bare text
        column)."""
        from nexus.db.chash_tables import CHASH_BEARING_TABLES

        result, config_dir = provisioned
        os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"
        _psql(bins, result.port, NEXUS_DB_NAME, os_user, "CREATE SCHEMA IF NOT EXISTS nexus")
        names = [t.table.split(".", 1)[1] for t in CHASH_BEARING_TABLES]
        for name, t in zip(names, CHASH_BEARING_TABLES):
            col_type = "bytea" if t.bytea else "text"
            _psql(bins, result.port, NEXUS_DB_NAME, os_user,
                  f"CREATE TABLE IF NOT EXISTS nexus.{name} ({t.column} {col_type})")
        yield result, config_dir, os_user
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "DROP VIEW IF EXISTS nexus.diag_chash_conformance")
        for name in names:
            _psql(bins, result.port, NEXUS_DB_NAME, os_user, f"DROP TABLE IF EXISTS nexus.{name}")

    def test_creates_fresh_view_when_absent_and_tables_ready(self, chash_tables, bins):
        """The POSITIVE case, unchanged behavior: absent view + all chash
        tables present -> the view gets created."""
        from nexus.db.pg_provision import _provision_diag_conformance_view

        result, config_dir, os_user = chash_tables
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT 1 FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'nexus' "
            "AND c.relname = 'diag_chash_conformance'",
        ) == "", "precondition: view must be absent"

        _provision_diag_conformance_view(bins, result.port, os_user)

        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT 1 FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'nexus' "
            "AND c.relname = 'diag_chash_conformance'",
        ) == "1"

    def test_backfill_idempotence_without_ownership_or_definition_clobber(
        self, chash_tables, bins,
    ):
        """THE IDEMPOTENCE PROOF (nexus-v5lk3): with the view ALREADY
        present (nexus_admin-owned, a definition DELIBERATELY different
        from the real generator's), a repeat call — exactly what
        ``_backfill_diag_role`` triggers on EVERY ``nx daemon service
        start`` — must touch NEITHER the ownership NOR the definition.
        Before this fix, the unconditional ``CREATE OR REPLACE VIEW`` here
        would have replaced the stub definition with the real generator's
        (a second, independent writer of the same object as
        taxonomy-011-8) even though ownership itself was separately proven
        safe from THIS specific statement (see the function's own
        docstring)."""
        from nexus.db.pg_provision import _provision_diag_conformance_view

        result, config_dir, os_user = chash_tables
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "CREATE OR REPLACE VIEW nexus.diag_chash_conformance AS "
              "SELECT 'sentinel-stub-definition'::text AS table_name, "
              "0::bigint AS non_conformant")
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "ALTER VIEW nexus.diag_chash_conformance OWNER TO nexus_admin")
        owner_sql = (
            "SELECT r.rolname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_roles r ON r.oid = c.relowner "
            "WHERE n.nspname = 'nexus' AND c.relname = 'diag_chash_conformance'"
        )
        assert _query(bins, result.port, NEXUS_DB_NAME, os_user, owner_sql) == "nexus_admin", (
            "precondition: view must be nexus_admin-owned with the stub definition"
        )

        _provision_diag_conformance_view(bins, result.port, os_user)

        assert _query(bins, result.port, NEXUS_DB_NAME, os_user, owner_sql) == "nexus_admin", (
            "ownership must survive a repeat backfill call unchanged"
        )
        definition = _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT pg_get_viewdef('nexus.diag_chash_conformance'::regclass)",
        )
        assert "sentinel-stub-definition" in definition, (
            f"the DDL must never re-run when the view already exists — the "
            f"stub definition must survive unchanged, proving deferral "
            f"rather than a lucky no-op: got {definition!r}"
        )


# ── Test 5: end-to-end provision → migrate DDL → svc DML under RLS ────────────

# DDL run AS nexus_admin to prove the two-role contract WITHOUT the service JAR.
#
# Structure mirrors what Liquibase does at service startup:
#   1. Public-schema Liquibase tracking tables (DATABASECHANGELOG + lock).
#      This is the CRITICAL part: on PG 15/16 the PUBLIC role lost CREATE on
#      the public schema. nexus_admin must have GRANT CREATE ON SCHEMA public
#      or this CREATE TABLE statement fails with "permission denied for schema
#      public" — exactly as SchemaMigratorIntegrationTest.java:120 documents.
#   2. Application schema 'nexus' with a representative table + RLS policy.
#   3. Grants for nexus_svc (mirrors grants-nexus-svc.xml, runAlways=true).
#
# ALL of this DDL is run as nexus_admin (NOSUPERUSER) — never as the OS
# superuser — so the test proves the grants are correct, not just the schema.
_LIQUIBASE_TRACKING_DDL = """
CREATE TABLE IF NOT EXISTS public.databasechangelog (
    id              VARCHAR(255) NOT NULL,
    author          VARCHAR(255) NOT NULL,
    filename        VARCHAR(255) NOT NULL,
    dateexecuted    TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    orderexecuted   INTEGER NOT NULL,
    exectype        VARCHAR(10) NOT NULL,
    md5sum          VARCHAR(35),
    description     VARCHAR(255),
    comments        VARCHAR(255),
    tag             VARCHAR(255),
    liquibase       VARCHAR(20),
    contexts        VARCHAR(255),
    labels          VARCHAR(255),
    deployment_id   VARCHAR(10)
);
CREATE TABLE IF NOT EXISTS public.databasechangeloglock (
    id          INTEGER NOT NULL,
    locked      BOOLEAN NOT NULL,
    lockgranted TIMESTAMP WITHOUT TIME ZONE,
    lockedby    VARCHAR(255),
    CONSTRAINT pk_databasechangeloglock PRIMARY KEY (id)
);
"""

_APP_SCHEMA_DDL = """
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
    """provision → apply DDL AS nexus_admin → nexus_svc DML under RLS.

    The DDL is intentionally run AS nexus_admin (NOSUPERUSER), not as the OS
    superuser, to prove the grant set is correct.  Running as the superuser
    would mask any missing privilege and make the tests vacuous.
    """

    def test_nexus_admin_can_create_liquibase_tracking_tables_in_public(
        self, e2e_provisioned, bins
    ):
        """nexus_admin can CREATE TABLE in the public schema.

        This exercises the GRANT CREATE ON SCHEMA public grant that is
        required for Liquibase (on PG 15/16 the PUBLIC role lost this
        privilege).  The test would fail with "permission denied for schema
        public" if the grant were absent.
        """
        result, config_dir = e2e_provisioned
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        admin_pass = creds["NX_DB_ADMIN_PASS"]
        # Run as nexus_admin — if GRANT CREATE ON SCHEMA public is missing
        # this raises RuntimeError("psql as nexus_admin failed: ... permission denied")
        _psql_as(bins, result.port, "nexus_admin", admin_pass, NEXUS_DB_NAME,
                 _LIQUIBASE_TRACKING_DDL)

    def test_nexus_admin_can_create_app_schema_and_tables(
        self, e2e_provisioned, bins
    ):
        """nexus_admin can CREATE SCHEMA nexus and the application tables within it."""
        result, config_dir = e2e_provisioned
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        admin_pass = creds["NX_DB_ADMIN_PASS"]
        _psql_as(bins, result.port, "nexus_admin", admin_pass, NEXUS_DB_NAME,
                 _APP_SCHEMA_DDL)

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


# ── Test 6: public-schema grant is load-bearing (red-without-grant proof) ──────


@pytest.fixture(scope="module")
def grant_proof_cluster(bins: PgBinaries, tmp_path_factory) -> tuple[ProvisionResult, Path]:
    """Hermetic cluster used ONLY for the grant-revocation proof.

    Separate fixture so the revoke/regrant cycle does not disturb the other
    module-scoped clusters.
    """
    config_dir = tmp_path_factory.mktemp("nexus_grant_proof")
    result = provision(config_dir, force_new_port=True)
    yield result, config_dir
    pgdata = config_dir / "postgres"
    _stop_pg(bins, pgdata)


class TestPublicSchemaGrantIsLoadBearing:
    """Prove that GRANT CREATE ON SCHEMA public TO nexus_admin is required.

    This test class:
      1. Revokes the public-schema grant from nexus_admin.
      2. Asserts that creating a table in public AS nexus_admin FAILS.
      3. Re-grants it.
      4. Asserts that creating the table now SUCCEEDS.

    This proves the grant is not decorative — removing it breaks the
    Liquibase migration path exactly as described in the critique.
    """

    def _os_user(self) -> str:
        return os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"

    def test_without_public_grant_nexus_admin_cannot_create_table(
        self, grant_proof_cluster, bins
    ):
        """After revoking CREATE ON SCHEMA public, nexus_admin cannot CREATE TABLE there."""
        result, config_dir = grant_proof_cluster
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        admin_pass = creds["NX_DB_ADMIN_PASS"]
        os_user = self._os_user()

        # Revoke the grant as the OS superuser.
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "REVOKE CREATE ON SCHEMA public FROM nexus_admin")

        # Attempt to CREATE TABLE in public as nexus_admin — must fail.
        env = {**os.environ, "PGPASSWORD": admin_pass}
        proc = subprocess.run(
            [str(bins.psql), "-h", "127.0.0.1", "-p", str(result.port),
             "-U", "nexus_admin", "-d", NEXUS_DB_NAME, "-t", "-A",
             "-c", "CREATE TABLE IF NOT EXISTS public.nx_grant_proof (id INT)"],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode != 0, (
            "Expected CREATE TABLE in public to fail for nexus_admin without "
            "the public-schema grant, but it succeeded — grant may not have been revoked"
        )
        assert "permission denied" in proc.stderr.lower(), (
            f"Expected 'permission denied' in stderr; got: {proc.stderr!r}"
        )

    def test_with_public_grant_nexus_admin_can_create_table(
        self, grant_proof_cluster, bins
    ):
        """After re-granting CREATE ON SCHEMA public, nexus_admin can CREATE TABLE there."""
        result, config_dir = grant_proof_cluster
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        admin_pass = creds["NX_DB_ADMIN_PASS"]
        os_user = self._os_user()

        # Re-grant as OS superuser.
        _psql(bins, result.port, NEXUS_DB_NAME, os_user,
              "GRANT CREATE ON SCHEMA public TO nexus_admin")

        # Now the CREATE TABLE must succeed.
        _psql_as(bins, result.port, "nexus_admin", admin_pass, NEXUS_DB_NAME,
                 "CREATE TABLE IF NOT EXISTS public.nx_grant_proof (id INT)")

        # Verify the table exists.
        row = _query(bins, result.port, NEXUS_DB_NAME, os_user,
                     "SELECT 1 FROM information_schema.tables "
                     "WHERE table_schema='public' AND table_name='nx_grant_proof'")
        assert row == "1", "nx_grant_proof table must exist after nexus_admin CREATE TABLE"
