# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bb5c8: grants-004 delivers nexus_svc pg_monitor MEMBERSHIP, but the
CLOUD deployment's nexus_svc is NOINHERIT (measured live, conexus relay
[22485]) -- under that posture a plain nexus_svc session gets `permission
denied` from pg_ls_waldir() until it explicitly `SET ROLE pg_monitor` first.
Local provisioning (pg_provision.py's _create_roles) currently issues no
NOINHERIT clause and keeps PostgreSQL's INHERIT default (divergence tracked
separately as nexus-v80f2) -- SET ROLE is a harmless no-op there.
nexus.db.svc_monitor issues SET ROLE UNCONDITIONALLY on every call, so it is
correct in both postures without probing which one it is talking to; this
is the ONE product-side place that escalation happens.

Unit tier (this file, no PG): the psql runner is a recording double, same
convention as test_admin_sql_env.py / test_diag_connection.py -- proves the
argv/env shape and the fail-loud-with-remedy classification without a real
cluster. The MECHANISM itself (a real NOINHERIT role really needing SET
ROLE) is proven live in tests/db/test_svc_monitor_role_live.py, which
EXPLICITLY alters the test role NOINHERIT to reproduce the cloud posture
(local provisioning does not do this today).
"""
from __future__ import annotations

import os
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from nexus.db.svc_monitor import (
    SvcCredentials,
    monitor_scoped_query,
    resolve_svc_credentials,
    wal_retained_bytes,
    wal_retention_report,
)

_CREDS = SvcCredentials(port=5599, user="nexus_svc", password="pw")


class _RecordingRunner:
    def __init__(self, stdout: str = "42", returncode: int = 0, stderr: str = ""):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, env):
        self.calls.append(argv)
        self.envs.append(env)
        return CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr=self.stderr)


def _bundle_psql(tmp_path: Path) -> Path:
    (tmp_path / "bundle" / "bin").mkdir(parents=True)
    (tmp_path / "bundle" / "lib").mkdir(parents=True)
    psql = tmp_path / "bundle" / "bin" / "psql"
    psql.write_text("")
    return psql


class TestCredentialResolution:
    def test_absent_file_returns_none(self, tmp_path):
        assert resolve_svc_credentials(tmp_path / "nope") is None

    def test_missing_keys_return_none(self, tmp_path):
        p = tmp_path / "pg_credentials"
        p.write_text("PG_PORT=5599\nNX_DB_ADMIN_USER=nexus_admin\n")
        assert resolve_svc_credentials(p) is None

    def test_complete_file_resolves(self, tmp_path):
        p = tmp_path / "pg_credentials"
        p.write_text("PG_PORT=5599\nNX_DB_USER=nexus_svc\nNX_DB_PASS=s3cret\n")
        creds = resolve_svc_credentials(p)
        assert creds == SvcCredentials(port=5599, user="nexus_svc", password="s3cret")

    def test_bad_port_returns_none(self, tmp_path):
        p = tmp_path / "pg_credentials"
        p.write_text("PG_PORT=banana\nNX_DB_USER=nexus_svc\nNX_DB_PASS=x\n")
        assert resolve_svc_credentials(p) is None

    def test_unreadable_file_returns_none(self, tmp_path):
        p = tmp_path / "pg_credentials"
        p.write_text("PG_PORT=5599\nNX_DB_USER=nexus_svc\nNX_DB_PASS=x\n")
        p.chmod(0o000)
        if os.access(p, os.R_OK):  # root ignores modes; skip rather than lie
            pytest.skip("cannot make file unreadable for this user")
        try:
            assert resolve_svc_credentials(p) is None
        finally:
            p.chmod(0o600)


class TestMonitorScopedQuery:
    def test_set_role_then_query_ride_one_session_same_argv(self, tmp_path):
        """SET ROLE and the real query must be two -c args on the SAME
        psql invocation (one session) -- separate invocations would each
        open a fresh connection and lose the role escalation."""
        runner = _RecordingRunner(stdout="7")
        psql = _bundle_psql(tmp_path)
        out = monitor_scoped_query(
            _CREDS, "SELECT 7", psql_bin=psql, psql_runner=runner,
        )
        assert out == "7"
        assert len(runner.calls) == 1  # ONE invocation, not two
        (argv,) = runner.calls
        c_indices = [i for i, a in enumerate(argv) if a == "-c"]
        assert len(c_indices) == 2
        assert argv[c_indices[0] + 1] == "SET ROLE pg_monitor"
        assert argv[c_indices[1] + 1] == "SELECT 7"
        assert "ON_ERROR_STOP=1" in argv

    def test_read_only_session_and_bundle_lib_env(self, tmp_path):
        runner = _RecordingRunner(stdout="0")
        psql = _bundle_psql(tmp_path)
        monitor_scoped_query(_CREDS, "SELECT 0", psql_bin=psql, psql_runner=runner)
        (env,) = runner.envs
        assert env["PGOPTIONS"] == "-c default_transaction_read_only=on"
        assert env["PGPASSWORD"] == "pw"
        lib = str(tmp_path / "bundle" / "lib")
        assert env.get("LD_LIBRARY_PATH", "").split(os.pathsep)[0] == lib

    def test_set_role_refusal_raises_with_grants_004_remedy(self, tmp_path):
        """The NOINHERIT permission-denied shape this bead exists to fix:
        the error must NAME the remedy, never a bare psql trace."""
        runner = _RecordingRunner(
            returncode=1, stdout="",
            stderr='ERROR:  permission denied to set role "pg_monitor"',
        )
        psql = _bundle_psql(tmp_path)
        with pytest.raises(RuntimeError) as excinfo:
            monitor_scoped_query(_CREDS, "SELECT 1", psql_bin=psql, psql_runner=runner)
        msg = str(excinfo.value)
        assert "grants-004-monitor-wal-visibility" in msg
        assert "pg_monitor MEMBERSHIP" in msg
        assert "ADMIN OPTION" in msg
        assert "docs/configuration.md" in msg
        assert "permission denied to set role" in msg

    def test_other_psql_failure_raises_plain_without_remedy_text(self, tmp_path):
        """A failure NOT shaped like a SET ROLE refusal (e.g. connection
        refused, bad query) must not be mis-classified as the grants-004
        gap -- that would send an operator chasing the wrong fix."""
        runner = _RecordingRunner(
            returncode=2, stdout="", stderr="psql: error: connection refused",
        )
        psql = _bundle_psql(tmp_path)
        with pytest.raises(RuntimeError) as excinfo:
            monitor_scoped_query(_CREDS, "SELECT 1", psql_bin=psql, psql_runner=runner)
        msg = str(excinfo.value)
        assert "connection refused" in msg
        assert "grants-004" not in msg

    def test_bad_query_failure_not_misclassified_as_set_role_refusal(self, tmp_path):
        runner = _RecordingRunner(
            returncode=1, stdout="", stderr='ERROR:  syntax error at or near "SELCT"',
        )
        psql = _bundle_psql(tmp_path)
        with pytest.raises(RuntimeError) as excinfo:
            monitor_scoped_query(_CREDS, "SELCT 1", psql_bin=psql, psql_runner=runner)
        assert "grants-004" not in str(excinfo.value)


class TestWalRetainedBytes:
    def test_returns_parsed_int(self, tmp_path):
        runner = _RecordingRunner(stdout="8388608")
        psql = _bundle_psql(tmp_path)
        n = wal_retained_bytes(_CREDS, psql_bin=psql, psql_runner=runner)
        assert n == 8388608
        (argv,) = runner.calls
        c_indices = [i for i, a in enumerate(argv) if a == "-c"]
        assert "pg_ls_waldir" in argv[c_indices[1] + 1]

    def test_set_role_refusal_propagates(self, tmp_path):
        runner = _RecordingRunner(
            returncode=1, stdout="",
            stderr='ERROR:  permission denied to set role "pg_monitor"',
        )
        psql = _bundle_psql(tmp_path)
        with pytest.raises(RuntimeError, match="grants-004"):
            wal_retained_bytes(_CREDS, psql_bin=psql, psql_runner=runner)


class TestWalRetentionReport:
    def test_no_credentials_reports_unmeasured(self, tmp_path):
        text = wal_retention_report(creds_path=tmp_path / "nope")
        assert text.startswith("WAL retention: UNMEASURED")
        assert "no local nexus_svc credentials" in text

    def test_set_role_refusal_reports_unmeasured_with_remedy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "nexus.db.svc_monitor.resolve_svc_credentials",
            lambda creds_path=None: _CREDS,
        )
        runner = _RecordingRunner(
            returncode=1, stdout="",
            stderr='ERROR:  permission denied to set role "pg_monitor"',
        )
        psql = _bundle_psql(tmp_path)
        text = wal_retention_report(psql_bin=psql, psql_runner=runner)
        assert text.startswith("WAL retention: UNMEASURED")
        assert "grants-004-monitor-wal-visibility" in text

    def test_genuine_sample_renders_byte_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "nexus.db.svc_monitor.resolve_svc_credentials",
            lambda creds_path=None: _CREDS,
        )
        runner = _RecordingRunner(stdout="16777216")
        psql = _bundle_psql(tmp_path)
        text = wal_retention_report(psql_bin=psql, psql_runner=runner)
        assert text == "WAL retention (local service): 16777216 bytes retained"
