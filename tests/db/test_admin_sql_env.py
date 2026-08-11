# SPDX-License-Identifier: AGPL-3.0-or-later
"""run_admin_sql's psql env carries the nexus-iytd3 bundle-lib loader guard
(GH #1414 era-hop review round 3, 2026-07-21) — the third of the three psql
invocation sites (health._run_psql, diag_connection.run_diagnostic_sql,
admin_sql.run_admin_sql); an unguarded copy is the setup for the next
"which copy has the fix" regression."""
from __future__ import annotations

import os
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from nexus.db.admin_sql import AdminCredentials, run_admin_sql


class _RecordingRunner:
    """Answers every ``to_regclass`` existence probe with "exists" (``t``) so
    tests written before the F14a existence-gate (nexus-o8dil.1) keep
    exercising the full VALIDATE path unchanged."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []

    def __call__(self, argv, env):
        self.calls.append(argv)
        self.envs.append(env)
        stmt = argv[-1]
        stdout = "t" if "to_regclass" in stmt else ""
        return CompletedProcess(argv, 0, stdout=stdout, stderr="")


def _bundle_psql(tmp_path: Path) -> Path:
    (tmp_path / "bundle" / "bin").mkdir(parents=True)
    (tmp_path / "bundle" / "lib").mkdir(parents=True)
    psql = tmp_path / "bundle" / "bin" / "psql"
    psql.write_text("")
    return psql


def test_admin_env_carries_bundle_lib_path(tmp_path, monkeypatch):
    runner = _RecordingRunner()
    psql = _bundle_psql(tmp_path)
    monkeypatch.setattr(
        "nexus.db.admin_sql.resolve_admin_credentials",
        lambda creds_path=None: AdminCredentials(port=5599, user="nexus_admin", password="apw"),
    )
    ok = run_admin_sql(
        ["ALTER TABLE nexus.chunks_768 VALIDATE CONSTRAINT chunks_768_chash_octet_check"],
        psql_bin=psql, psql_runner=runner,
    )
    assert ok is True
    env = runner.envs[0]
    lib = str(tmp_path / "bundle" / "lib")
    assert env.get("LD_LIBRARY_PATH", "").split(os.pathsep)[0] == lib
    assert env["PGPASSWORD"] == "apw"


class TestExistenceGate:
    """F14a (nexus-o8dil.1): a VALIDATE statement whose target relation is
    absent must be SKIPPED, never raised -- the chash_index shape (RDR-187
    dropped the table; a stale VALIDATE against it would crash-loop `nx
    upgrade` forever, per the chash_rekey.py OCTET_CHECKS comment). Existence
    is checked via ``to_regclass`` over the SAME admin connection, before
    each ALTER TABLE ... VALIDATE CONSTRAINT is attempted."""

    def test_absent_relation_is_skipped_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "nexus.db.admin_sql.resolve_admin_credentials",
            lambda creds_path=None: AdminCredentials(port=5599, user="nexus_admin", password="apw"),
        )
        psql = _bundle_psql(tmp_path)

        def runner(argv, env):
            stmt = argv[-1]
            if "to_regclass" in stmt:
                return CompletedProcess(argv, 0, stdout="", stderr="")  # relation absent
            # Pre-fix, the existence check never runs and this ALTER TABLE
            # is issued directly -- exactly what psql reports for a
            # genuinely dropped relation.
            return CompletedProcess(
                argv, 3, stdout="",
                stderr='ERROR:  relation "nexus.chash_index" does not exist',
            )

        ok = run_admin_sql(
            ["ALTER TABLE nexus.chash_index VALIDATE CONSTRAINT chash_index_chash_octet_check"],
            psql_bin=psql, psql_runner=runner,
        )
        assert ok is True

    def test_present_relations_still_validate_every_statement(self, tmp_path, monkeypatch):
        """Regression: when every relation exists, behaviour is unchanged --
        every statement is still issued (no convergence-count regression)."""
        monkeypatch.setattr(
            "nexus.db.admin_sql.resolve_admin_credentials",
            lambda creds_path=None: AdminCredentials(port=5599, user="nexus_admin", password="apw"),
        )
        psql = _bundle_psql(tmp_path)
        validated: list[str] = []

        def runner(argv, env):
            stmt = argv[-1]
            if "to_regclass" in stmt:
                return CompletedProcess(argv, 0, stdout="t", stderr="")
            validated.append(stmt)
            return CompletedProcess(argv, 0, stdout="", stderr="")

        stmts = [
            "ALTER TABLE nexus.chunks_384 VALIDATE CONSTRAINT chunks_384_chash_octet_check",
            "ALTER TABLE nexus.chunks_768 VALIDATE CONSTRAINT chunks_768_chash_octet_check",
            "ALTER TABLE nexus.chunks_1024 VALIDATE CONSTRAINT chunks_1024_chash_octet_check",
        ]
        ok = run_admin_sql(stmts, psql_bin=psql, psql_runner=runner)
        assert ok is True
        assert validated == stmts, (
            f"the existence gate must not skip a present relation: {validated!r}"
        )

    def test_a_mix_skips_only_the_absent_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "nexus.db.admin_sql.resolve_admin_credentials",
            lambda creds_path=None: AdminCredentials(port=5599, user="nexus_admin", password="apw"),
        )
        psql = _bundle_psql(tmp_path)
        existing = {"nexus.chunks_768"}
        validated: list[str] = []

        def runner(argv, env):
            stmt = argv[-1]
            if "to_regclass" in stmt:
                table = stmt.split("'")[1]
                return CompletedProcess(argv, 0, stdout=("t" if table in existing else ""), stderr="")
            validated.append(stmt)
            return CompletedProcess(argv, 0, stdout="", stderr="")

        ok = run_admin_sql(
            [
                "ALTER TABLE nexus.chunks_384 VALIDATE CONSTRAINT chunks_384_chash_octet_check",
                "ALTER TABLE nexus.chunks_768 VALIDATE CONSTRAINT chunks_768_chash_octet_check",
            ],
            psql_bin=psql, psql_runner=runner,
        )
        assert ok is True
        assert validated == [
            "ALTER TABLE nexus.chunks_768 VALIDATE CONSTRAINT chunks_768_chash_octet_check",
        ]

    def test_existence_check_itself_failing_raises_loud(self, tmp_path, monkeypatch):
        """A probe that cannot even run (permission denied, connection
        refused) must not be silently read as 'absent' -- that would
        silently skip a VALIDATE that might genuinely be needed. Fail
        loud, per the project's no-silent-fallback-for-correctness rule."""
        monkeypatch.setattr(
            "nexus.db.admin_sql.resolve_admin_credentials",
            lambda creds_path=None: AdminCredentials(port=5599, user="nexus_admin", password="apw"),
        )
        psql = _bundle_psql(tmp_path)

        def runner(argv, env):
            return CompletedProcess(argv, 2, stdout="", stderr="psql: error: connection refused")

        with pytest.raises(RuntimeError, match="existence check"):
            run_admin_sql(
                ["ALTER TABLE nexus.chunks_384 VALIDATE CONSTRAINT chunks_384_chash_octet_check"],
                psql_bin=psql, psql_runner=runner,
            )
