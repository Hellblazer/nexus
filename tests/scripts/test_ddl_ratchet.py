# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sz651: ``scripts/ddl_ratchet.py`` fails a run that wrote more bytes (or ran more DDL) than its
committed ceiling, compares only numbers that do not move with how the work was split, and refuses a
measurement that proves nothing."""
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


def _py_report(tmp_path: Path, *, name="nx-ddl-report.json", migration_ddl=2000, tests_ddl=3000, workers=2,
               tests_run=40, wal_migration=200_000, wal_tests=400_000, ready=None, wal_complete=True) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({
        "workers": workers, "workers_booted": workers, "tests_run": tests_run,
        "workers_ready_mark_seen": workers if ready is None else ready,
        "buckets": {"boot": _tally(6), "migration": _tally(migration_ddl), "tests": _tally(tests_ddl)},
        "total": _tally(migration_ddl + tests_ddl),
        "wal": {
            "migration_bytes": wal_migration if wal_complete else None,
            "tests_bytes": wal_tests if wal_complete else None,
            "total_bytes": wal_migration + wal_tests if wal_complete else None,
            "dbsize_growth_bytes": 1,
            "workers_complete": workers if wal_complete else 0,
        },
        "uncounted_cluster_start_sites": 59,
    }))
    return path


def _java_fork(directory: Path, name: str, *, suite="main", run="r1", shared=1, dedicated=2, errors=0, ready=True,
               unread=0, incomplete=0, mig_ddl=2000, tests_ddl=500, empty_ddl=0, dedicated_ddl=4000,
               mig_bytes=300_000, tests_bytes=50_000, dedicated_bytes=700_000) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps({
        "suite": suite, "run": run,
        "containers": {"shared": shared, "dedicated": dedicated}, "containers_unread": unread, "log_errors": errors,
        "ready_mark_seen": ready,
        "buckets": {
            "boot": _tally(8), "shared_migration": _tally(mig_ddl), "shared_tests": _tally(tests_ddl),
            "shared_empty": _tally(empty_ddl), "dedicated": _tally(dedicated_ddl),
        },
        "wal": {
            "shared_migration_bytes": mig_bytes if shared else 0, "shared_tests_bytes": tests_bytes if shared else 0,
            "dedicated_bytes": dedicated_bytes if dedicated else 0, "incomplete_containers": incomplete,
        },
    }))


def _ceilings(tmp_path: Path, section: str, **entries) -> Path:
    path = tmp_path / "ceilings.toml"
    value = lambda v: f'"{v}"' if isinstance(v, str) else str(v)  # noqa: E731
    body = "".join(f"{k} = {value(v)}\n" for k, v in entries.items())
    path.write_text(f"[{section}]\n{body}")
    return path


#: What `_py_report()` measures: 400k WAL bytes and 3000 DDL over 40 tests; a 200k / 2000 migration over 2 workers.
PY = {"wal_bytes_per_test": 10_000, "ddl_per_test": 75, "migration_wal_bytes_per_worker": 100_000,
      "migration_ddl_per_worker": 1000}
#: What `_java_fork()` x1 measures.
JV = {"migration_wal_bytes_per_fork": 300_000, "migration_ddl_per_fork": 2000, "other_wal_bytes": 750_000,
      "other_ddl": 4500}


def _py_ceilings(tmp_path, **overrides) -> Path:
    return _ceilings(tmp_path, "python_unit", bytes_tolerance=0.05, ddl_tolerance=0.0, **{**PY, **overrides})


def _jv_ceilings(tmp_path, suite="java_main", **overrides) -> Path:
    return _ceilings(tmp_path, suite, bytes_tolerance=0.05, ddl_tolerance=0.0, **{**JV, **overrides})


# ── the committed file ───────────────────────────────────────────────────────


def test_the_committed_ceiling_file_names_every_suite_and_metric() -> None:
    committed = tomllib.loads((_REPO / "tests" / "ddl_ceiling.toml").read_text())
    assert set(committed) == {*ratchet.SUITES, ratchet.TOTAL_SECTION}
    shape = {
        "python_unit": ratchet.PYTHON_METRICS,
        "java_main": ratchet.JAVA_METRICS,
        "java_integration": ratchet.JAVA_METRICS,
        ratchet.TOTAL_SECTION: (*ratchet.TOTAL_METRICS, *ratchet.TOTAL_BASIS),
    }
    for section, entry in committed.items():
        assert set(entry) == {"bytes_tolerance", "ddl_tolerance", *shape[section]}, section
        assert entry["bytes_tolerance"] >= 0 and entry["ddl_tolerance"] >= 0
        # "measure" until a full run supplies the number, then a positive number; the file does not
        # pin which, so committing the measured ceilings is not a test change.
        for name in shape[section]:
            value = entry[name]
            assert value == ratchet.MEASURE or (isinstance(value, (int, float)) and value > 0), (section, name, value)


def test_the_exit_codes_are_distinct_from_argparse_and_from_a_crash() -> None:
    codes = {ratchet.EXIT_OK, ratchet.EXIT_OVER, ratchet.EXIT_UNSET, ratchet.EXIT_NOT_COMPARABLE}
    assert len(codes) == 4
    assert not codes & {1, 2}
    with pytest.raises(SystemExit) as exc:
        ratchet.main(["check", "--suite", "nonsense", "--report", "x"])
    assert exc.value.code == 2


# ── python: intensive numbers, per shard ─────────────────────────────────────


def test_an_unset_ceiling_does_not_pass_and_prints_the_numbers_to_commit(tmp_path) -> None:
    ceilings = _py_ceilings(tmp_path, wal_bytes_per_test="measure", ddl_per_test="measure")
    code, message = ratchet.check("python_unit", _py_report(tmp_path), ceilings)
    assert code == ratchet.EXIT_UNSET
    assert "wal_bytes_per_test = 10000" in message
    assert "ddl_per_test = 75" in message


def test_unset_ceilings_fail_a_real_looking_run(tmp_path) -> None:
    """A ceilings file with every metric still "measure" (a temp file: the committed one goes from
    "measure" to real numbers after the first full run, and this must not turn red when it does)."""
    unset = _py_ceilings(tmp_path, **{name: "measure" for name in ratchet.PYTHON_METRICS})
    code, message = ratchet.check("python_unit", _py_report(tmp_path), unset)
    assert code == ratchet.EXIT_UNSET, message


def test_a_run_at_every_ceiling_passes(tmp_path) -> None:
    code, message = ratchet.check("python_unit", _py_report(tmp_path), _py_ceilings(tmp_path))
    assert code == ratchet.EXIT_OK, message
    assert "uncounted_cluster_start_sites=59 (static tree census)" in message


def test_bytes_pass_inside_the_tolerance_band_and_fail_outside_it(tmp_path) -> None:
    # 400000 / 40 = 10000 per test; ceiling 9600 -> band top 10080.
    assert ratchet.check("python_unit", _py_report(tmp_path), _py_ceilings(tmp_path, wal_bytes_per_test=9600))[0] == ratchet.EXIT_OK
    code, message = ratchet.check("python_unit", _py_report(tmp_path), _py_ceilings(tmp_path, wal_bytes_per_test=9500))
    assert code == ratchet.EXIT_OVER
    assert "wal_bytes_per_test" in message and "+5% band" in message


def test_ddl_has_no_band_by_default_so_one_statement_per_test_over_fails(tmp_path) -> None:
    code, message = ratchet.check("python_unit", _py_report(tmp_path, tests_ddl=3041), _py_ceilings(tmp_path))
    assert code == ratchet.EXIT_OVER
    assert "ddl_per_test" in message


def test_the_migration_is_judged_per_worker_so_width_does_not_matter(tmp_path) -> None:
    # Same per-worker migration and the same per-test numbers on 8 workers and 4x the tests: a pass,
    # where the old whole-run total (migration x workers) would have been "not comparable".
    wide = _py_report(tmp_path, workers=8, tests_run=160, migration_ddl=8000, tests_ddl=12000,
                      wal_migration=800_000, wal_tests=1_600_000)
    code, message = ratchet.check("python_unit", wide, _py_ceilings(tmp_path))
    assert code == ratchet.EXIT_OK, message


def test_a_heavier_migration_per_worker_fails_at_any_width(tmp_path) -> None:
    heavy = _py_report(tmp_path, workers=8, migration_ddl=8000 * 2)
    code, message = ratchet.check("python_unit", heavy, _py_ceilings(tmp_path))
    assert code == ratchet.EXIT_OVER
    assert "migration_ddl_per_worker" in message


def test_more_tests_with_the_same_per_test_cost_pass(tmp_path) -> None:
    more = _py_report(tmp_path, tests_run=80, tests_ddl=6000, wal_tests=800_000)
    assert ratchet.check("python_unit", more, _py_ceilings(tmp_path))[0] == ratchet.EXIT_OK


@pytest.mark.parametrize(
    "kwargs",
    [{"workers": 0}, {"tests_run": 0}, {"wal_complete": False}, {"ready": 1}],
    ids=["no worker booted", "no tests", "bytes unmeasured", "a ready mark missing"],
)
def test_a_python_run_that_proves_nothing_is_not_a_pass(tmp_path, kwargs) -> None:
    code, message = ratchet.check("python_unit", _py_report(tmp_path, **kwargs), _py_ceilings(tmp_path))
    assert code == ratchet.EXIT_NOT_COMPARABLE, message


def test_an_unreadable_report_is_a_failed_gate(tmp_path) -> None:
    bad = tmp_path / "nx-ddl-report.json"
    bad.write_text("not json")
    assert ratchet.check("python_unit", bad, _py_ceilings(tmp_path))[0] == ratchet.EXIT_NOT_COMPARABLE


def test_a_ceiling_file_missing_a_metric_is_not_a_pass(tmp_path) -> None:
    ceilings = _ceilings(tmp_path, "python_unit", bytes_tolerance=0.05, wal_bytes_per_test=1)
    code, message = ratchet.check("python_unit", _py_report(tmp_path), ceilings)
    assert code == ratchet.EXIT_NOT_COMPARABLE
    assert "ddl_per_test" in message or "ddl_tolerance" in message or "no '" in message


# ── fan-in: the full-run total ───────────────────────────────────────────────


def _total_ceilings(tmp_path, **overrides) -> Path:
    entries = {"wal_bytes": 1_200_000, "ddl": 15_000, "workers": 6, "shards": 3, **overrides}
    return _ceilings(tmp_path, ratchet.TOTAL_SECTION, bytes_tolerance=0.05, ddl_tolerance=0.0, **entries)


def _shards(tmp_path, n=3, **kwargs) -> list[Path]:
    return [_py_report(tmp_path, name=f"shard{i}.json", **kwargs) for i in range(n)]


def test_fan_in_sums_the_shards_and_compares_the_total(tmp_path) -> None:
    # each shard: 600k WAL, 5000 DDL, 2 workers
    code, message = ratchet.check_fan_in("python_unit", _shards(tmp_path), 3, _total_ceilings(tmp_path, wal_bytes=1_800_000, ddl=15_000))
    assert code == ratchet.EXIT_OK, message
    assert "wal_bytes=1800000" in message and "ddl=15000" in message


def test_fan_in_over_the_total_fails(tmp_path) -> None:
    code, message = ratchet.check_fan_in("python_unit", _shards(tmp_path), 3, _total_ceilings(tmp_path, wal_bytes=1_800_000, ddl=14_999))
    assert code == ratchet.EXIT_OVER, message


def test_fan_in_refuses_a_missing_shard(tmp_path) -> None:
    code, message = ratchet.check_fan_in("python_unit", _shards(tmp_path, n=2), 3, _total_ceilings(tmp_path))
    assert code == ratchet.EXIT_NOT_COMPARABLE
    assert "2 shard report(s) given, 3 expected" in message


def test_fan_in_refuses_a_total_measured_at_another_width(tmp_path) -> None:
    code, message = ratchet.check_fan_in("python_unit", _shards(tmp_path), 3, _total_ceilings(tmp_path, workers=12))
    assert code == ratchet.EXIT_NOT_COMPARABLE
    assert "6 workers" in message and "measured at 12" in message


def test_fan_in_with_an_unset_total_prints_the_numbers_including_the_basis(tmp_path) -> None:
    ceilings = _total_ceilings(tmp_path, wal_bytes="measure", ddl="measure", workers="measure", shards="measure")
    code, message = ratchet.check_fan_in("python_unit", _shards(tmp_path), 3, ceilings)
    assert code == ratchet.EXIT_UNSET
    for line in ("wal_bytes = 1800000", "ddl = 15000", "workers = 6", "shards = 3"):
        assert line in message


def test_fan_in_with_one_unmeasured_shard_is_not_a_total(tmp_path) -> None:
    reports = [*_shards(tmp_path, n=2), _py_report(tmp_path, name="bad.json", wal_complete=False)]
    assert ratchet.check_fan_in("python_unit", reports, 3, _total_ceilings(tmp_path))[0] == ratchet.EXIT_NOT_COMPARABLE


def test_fan_in_is_for_python_only(tmp_path) -> None:
    assert ratchet.check_fan_in("java_main", _shards(tmp_path), 3, _total_ceilings(tmp_path))[0] == ratchet.EXIT_NOT_COMPARABLE


# ── java: one suite, one run, per fork ───────────────────────────────────────


def test_java_reads_one_suite_of_one_run(tmp_path) -> None:
    d = tmp_path / "ddl-counts"
    _java_fork(d, "ddl-main-r1-1.json")
    _java_fork(d, "ddl-integration-r1-2.json", suite="integration", mig_ddl=999_999)
    code, message = ratchet.check("java_main", d, _jv_ceilings(tmp_path))
    assert code == ratchet.EXIT_OK, message
    code, message = ratchet.check("java_integration", d, _jv_ceilings(tmp_path, "java_integration"))
    assert code == ratchet.EXIT_OVER, "the integration suite is judged on its own files"
    assert "migration_ddl_per_fork" in message


def test_java_refuses_to_guess_between_runs_of_one_suite(tmp_path) -> None:
    d = tmp_path / "ddl-counts"
    _java_fork(d, "ddl-main-r1-1.json", run="r1")
    _java_fork(d, "ddl-main-r2-1.json", run="r2")
    code, message = ratchet.check("java_main", d, _jv_ceilings(tmp_path))
    assert code == ratchet.EXIT_NOT_COMPARABLE
    assert "2 runs" in message and "--run" in message
    assert ratchet.check("java_main", d, _jv_ceilings(tmp_path), run="r2")[0] == ratchet.EXIT_OK
    assert ratchet.check("java_main", d, _jv_ceilings(tmp_path), run="nope")[0] == ratchet.EXIT_NOT_COMPARABLE


def test_java_is_judged_per_fork_so_the_fork_count_does_not_matter(tmp_path) -> None:
    # One fork on a github-hosted runner, four on the pool: the same migration per fork and the same
    # total of everything else. The old basis (fork count) refused the comparison.
    one, four = tmp_path / "one", tmp_path / "four"
    _java_fork(one, "ddl-main-r1-1.json", tests_ddl=2000, empty_ddl=1000, dedicated_ddl=1500,
               tests_bytes=200_000, dedicated_bytes=550_000)
    for i in range(4):
        _java_fork(four, f"ddl-main-r1-{i}.json", tests_ddl=500, empty_ddl=250, dedicated_ddl=375,
                   tests_bytes=50_000, dedicated_bytes=137_500)
    ceilings = _jv_ceilings(tmp_path)
    assert ratchet.check("java_main", one, ceilings)[0] == ratchet.EXIT_OK
    code, message = ratchet.check("java_main", four, ceilings)
    assert code == ratchet.EXIT_OK, message


def test_the_shared_empty_bucket_counts_toward_the_java_other_total(tmp_path) -> None:
    # nexus-7xfh7: migration-process classes walk the changelog in an empty database on the shared cluster;
    # leaving that bucket out would hide the cost the dedicated bucket used to carry.
    d = tmp_path / "ddl-counts"
    _java_fork(d, "ddl-main-r1-1.json", empty_ddl=1200)
    code, message = ratchet.check("java_main", d, _jv_ceilings(tmp_path))
    assert code == ratchet.EXIT_OVER
    assert "other_ddl 5700" in message
    assert ratchet.check("java_main", d, _jv_ceilings(tmp_path, other_ddl=5700))[0] == ratchet.EXIT_OK


@pytest.mark.parametrize(
    ("case", "build"),
    [
        ("no directory", lambda d: None),
        ("empty directory", lambda d: d.mkdir()),
        ("a log that could not be read", lambda d: _java_fork(d, "ddl-main-r1-1.json", errors=1)),
        ("a template whose ready mark never logged", lambda d: _java_fork(d, "ddl-main-r1-1.json", ready=False)),
        ("forks that started no container", lambda d: _java_fork(d, "ddl-main-r1-1.json", shared=0, dedicated=0)),
        ("a container never read", lambda d: _java_fork(d, "ddl-main-r1-1.json", unread=1)),
        ("a container without the WAL marks", lambda d: _java_fork(d, "ddl-main-r1-1.json", incomplete=1)),
        ("only dedicated containers", lambda d: _java_fork(d, "ddl-main-r1-1.json", shared=0)),
        ("only another suite", lambda d: _java_fork(d, "ddl-integration-r1-1.json", suite="integration")),
    ],
)
def test_a_java_measurement_that_proves_nothing_is_not_a_pass(tmp_path, case, build) -> None:
    d = tmp_path / "ddl-counts"
    build(d)
    code, _ = ratchet.check("java_main", d, _jv_ceilings(tmp_path, **dict.fromkeys(JV, 10**9)))
    assert code == ratchet.EXIT_NOT_COMPARABLE, case


# ── the command line ─────────────────────────────────────────────────────────


def test_the_cli_exit_status_is_the_check_status(tmp_path, capsys) -> None:
    ceilings = _py_ceilings(tmp_path, ddl_per_test=1)
    status = ratchet.main(["check", "--suite", "python_unit", "--report", str(_py_report(tmp_path)), "--ceilings", str(ceilings)])
    assert status == ratchet.EXIT_OVER
    assert "ABOVE" in capsys.readouterr().out


def test_the_cli_fan_in(tmp_path, capsys) -> None:
    reports = [str(p) for p in _shards(tmp_path)]
    status = ratchet.main(["fan-in", "--suite", "python_unit", "--report", *reports, "--shards", "3",
                           "--ceilings", str(_total_ceilings(tmp_path, wal_bytes=1_800_000))])
    assert status == ratchet.EXIT_OK
    assert "python_unit fan-in" in capsys.readouterr().out


def test_a_java_check_reports_the_uncounted_cluster_start_sites_next_to_its_ceilings(tmp_path) -> None:
    d = tmp_path / "ddl-counts"
    _java_fork(d, "ddl-main-r1-1.json")
    (tmp_path / "ddl_uncounted_clusters.toml").write_text('[counted]\nsites = []\n[uncounted]\n"tests/a.py::b::pg_ctl_start" = 2\n"service/pom.xml::gen" = 1\n')
    code, message = ratchet.check("java_main", d, _jv_ceilings(tmp_path))
    assert code == ratchet.EXIT_OK, message
    assert "1 uncounted cluster start sites (static tree census" in message
