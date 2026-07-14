# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 P2.1: the nexus_diag connection choke point (unit tier, no PG).

The content boundary for the diagnostic role is enforced HERE, not by the
role's DB grants (critic-foundations Critical-1): run_diagnostic_sql lints
every statement read-only + metadata-scoped BEFORE any DB contact, and wraps
execution in SET TRANSACTION READ ONLY. These tests prove the choke point
mechanically with a recording runner — a refused statement never reaches
psql at all.
"""
from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from nexus.db.diag_connection import (
    DiagCredentials,
    resolve_diag_credentials,
    run_diagnostic_sql,
)
from nexus.remediation.sql_lint import DiagnosticSqlViolation

_CREDS = DiagCredentials(port=5599, user="nexus_diag", password="pw")


class _RecordingRunner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []

    def __call__(self, argv, env):
        self.calls.append(argv)
        self.envs.append(env)
        return CompletedProcess(argv, 0, stdout="42\n", stderr="")


class TestChokePoint:
    def test_mutating_statement_never_reaches_psql(self):
        runner = _RecordingRunner()
        with pytest.raises(DiagnosticSqlViolation):
            run_diagnostic_sql(
                ["SELECT count(*) FROM nexus.chunks_768",
                 "DELETE FROM nexus.memory"],
                _CREDS, psql_bin=Path("/nope/psql"), psql_runner=runner,
            )
        assert runner.calls == []  # NOTHING executed — lint precedes DB contact

    def test_content_reading_statement_never_reaches_psql(self):
        runner = _RecordingRunner()
        with pytest.raises(DiagnosticSqlViolation):
            run_diagnostic_sql(
                ["SELECT content FROM nexus.memory"],
                _CREDS, psql_bin=Path("/nope/psql"), psql_runner=runner,
            )
        assert runner.calls == []

    def test_clean_statements_run_in_read_only_session(self):
        runner = _RecordingRunner()
        out = run_diagnostic_sql(
            ["SELECT count(*) FROM nexus.chunks_768 WHERE length(chash) <> 32"],
            _CREDS, psql_bin=Path("/x/psql"), psql_runner=runner,
        )
        assert out == ["42"]
        (argv,) = runner.calls
        assert argv[argv.index("-U") + 1] == "nexus_diag"
        (env,) = runner.envs
        # Whole-session read-only guard rides the connection itself.
        assert env["PGOPTIONS"] == "-c default_transaction_read_only=on"
        assert env["PGPASSWORD"] == "pw"

    def test_psql_failure_raises_with_stderr(self):
        def failing(argv, env):
            return CompletedProcess(argv, 1, stdout="", stderr="boom")

        with pytest.raises(RuntimeError, match="boom"):
            run_diagnostic_sql(
                ["SELECT count(*) FROM public.databasechangelog"],
                _CREDS, psql_bin=Path("/x/psql"), psql_runner=failing,
            )


class TestCredentialResolution:
    def test_absent_file_returns_none(self, tmp_path):
        assert resolve_diag_credentials(tmp_path / "nope") is None

    def test_pre_p21_file_without_diag_keys_returns_none(self, tmp_path):
        p = tmp_path / "pg_credentials"
        p.write_text("PG_PORT=5599\nNX_DB_ADMIN_USER=nexus_admin\n")
        assert resolve_diag_credentials(p) is None

    def test_complete_file_resolves(self, tmp_path):
        p = tmp_path / "pg_credentials"
        p.write_text(
            "PG_PORT=5599\nNX_DB_DIAG_USER=nexus_diag\nNX_DB_DIAG_PASS=s3cret\n"
        )
        creds = resolve_diag_credentials(p)
        assert creds == DiagCredentials(port=5599, user="nexus_diag", password="s3cret")

    def test_bad_port_returns_none(self, tmp_path):
        p = tmp_path / "pg_credentials"
        p.write_text(
            "PG_PORT=banana\nNX_DB_DIAG_USER=nexus_diag\nNX_DB_DIAG_PASS=x\n"
        )
        assert resolve_diag_credentials(p) is None

    def test_unreadable_file_returns_none(self, tmp_path):
        """(review-foundations Low) An unreadable credentials file degrades to
        None like every other resolution failure — never a raised OSError."""
        import os as _os
        import pytest as _pytest

        p = tmp_path / "pg_credentials"
        p.write_text("PG_PORT=5599\nNX_DB_DIAG_USER=nexus_diag\nNX_DB_DIAG_PASS=x\n")
        p.chmod(0o000)
        if _os.access(p, _os.R_OK):  # root ignores modes; skip rather than lie
            _pytest.skip("cannot make file unreadable for this user")
        try:
            assert resolve_diag_credentials(p) is None
        finally:
            p.chmod(0o600)
