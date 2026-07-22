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

from nexus.db.admin_sql import AdminCredentials, run_admin_sql


class _RecordingRunner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []

    def __call__(self, argv, env):
        self.calls.append(argv)
        self.envs.append(env)
        return CompletedProcess(argv, 0, stdout="", stderr="")


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
