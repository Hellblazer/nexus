# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sz651: ``scripts/ddl_ratchet.py`` fails a run whose DDL count passes its ceiling, and
refuses a measurement that proves nothing."""
from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent.parent
spec = importlib.util.spec_from_file_location("ddl_ratchet", _REPO / "scripts" / "ddl_ratchet.py")
ratchet = importlib.util.module_from_spec(spec)
sys.modules["ddl_ratchet"] = ratchet
spec.loader.exec_module(ratchet)


def _tally(total: int) -> dict:
    return {"total": total, "toplevel": 0, "nested": total, "by_kind": {}}


def _py_report(tmp_path: Path, *, migration=2000, tests=3000, workers=2, tests_run=40) -> Path:
    path = tmp_path / "nx-ddl-report.json"
    path.write_text(json.dumps({
        "workers": workers, "workers_booted": workers, "tests_run": tests_run,
        "buckets": {"migration": _tally(migration), "tests": _tally(tests)},
    }))
    return path


def _java_fork(directory: Path, name: str, *, shared=1, dedicated=2, errors=0, ready=True,
               buckets=(("shared_migration", 2000), ("shared_tests", 500), ("dedicated", 4000))) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps({
        "containers": {"shared": shared, "dedicated": dedicated}, "log_errors": errors,
        "ready_mark_seen": ready, "buckets": {k: _tally(v) for k, v in buckets},
    }))


def _ceilings(tmp_path: Path, suite: str, ceiling, basis) -> Path:
    path = tmp_path / "ceilings.toml"
    value = lambda v: f'"{v}"' if isinstance(v, str) else str(v)  # noqa: E731
    path.write_text(f"[{suite}]\nceiling = {value(ceiling)}\nbasis = {value(basis)}\n")
    return path


def test_the_committed_ceiling_file_names_every_suite() -> None:
    committed = tomllib.loads((_REPO / "tests" / "ddl_ceiling.toml").read_text())
    assert set(committed) == set(ratchet.SUITES)
    for suite, entry in committed.items():
        assert set(entry) == {"ceiling", "basis"}, suite
        for value in entry.values():
            assert value == ratchet.MEASURE or (isinstance(value, int) and value > 0), (suite, value)


def test_an_unset_ceiling_fails_and_prints_the_number_to_commit(tmp_path) -> None:
    code, message = ratchet.check("python_unit", _py_report(tmp_path), _ceilings(tmp_path, "python_unit", "measure", "measure"))
    assert code == ratchet.EXIT_UNSET
    assert "ceiling = 5000 and basis = 2" in message


def test_the_committed_unset_ceilings_fail_a_real_looking_run(tmp_path) -> None:
    code, _ = ratchet.check("python_unit", _py_report(tmp_path))
    assert code == ratchet.EXIT_UNSET


def test_a_run_within_the_ceiling_passes_and_names_unclaimed_headroom(tmp_path) -> None:
    code, message = ratchet.check("python_unit", _py_report(tmp_path), _ceilings(tmp_path, "python_unit", 5200, 2))
    assert code == ratchet.EXIT_OK
    assert "lower ceiling to 5000" in message


def test_a_run_at_the_ceiling_passes_without_a_headroom_note(tmp_path) -> None:
    code, message = ratchet.check("python_unit", _py_report(tmp_path), _ceilings(tmp_path, "python_unit", 5000, 2))
    assert code == ratchet.EXIT_OK
    assert "lower ceiling" not in message


def test_one_statement_over_the_ceiling_fails(tmp_path) -> None:
    code, message = ratchet.check("python_unit", _py_report(tmp_path, tests=3001), _ceilings(tmp_path, "python_unit", 5000, 2))
    assert code == ratchet.EXIT_OVER
    assert "by 1" in message


def test_a_run_at_another_width_is_not_compared(tmp_path) -> None:
    code, message = ratchet.check("python_unit", _py_report(tmp_path, workers=8), _ceilings(tmp_path, "python_unit", 5000, 2))
    assert code == ratchet.EXIT_NOT_COMPARABLE
    assert "8 workers" in message and "measured at 2" in message


@pytest.mark.parametrize("kwargs", [{"workers": 0}, {"tests_run": 0}])
def test_a_python_run_that_counted_nothing_is_not_a_pass(tmp_path, kwargs) -> None:
    code, _ = ratchet.check("python_unit", _py_report(tmp_path, **kwargs), _ceilings(tmp_path, "python_unit", 10**9, kwargs.get("workers", 2)))
    assert code == ratchet.EXIT_NOT_COMPARABLE


def test_java_forks_are_summed_and_a_forkless_fork_is_not_a_fork(tmp_path) -> None:
    d = tmp_path / "ddl-counts"
    _java_fork(d, "ddl-1-1.json")
    _java_fork(d, "ddl-2-1.json")
    _java_fork(d, "ddl-3-1.json", shared=0, dedicated=0, buckets=())  # never touched a database
    code, message = ratchet.check("java_main", d, _ceilings(tmp_path, "java_main", 13000, 2))
    assert code == ratchet.EXIT_OK
    assert "total=13000 basis=2" in message
    assert "dedicated=8000" in message


def test_the_shared_empty_bucket_counts_toward_the_java_total(tmp_path) -> None:
    # nexus-sz651 + nexus-7xfh7: migration-process classes walk the changelog in an empty database on the
    # shared cluster; leaving that bucket out of the sum would hide the cost the dedicated bucket used to carry.
    d = tmp_path / "ddl-counts"
    _java_fork(d, "ddl-1-1.json", buckets=(("shared_migration", 2000), ("shared_tests", 500),
                                           ("shared_empty", 1200), ("dedicated", 4000)))
    code, message = ratchet.check("java_main", d, _ceilings(tmp_path, "java_main", 7700, 1))
    assert code == ratchet.EXIT_OK, message
    assert "total=7700" in message
    assert "shared_empty=1200" in message
    code, _ = ratchet.check("java_main", d, _ceilings(tmp_path, "java_main", 7699, 1))
    assert code == ratchet.EXIT_OVER


def test_a_java_run_over_the_ceiling_fails(tmp_path) -> None:
    d = tmp_path / "ddl-counts"
    _java_fork(d, "ddl-1-1.json")
    code, _ = ratchet.check("java_main", d, _ceilings(tmp_path, "java_main", 6499, 1))
    assert code == ratchet.EXIT_OVER


@pytest.mark.parametrize(
    ("case", "build"),
    [
        ("no directory", lambda d: None),
        ("empty directory", lambda d: d.mkdir()),
        ("a log that could not be read", lambda d: _java_fork(d, "ddl-1-1.json", errors=1)),
        ("a template whose ready mark never logged", lambda d: _java_fork(d, "ddl-1-1.json", ready=False)),
        ("forks that started no container", lambda d: _java_fork(d, "ddl-1-1.json", shared=0, dedicated=0, buckets=())),
    ],
)
def test_a_java_measurement_that_proves_nothing_is_not_a_pass(tmp_path, case, build) -> None:
    d = tmp_path / "ddl-counts"
    build(d)
    code, _ = ratchet.check("java_main", d, _ceilings(tmp_path, "java_main", 10**9, 1))
    assert code == ratchet.EXIT_NOT_COMPARABLE, case


def test_an_unreadable_report_is_a_failed_gate(tmp_path) -> None:
    bad = tmp_path / "nx-ddl-report.json"
    bad.write_text("not json")
    code, _ = ratchet.check("python_unit", bad, _ceilings(tmp_path, "python_unit", 1, 1))
    assert code == ratchet.EXIT_NOT_COMPARABLE


def test_the_cli_exit_status_is_the_check_status(tmp_path, capsys) -> None:
    ceilings = _ceilings(tmp_path, "python_unit", 4999, 2)
    status = ratchet.main(["check", "--suite", "python_unit", "--report", str(_py_report(tmp_path)), "--ceilings", str(ceilings)])
    assert status == ratchet.EXIT_OVER
    assert "ABOVE the ceiling" in capsys.readouterr().out
