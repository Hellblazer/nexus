# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bb5c8: grants-004-monitor-wal-visibility grants nexus_svc pg_monitor
MEMBERSHIP, but membership alone is NOT usable privilege under NOINHERIT --
and the CLOUD deployment's nexus_svc IS NOINHERIT (measured live, conexus
relay [22485]). Local provisioning (pg_provision.py's _create_roles) issues
no NOINHERIT clause today and keeps PostgreSQL's INHERIT default -- a
divergence tracked separately as nexus-v80f2, not addressed here.
GrantsPgMonitorTest.java (service/src/test/java/dev/nexus/service/) proves
the GRANT itself succeeds/fails-loud, but its provisionAdminAndSvcRoles()
helper mirrors today's local (INHERIT-default) shape, not the cloud
(NOINHERIT) one, so pg_ls_waldir() succeeds there on a PLAIN connection --
it does NOT reproduce the cloud symptom this bead exists to fix.

This module reproduces the MECHANISM live: a self-provisioned cluster with
nexus_svc EXPLICITLY altered NOINHERIT (mirroring the real production/cloud
role shape), proves pg_ls_waldir() permission-denies on a plain nexus_svc
connection, and proves nexus.db.svc_monitor's SET-ROLE escalation actually
fixes it end-to-end.

Real PG, no mocks (integration-over-mocks) -- same self-provisioning
seam/skip policy as tests/db/test_nexus_diag_role.py: pg_bin_dir() downloads
the sigstore-verified nexus-pg bundle; the gate is a genuine self-
provisioning failure, never ambient host PostgreSQL.
"""
from __future__ import annotations

import getpass
import socket
import subprocess

import pytest

from tests.db._service_fixture import pg_bin_dir

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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def svc_monitor_cluster(tmp_path_factory):
    """Scratch cluster with nexus_svc EXPLICITLY NOINHERIT + pg_monitor
    granted -- the real production/cloud shape (grants-004 having applied),
    reconstructed by hand since this module runs no Liquibase."""
    from nexus.db.pg_provision import (
        PgBinaries,
        _create_roles,
        _init_cluster,
        _start_cluster,
        _configure_cluster,
        _create_db,
    )

    bins = PgBinaries.from_dir(pg_bin_dir())
    pgdata = tmp_path_factory.mktemp("svc-monitor-pg") / "data"
    port = _free_port()
    os_user = getpass.getuser()

    _init_cluster(bins, pgdata, os_user)
    _configure_cluster(pgdata, port)
    _start_cluster(bins, pgdata, port)
    _create_db(bins, port, os_user)

    created = _create_roles(bins, port, os_user, "admin-pw", "svc-pw", "diag-pw")
    assert created.svc_created is True  # non-vacuity: the role really was made

    def su(sql: str) -> str:
        proc = subprocess.run(
            [str(bins.psql), "-h", "127.0.0.1", "-p", str(port), "-U", os_user,
             "-d", "nexus", "-v", "ON_ERROR_STOP=1", "-tAc", sql],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    # THE MECHANISM FIXTURE (not addressed by pg_provision.py's default —
    # that omits NOINHERIT for a from-scratch local install; production/
    # cloud roles are NOINHERIT deliberately, see svc_monitor.py's module
    # docstring). Reproduce it explicitly, then apply grants-004's own
    # action (GRANT pg_monitor TO nexus_svc — _create_roles already ran
    # pg_provision.py's `GRANT pg_monitor TO nexus_admin WITH ADMIN
    # OPTION` bootstrap, so nexus_admin could grant this onward too; doing
    # it directly as superuser here is equivalent and keeps this fixture
    # free of a Liquibase dependency).
    su("ALTER ROLE nexus_svc NOINHERIT")
    su("GRANT pg_monitor TO nexus_svc")

    def svc_psql(sql: str) -> subprocess.CompletedProcess:
        import os as _os
        env = dict(_os.environ, PGPASSWORD="svc-pw")
        return subprocess.run(
            [str(bins.psql), "-h", "127.0.0.1", "-p", str(port),
             "-U", "nexus_svc", "-d", "nexus", "-v", "ON_ERROR_STOP=1",
             "-tAc", sql],
            capture_output=True, text=True, timeout=30, env=env,
        )

    yield {"su": su, "svc_psql": svc_psql, "port": port, "bins": bins}

    subprocess.run(
        [str(bins.pg_ctl), "-D", str(pgdata), "stop", "-m", "immediate"],
        capture_output=True, text=True, timeout=30,
    )


class TestMembershipAloneIsNotUsablePrivilege:
    """The bead's core claim, falsified live rather than merely argued."""

    def test_nexus_svc_holds_pg_monitor_membership(self, svc_monitor_cluster):
        row = svc_monitor_cluster["su"](
            "SELECT pg_has_role('nexus_svc', 'pg_monitor', 'member')"
        )
        assert row == "t"

    def test_nexus_svc_is_noinherit(self, svc_monitor_cluster):
        row = svc_monitor_cluster["su"](
            "SELECT rolinherit FROM pg_roles WHERE rolname = 'nexus_svc'"
        )
        assert row == "f"

    def test_plain_connection_permission_denies_on_pg_ls_waldir(self, svc_monitor_cluster):
        """THE symptom: membership held, privilege unusable without SET ROLE."""
        proc = svc_monitor_cluster["svc_psql"]("SELECT count(*) FROM pg_ls_waldir()")
        assert proc.returncode != 0
        assert "permission denied" in proc.stderr.lower()

    def test_set_role_then_same_query_succeeds(self, svc_monitor_cluster):
        """THE fix, by hand: SET ROLE pg_monitor in the same session makes
        the identical query succeed."""
        import os as _os

        env = dict(_os.environ, PGPASSWORD="svc-pw")
        bins = svc_monitor_cluster["bins"]
        proc = subprocess.run(
            [str(bins.psql), "-h", "127.0.0.1", "-p", str(svc_monitor_cluster["port"]),
             "-U", "nexus_svc", "-d", "nexus", "-v", "ON_ERROR_STOP=1",
             "-t", "-A", "-q",
             "-c", "SET ROLE pg_monitor", "-c", "SELECT count(*) FROM pg_ls_waldir()"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert int(proc.stdout.strip()) >= 1


class TestSvcMonitorHelperLive:
    """nexus.db.svc_monitor's escalation, end-to-end against the live
    NOINHERIT cluster -- the product path, not a hand-rolled psql call."""

    def test_wal_retained_bytes_succeeds_via_helper(self, svc_monitor_cluster):
        from nexus.db.svc_monitor import SvcCredentials, wal_retained_bytes

        creds = SvcCredentials(
            port=svc_monitor_cluster["port"], user="nexus_svc", password="svc-pw",
        )
        n = wal_retained_bytes(creds, psql_bin=pg_bin_dir() / "psql")
        assert n >= 0  # a fresh scratch cluster's current WAL segment size

    def test_wal_retention_report_renders_byte_count(self, svc_monitor_cluster):
        """wal_retention_report resolves credentials from a real
        pg_credentials-shaped file — end to end, not a monkeypatched
        resolver — then goes through the same SET-ROLE escalation."""
        import tempfile
        from pathlib import Path

        from nexus.db.svc_monitor import wal_retention_report

        with tempfile.TemporaryDirectory() as td:
            creds_path = Path(td) / "pg_credentials"
            creds_path.write_text(
                f"PG_PORT={svc_monitor_cluster['port']}\n"
                "NX_DB_USER=nexus_svc\nNX_DB_PASS=svc-pw\n"
            )
            text = wal_retention_report(creds_path=creds_path, psql_bin=pg_bin_dir() / "psql")
        assert text.startswith("WAL retention (local service): ")
        assert "bytes retained" in text
        assert "UNMEASURED" not in text

    def test_missing_membership_fails_loud_with_grants_004_remedy(self, svc_monitor_cluster):
        """Revoke pg_monitor to reproduce the pre-grants-004 state; the
        helper must fail loud naming the remedy, never silently degrade."""
        from nexus.db.svc_monitor import SvcCredentials, wal_retained_bytes

        svc_monitor_cluster["su"]("REVOKE pg_monitor FROM nexus_svc")
        try:
            creds = SvcCredentials(
                port=svc_monitor_cluster["port"], user="nexus_svc", password="svc-pw",
            )
            with pytest.raises(RuntimeError, match="grants-004-monitor-wal-visibility"):
                wal_retained_bytes(creds, psql_bin=pg_bin_dir() / "psql")
        finally:
            svc_monitor_cluster["su"]("GRANT pg_monitor TO nexus_svc")  # restore for siblings
