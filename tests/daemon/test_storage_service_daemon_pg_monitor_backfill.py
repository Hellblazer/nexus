# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression coverage: the engine-convergence / service-start preflight
must re-run ``pg_provision.provision()``'s idempotent fast path against the
bundled PG BEFORE the service binary is spawned — closing the gap
nexus-hzhgl round 3 left open.

THE BUG (package-upgrade MVV P1, 2026-08-14): round 2
(``_backfill_pg_monitor_admin_option``) wired the ADMIN OPTION backfill into
``provision()``'s own fast idempotency path — the steady state for every
already-provisioned install. But nothing on the engine-convergence /
service-restart path (``nx daemon restart-stale`` -> ``nx daemon service
stop`` && ``nx daemon service start``) ever called ``provision()`` again.
``StorageServiceSupervisor._ensure_pg_running`` only ever called
``pg_provision._start_cluster`` directly, and when PG was ALREADY running
(the steady state after ``nx daemon service stop``, which leaves PG up by
design) it short-circuited with no backfill at all. A box provisioned
before the round-2 fix shipped converges its on-disk engine binary cleanly,
then crash-loops PERMANENTLY at Liquibase's
``grants-004-monitor-wal-visibility`` changeset on every subsequent boot.

Uses a REAL, hermetic, tmp-provisioned Postgres cluster (mirrors
tests/db/test_pg_provision.py's fixture pattern) — this is the actual
"upgrade-shaped state" the bug describes, not a mock of it.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from nexus.db.pg_provision import (
    CREDENTIALS_FILENAME,
    NEXUS_DB_NAME,
    PgBinaries,
    ProvisionResult,
    _port_accepting,
    _psql,
    _read_credentials,
    discover_pg_binaries,
    provision,
)

# THE GATE RESOLVES THROUGH THE SELF-PROVISIONING SEAM, NEVER AMBIENT
# DISCOVERY — see tests/db/test_pg_provision.py's identical header comment.
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


def _os_user() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "postgres"


def _query(bins: PgBinaries, port: int, db: str, user: str, sql: str) -> str:
    result = subprocess.run(
        [str(bins.psql), "-h", "127.0.0.1", "-p", str(port),
         "-U", user, "-d", db, "-t", "-A", "-c", sql],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@pytest.fixture(scope="module", autouse=True)
def _pin_discovery_to_the_built_bundle():
    """Point discovery at the self-provisioned bundle (see
    tests/db/test_pg_provision.py's identical fixture for full rationale).
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


@pytest.fixture(scope="module")
def bins() -> PgBinaries:
    return discover_pg_binaries()


@pytest.fixture
def upgrade_shaped_cluster(bins: PgBinaries, tmp_path_factory):
    """A freshly-provisioned, RUNNING cluster with nexus_admin's pg_monitor
    ADMIN OPTION revoked — simulating a pre-nexus-hzhgl install whose
    on-disk engine has since been converged, but which has never had
    provision() re-run against it.

    Function-scoped (unlike test_pg_provision.py's module-scoped
    ``provisioned``): each test in this module mutates cluster grant state,
    and the two tests here have different starting creds (one strips
    PG_DATA), so sharing a cluster across tests would make ordering
    load-bearing. A fresh hermetic cluster per test keeps each test's
    precondition self-evident.
    """
    config_dir = tmp_path_factory.mktemp("nexus_upgrade_shaped")
    old_env = os.environ.get("NEXUS_CONFIG_DIR")
    os.environ["NEXUS_CONFIG_DIR"] = str(config_dir)
    try:
        result = provision(config_dir, force_new_port=True)
    finally:
        if old_env is None:
            os.environ.pop("NEXUS_CONFIG_DIR", None)
        else:
            os.environ["NEXUS_CONFIG_DIR"] = old_env

    os_user = _os_user()
    # A from-scratch _create_roles run only ever grants pg_monitor WITH
    # ADMIN OPTION, so a plain REVOKE cleanly reproduces the "never
    # granted" precondition of a pre-round-2 install (same technique as
    # tests/db/test_pg_provision.py's round-2 regression test).
    _psql(bins, result.port, NEXUS_DB_NAME, os_user,
          "REVOKE pg_monitor FROM nexus_admin")
    assert _query(
        bins, result.port, NEXUS_DB_NAME, os_user,
        "SELECT pg_has_role('nexus_admin', 'pg_monitor', 'member')",
    ) == "f", "revoke precondition failed"

    yield result, config_dir

    pgdata = config_dir / "postgres"
    try:
        subprocess.run(
            [str(bins.pg_ctl), "-D", str(pgdata), "-m", "immediate", "stop"],
            capture_output=True, check=False, timeout=10,
        )
    except Exception:  # noqa: BLE001 — teardown must not reraise
        pass


def _make_supervisor(result: ProvisionResult, config_dir: Path, *, creds: dict[str, str] | None = None):
    """Construct a StorageServiceSupervisor wired at the REAL cluster,
    without needing a real service binary — ``_ensure_pg_running`` and
    ``_backfill_provision_grants`` never touch ``self._binary_path``.
    """
    from nexus.daemon.storage_service_daemon import StorageServiceSupervisor  # noqa: PLC0415 — deferred import, test-local helper

    resolved_creds = creds if creds is not None else _read_credentials(
        config_dir / CREDENTIALS_FILENAME
    )
    return StorageServiceSupervisor(
        config_dir=config_dir,
        pg_port=result.port,
        service_port=0,
        creds=resolved_creds,
        binary_path=Path("/nonexistent/fake-nexus-service-binary"),
        launch_kind="native",
    )


class TestEnsurePgRunningBackfillsAdminOption:
    """The core fix: ``_ensure_pg_running()`` (called from every service
    start AND every PG-independent-recovery cycle) must restore drifted
    grants, even when PG was ALREADY running — the steady state
    ``nx daemon service stop`` leaves behind by design.
    """

    def test_ensure_pg_running_restores_admin_option_when_pg_already_up(
        self, upgrade_shaped_cluster, bins,
    ):
        """RED-THEN-GREEN (see task write-up): before this fix,
        ``_ensure_pg_running`` only checked ``_port_accepting`` and
        returned on the already-running branch — it never re-ran
        ``provision()``, so a REVOKEd ADMIN OPTION stayed revoked forever.
        After the fix, calling ``_ensure_pg_running()`` against the SAME
        already-running cluster restores it, BEFORE any engine boot would
        consume the grant.
        """
        result, config_dir = upgrade_shaped_cluster
        sup = _make_supervisor(result, config_dir)

        # Precondition: PG is already running — this is the exact
        # upgrade-shaped state (`nx daemon service stop` leaves PG up).
        assert _port_accepting("127.0.0.1", result.port)

        sup._ensure_pg_running()

        os_user = _os_user()
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT pg_has_role('nexus_admin', 'pg_monitor', 'member')",
        ) == "t", (
            "_ensure_pg_running() did not restore nexus_admin's pg_monitor "
            "membership — the engine-convergence/service-start path still "
            "skips the backfill; this is exactly the P1 upgrade bug."
        )
        # Must be WITH ADMIN OPTION specifically — nexus_admin must be able
        # to grant pg_monitor ONWARD to nexus_svc (the actual Liquibase
        # dependency).
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT m.admin_option FROM pg_auth_members m "
            "JOIN pg_roles r ON r.oid = m.roleid AND r.rolname = 'pg_monitor' "
            "JOIN pg_roles g ON g.oid = m.member AND g.rolname = 'nexus_admin'",
        ) == "t", "restored grant must be WITH ADMIN OPTION, not bare membership"


class TestBackfillSkippedWithoutPgData:
    """Requirement 2: a managed/BYO Postgres deployment (no ``PG_DATA`` in
    ``pg_credentials`` — the ``nx daemon service install-binary
    --no-pg-bundle`` cloud-habitat posture) must NEVER invoke the
    superuser backfill. ``pg_provision`` assumes OS-level superuser access
    to a LOCALLY-BUNDLED cluster; a remote/customer-managed Postgres has
    neither the bundle nor that access, and the docs/configuration.md
    manual-DBA-step prerequisite must remain the only path there.
    """

    def test_provision_not_invoked_when_pg_data_absent(
        self, upgrade_shaped_cluster, monkeypatch,
    ):
        result, config_dir = upgrade_shaped_cluster
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        # Simulate a managed/BYO install: no PG_DATA was ever written,
        # because --no-pg-bundle never provisions a bundled cluster.
        managed_creds = {k: v for k, v in creds.items() if k != "PG_DATA"}
        sup = _make_supervisor(result, config_dir, creds=managed_creds)

        called = {"provision": False}

        def _fail_if_called(*args, **kwargs):
            called["provision"] = True
            raise AssertionError(
                "provision() must never be called for a managed/BYO Postgres"
            )

        monkeypatch.setattr("nexus.db.pg_provision.provision", _fail_if_called)

        # PG is already running (same precondition as the fixture); the
        # short-circuit branch must still route through the PG_DATA guard.
        sup._ensure_pg_running()

        assert not called["provision"], (
            "the superuser backfill ran against a managed/BYO Postgres — "
            "pg_provision must never touch a remote/customer-managed cluster"
        )

    def test_admin_option_stays_revoked_for_managed_config(
        self, upgrade_shaped_cluster, bins,
    ):
        """Direct behavioural check mirroring the positive test above: with
        no PG_DATA, the grant is left exactly as the (simulated) DBA left
        it — neither restored nor otherwise touched.
        """
        result, config_dir = upgrade_shaped_cluster
        creds = _read_credentials(config_dir / CREDENTIALS_FILENAME)
        managed_creds = {k: v for k, v in creds.items() if k != "PG_DATA"}
        sup = _make_supervisor(result, config_dir, creds=managed_creds)

        sup._ensure_pg_running()

        os_user = _os_user()
        assert _query(
            bins, result.port, NEXUS_DB_NAME, os_user,
            "SELECT pg_has_role('nexus_admin', 'pg_monitor', 'member')",
        ) == "f", (
            "a managed/BYO config must never mutate grants on a Postgres "
            "this supervisor does not own"
        )
