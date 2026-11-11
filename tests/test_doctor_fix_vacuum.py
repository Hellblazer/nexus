# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1609 follow-up: `nx doctor --fix` runs the dead-tuple VACUUM the tuples.dead_tuple_ratio row asks for.

The row's fix line was raw SQL (`VACUUM ANALYZE nexus.<table>;`), which needs the database password a user does not
have at hand. `--fix` now runs it through the same local admin credentials (`pg_credentials`) the row reads, only on
a table the row would fail (dead rows past twice its autovacuum trigger and a ratio over the warning line), and
`--fix --dry-run` lists what it would vacuum. A managed service is the operator's database: nothing is run there.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

import nexus.health as h
from nexus.commands import doctor as doctor_mod
from tests.test_health_tuple_doctor_rows import _make_creds_file, _psql_runner

#: tuples is over the line (90 % dead, far past its floor); tuple_claim_log is small and not judged.
_STATS = "tuples|100|900|never|never\ntuple_claim_log|32|30|never|never\n"


def _responder(seen: list[str]):
    def respond(sql: str, cmd: list[str]) -> subprocess.CompletedProcess:
        seen.append(sql)
        out = _STATS if "pg_stat_user_tables" in sql else ""
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=out, stderr="")
    return respond


def test_the_row_points_at_nx_doctor_fix(tmp_path: Path) -> None:
    r = h._check_tuple_table_bloat(
        creds_path=_make_creds_file(tmp_path), psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(_responder([])),
    )[0]
    assert r.ok is False
    assert r.fix_suggestions and r.fix_suggestions[0].startswith("nx doctor --fix"), r.fix_suggestions
    assert "VACUUM ANALYZE nexus.tuples" in r.fix_suggestions[0]


def test_fix_vacuums_only_the_table_over_the_line(tmp_path: Path) -> None:
    seen: list[str] = []
    lines = h.vacuum_tuple_tables(
        dry_run=False, creds_path=_make_creds_file(tmp_path), psql_bin=Path("/fake/psql"),
        psql_runner=_psql_runner(_responder(seen)),
    )
    vacuums = [s for s in seen if "VACUUM" in s]
    assert len(vacuums) == 1 and "VACUUM ANALYZE nexus.tuples" in vacuums[0], seen
    assert "tuple_claim_log" not in vacuums[0]
    assert lines == ["Vacuumed nexus.tuples (dead-tuple ratio 90.00%)."], lines


def test_dry_run_lists_and_runs_nothing(tmp_path: Path) -> None:
    seen: list[str] = []
    lines = h.vacuum_tuple_tables(
        dry_run=True, creds_path=_make_creds_file(tmp_path), psql_bin=Path("/fake/psql"),
        psql_runner=_psql_runner(_responder(seen)),
    )
    assert not [s for s in seen if "VACUUM" in s]
    assert lines == ["--fix would VACUUM ANALYZE nexus.tuples (dead-tuple ratio 90.00%)."], lines


def test_nothing_over_the_line_vacuums_nothing(tmp_path: Path) -> None:
    seen: list[str] = []

    def respond(sql: str, cmd: list[str]) -> subprocess.CompletedProcess:
        seen.append(sql)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="tuples|1000|10|never|never\n", stderr="")

    lines = h.vacuum_tuple_tables(
        dry_run=False, creds_path=_make_creds_file(tmp_path), psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(respond),
    )
    assert not [s for s in seen if "VACUUM" in s] and lines == []


def test_a_managed_service_is_left_to_its_operator(tmp_path: Path) -> None:
    with patch("nexus.config.is_local_mode", return_value=False):
        lines = h.vacuum_tuple_tables(dry_run=False, creds_path=tmp_path / "pg_credentials")
    assert lines == ["Dead-tuple VACUUM skipped: a managed service's database is its operator's to vacuum."], lines


def test_a_failed_vacuum_is_reported(tmp_path: Path) -> None:
    def respond(sql: str, cmd: list[str]) -> subprocess.CompletedProcess:
        if "VACUUM" in sql:
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="ERROR: permission denied")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_STATS, stderr="")

    lines = h.vacuum_tuple_tables(
        dry_run=False, creds_path=_make_creds_file(tmp_path), psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(respond),
    )
    assert lines == ["VACUUM of nexus.tuples failed (psql exit 1): ERROR: permission denied"], lines


def test_doctor_fix_wires_the_vacuum_and_fails_on_a_failed_one(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: object())
    monkeypatch.setattr("nexus.garbage.catalog_garbage", lambda _r: SimpleNamespace(
        error=None, orphaned_links=0, trash_documents=0, stranded_chunks=0))
    monkeypatch.setattr(h, "vacuum_tuple_tables", lambda *, dry_run: calls.append(dry_run) or (
        ["--fix would VACUUM ANALYZE nexus.tuples (dead-tuple ratio 90.00%)."] if dry_run
        else ["VACUUM of nexus.tuples failed (psql exit 1): ERROR: x"]))
    preview = CliRunner().invoke(doctor_mod.doctor_cmd, ["--fix", "--dry-run"])
    assert preview.exit_code == 0 and "--fix would VACUUM ANALYZE nexus.tuples" in preview.output, preview.output

    class _Writer:
        def close(self) -> None: ...

    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: _Writer())
    monkeypatch.setattr("nexus.garbage.reclaim_catalog_garbage", lambda _w, reader: SimpleNamespace(
        links_deleted=0, trash_documents=0, stranded_chunks=0))
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)  # stops after the VACUUM, before HNSW tuning
    run = CliRunner().invoke(doctor_mod.doctor_cmd, ["--fix"])
    assert run.exit_code != 0 and "VACUUM of nexus.tuples failed" in run.output, run.output
    assert calls == [True, False]
