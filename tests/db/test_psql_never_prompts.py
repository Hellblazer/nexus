# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every product psql invocation passes ``-w`` (nexus-ja4pq).

The cluster demands passwords now (scram-sha-256). Each of these call sites
reads its password from ``pg_credentials`` with ``""`` as the default for a
missing key, and libpq treats an empty ``PGPASSWORD`` as no password at all.
Without ``-w``, psql then asks for one on the controlling terminal (the console
on Windows) and ``nx doctor`` or ``nx upgrade`` blocks forever instead of
reporting a failed connection. ``capture_output`` does not prevent this: psql
prompts on ``/dev/tty``, not stdin.

Each test drives the real function through its runner seam with an EMPTY
password, the exact condition that would prompt.
"""
from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

from nexus.db.admin_sql import AdminCredentials, run_admin_sql
from nexus.db.diag_connection import DiagCredentials, run_diagnostic_sql
from nexus.db.svc_monitor import SvcCredentials, monitor_scoped_query
from nexus.health import _run_psql


class _Runner:
    def __init__(self, stdout: str = "t"):
        self.calls: list[list[str]] = []
        self.stdout = stdout

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        return CompletedProcess(argv, 0, stdout=self.stdout, stderr="")


def _assert_never_prompts(runner: _Runner) -> None:
    assert runner.calls, "non-vacuity: the function never reached psql"
    for argv in runner.calls:
        assert "-w" in argv, f"psql may block on a password prompt: {argv}"


def test_admin_sql_passes_no_password_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "nexus.db.admin_sql.resolve_admin_credentials",
        lambda creds_path=None: AdminCredentials(port=5599, user="nexus_admin", password=""),
    )
    runner = _Runner()
    run_admin_sql(
        ["ALTER TABLE nexus.chunks VALIDATE CONSTRAINT chunks_chash_octet_check"],
        psql_bin=tmp_path / "psql", psql_runner=runner,
    )
    _assert_never_prompts(runner)


def test_diag_connection_passes_no_password_flag(tmp_path: Path) -> None:
    runner = _Runner()
    run_diagnostic_sql(
        ["SELECT 1"], DiagCredentials(port=5599, user="nexus_diag", password=""),
        psql_bin=tmp_path / "psql", psql_runner=runner,
    )
    _assert_never_prompts(runner)


def test_svc_monitor_passes_no_password_flag(tmp_path: Path) -> None:
    runner = _Runner(stdout="42")
    monitor_scoped_query(
        SvcCredentials(port=5599, user="nexus_svc", password=""), "SELECT 42",
        psql_bin=tmp_path / "psql", psql_runner=runner,
    )
    _assert_never_prompts(runner)


def test_health_run_psql_passes_no_password_flag(tmp_path: Path) -> None:
    runner = _Runner()
    _run_psql(
        tmp_path / "psql", "127.0.0.1", 5599, "nexus", "nexus_admin", "", "SELECT 1",
        psql_runner=runner,
    )
    _assert_never_prompts(runner)
