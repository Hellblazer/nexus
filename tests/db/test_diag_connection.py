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


class TestLiveStoreDetailLocalOnly:
    """nexus-y3wuu (Hal decision 2026-07-20, option b): the diagnostic path
    is LOCAL-ONLY BY DESIGN — no remote host/dbname/sslmode resolution
    exists. On a non-local (managed/BYO service) deployment the leg must
    refuse with the contract stated, not report 'no nexus_diag credentials'
    (indistinguishable from 'not provisioned'; cost conexus a source-read
    to discover)."""

    def test_non_local_mode_with_nothing_local_refuses_naming_the_contract(
        self, monkeypatch,
    ):
        from unittest.mock import patch

        from nexus.db.diag_connection import live_store_detail

        calls = {"run": 0}

        def _run(statements, creds):
            calls["run"] += 1

        with patch("nexus.config.is_local_mode", return_value=False):
            text = live_store_detail(
                ["SELECT COUNT(*) FROM nexus.chunks_768;"],
                resolve=lambda: None, run=_run,
            )
        assert "LOCAL-ONLY" in text
        assert "by design" in text.lower()
        assert "no nexus_diag credentials" not in text
        # No SQL runs on the refused path.
        assert calls == {"run": 0}

    def test_non_local_heuristic_with_real_local_creds_still_probes(self):
        # Arc-critique SIG-2: is_local_mode() is a heuristic (a self-hosted
        # local service holding cloud embedder keys reads non-local). A box
        # whose LOCAL resolution genuinely succeeds must keep a WORKING
        # probe — the contract refusal fires only when there is actually
        # nothing local to probe.
        from unittest.mock import patch

        from nexus.db.diag_connection import live_store_detail

        with patch("nexus.config.is_local_mode", return_value=False):
            text = live_store_detail(
                ["SELECT 1;"], resolve=lambda: _CREDS, run=lambda s, c: ["9"],
            )
        assert "live diagnostic results" in text
        assert "SELECT 1; = 9" in text
        assert "REFUSED" not in text

    def test_local_mode_no_creds_keeps_unavailable_message(self):
        from unittest.mock import patch

        from nexus.db.diag_connection import live_store_detail

        with patch("nexus.config.is_local_mode", return_value=True):
            text = live_store_detail(
                ["SELECT 1;"], resolve=lambda: None, run=lambda s, c: [],
            )
        assert "UNAVAILABLE" in text
        assert "no nexus_diag credentials" in text
        assert "Do NOT interpret this as a clean store" in text

    def test_local_mode_with_creds_renders_results(self):
        from unittest.mock import patch

        from nexus.db.diag_connection import live_store_detail

        with patch("nexus.config.is_local_mode", return_value=True):
            text = live_store_detail(
                ["SELECT 1;"],
                resolve=lambda: _CREDS,
                run=lambda s, c: ["42"],
            )
        assert "live diagnostic results" in text
        assert "SELECT 1; = 42" in text
