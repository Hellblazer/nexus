# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sz651: ``scripts/ddl_ratchet.py`` fails a run that wrote more bytes (or ran more DDL) than its
committed ceiling, compares only numbers that do not move with how the work was split, and refuses a
measurement that proves nothing.

nexus-2rhif: the python gate is the fan-in over every shard, in numbers that survive a different shard
split, a different ``-n`` and a worker that never booted the substrate; one shard's ``check`` is advisory.
nexus-mvr3t: a measured 0 on a booted cluster, and a worker lost before it reported, are exit 12."""
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
               tests_run=40, wal_migration=200_000, wal_tests=400_000, ready=None, wal_complete=True, lost=0,
               booted=None, io_wbytes=None) -> Path:
    path = tmp_path / name
    booted = workers if booted is None else booted
    io = {} if io_wbytes is None else {"cgroup_io": {"cgroup": "/x", "wbytes": io_wbytes, "wbytes_delta": io_wbytes, "per_device_delta": {}}}
    path.write_text(json.dumps({**io,
        "workers": workers, "workers_booted": booted, "tests_run": tests_run, "workers_lost": lost,
        "workers_ready_mark_seen": booted if ready is None else ready,
        "buckets": {"boot": _tally(6), "migration": _tally(migration_ddl), "tests": _tally(tests_ddl)},
        "total": _tally(migration_ddl + tests_ddl),
        "wal": {
            "migration_bytes": wal_migration if wal_complete else None,
            "tests_bytes": wal_tests if wal_complete else None,
            "total_bytes": wal_migration + wal_tests if wal_complete else None,
            "dbsize_growth_bytes": 1,
            "workers_complete": booted if wal_complete else 0,
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
    assert set(committed) == set(ratchet.SUITES)
    shape = {
        "python_unit": ratchet.PYTHON_METRICS,
        "java_main": ratchet.JAVA_METRICS,
        "java_integration": ratchet.JAVA_METRICS,
    }
    for section, entry in committed.items():
        own = {f"{name}_tolerance" for name in shape[section]}
        marker = {"measured_on"} if section == "python_unit" else set()
        assert {"bytes_tolerance", "ddl_tolerance", *shape[section]} <= set(entry) <= {
            "bytes_tolerance", "ddl_tolerance", *shape[section], *own, *marker}, section
        assert all(isinstance(v, (int, float)) and v >= 0 for k, v in entry.items() if k.endswith("_tolerance")), section
        # "measure" until a full run supplies the number, then a positive number; a Java section may still be
        # "measure" (see the test below), so committing its measured ceilings is not a test change.
        for name in shape[section]:
            value = entry[name]
            assert value == ratchet.MEASURE or (isinstance(value, (int, float)) and value > 0), (section, name, value)


def test_the_committed_header_does_not_overclaim_what_the_ceilings_are() -> None:
    """The review's wording blocker: a set ceiling was called "the WORST value measured" where it was an estimate,
    and WAL was presented as the disk target when it lands on tmpfs and never reaches the NVMe."""
    toml_text = (_REPO / "tests" / "ddl_ceiling.toml").read_text()
    script_text = (_REPO / "scripts" / "ddl_ratchet.py").read_text()
    assert "WORST value measured" not in toml_text
    assert "THE TARGET IS BYTES WRITTEN" not in toml_text and "THE TARGET IS BYTES WRITTEN" not in script_text
    for text in (toml_text, script_text):
        flat = " ".join(text.replace("#", " ").split())
        assert "churn proxy" in flat.lower(), "WAL must be called a churn proxy"
        assert "NVMe" in flat and "tmpfs" in flat
        assert "labrun" in flat
        assert "DDL PER TEST" in flat.upper()


def test_the_committed_python_ceilings_are_unarmed_until_a_linux_measurement_sets_the_marker() -> None:
    py = tomllib.loads((_REPO / "tests" / "ddl_ceiling.toml").read_text())["python_unit"]
    marker = py.get("measured_on")
    assert marker is None or (isinstance(marker, str) and marker.strip()), marker
    # the marker is the one non-metric, non-tolerance key a section may carry
    assert ratchet.measured_on("python_unit", _REPO / "tests" / "ddl_ceiling.toml") == (marker.strip() if marker else None)


def test_the_python_gate_has_real_ceilings_and_a_ceiling_never_sits_on_a_zero() -> None:
    """nexus-ekp7w: the ratchet held no line while every ceiling read "measure" (exit 11 never reaches
    a number). The python section gates CI through the fan-in, so every metric of it is a number. The Java
    sections are advisory in CI until nexus-0ty8z's DDL cut lands; each is either all numbers or all "measure"."""
    committed = tomllib.loads((_REPO / "tests" / "ddl_ceiling.toml").read_text())
    py = committed["python_unit"]
    for name in ratchet.PYTHON_METRICS:
        assert isinstance(py[name], (int, float)) and py[name] > 0, f"python_unit.{name} is {py[name]!r}"
    for section in ("java_main", "java_integration"):
        kinds = {committed[section][name] == ratchet.MEASURE for name in ratchet.JAVA_METRICS}
        assert len(kinds) == 1, f"{section} is half set; set all four from one run"


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


def test_a_metric_can_carry_its_own_tolerance(tmp_path) -> None:
    """The migration's DDL per worker is exactly the changelog's, so it keeps a 0 band beside a ratio that has 5%."""
    # _py_report measures ddl_per_test 75 and migration_ddl_per_worker 1000
    wide = {"ddl_tolerance": 0.05, "migration_ddl_per_worker_tolerance": 0.0}
    path = tmp_path / "c.toml"
    path.write_text("[python_unit]\nbytes_tolerance = 0.05\n" + "".join(f"{k} = {v}\n" for k, v in {**PY, **wide}.items())
                    .replace("ddl_per_test = 75", "ddl_per_test = 72").replace("migration_ddl_per_worker = 1000", "migration_ddl_per_worker = 999"))
    code, message = ratchet.check("python_unit", _py_report(tmp_path), path)
    assert code == ratchet.EXIT_OVER, message  # 75 > 72 * 1.05 is false (75.6), so only the migration (1000 > 999 * 1.0) is over
    assert "migration_ddl_per_worker" in message and "ddl_per_test" not in message.split("ABOVE:")[1]


def test_a_bad_own_tolerance_is_not_comparable(tmp_path) -> None:
    ceilings = _py_ceilings(tmp_path, ddl_per_test_tolerance=-1)
    assert ratchet.check("python_unit", _py_report(tmp_path), ceilings)[0] == ratchet.EXIT_NOT_COMPARABLE


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


# ── fan-in: the gate, on the whole run ───────────────────────────────────────


def _shards(tmp_path, n=3, **kwargs) -> list[Path]:
    """n identical shards, each what `_py_report()` measures: 40 tests, 2 workers, 600k WAL, 5000 DDL."""
    return [_py_report(tmp_path, name=f"shard{i}.json", **kwargs) for i in range(n)]


def _unbooted_shard(tmp_path, name="lazy.json", tests_run=40) -> Path:
    """A shard whose tests never touched the substrate (it boots lazily): tests ran, no worker booted, no cluster."""
    return _py_report(tmp_path, name=name, workers=2, booted=0, tests_run=tests_run, migration_ddl=0, tests_ddl=0,
                      wal_migration=0, wal_tests=0)


def test_fan_in_sums_the_shards_and_judges_the_run_in_numbers_that_do_not_depend_on_its_split(tmp_path) -> None:
    # 3 shards x (40 tests, 2 workers, 600k WAL, 5000 DDL) = 120 tests, 6 workers: the metrics `_py_report` is built for.
    code, message = ratchet.check_fan_in("python_unit", _shards(tmp_path), 3, _py_ceilings(tmp_path))
    assert code == ratchet.EXIT_OK, message
    for shown in ("wal_bytes_per_test=10000", "ddl_per_test=75", "migration_ddl_per_worker=1000", "total_ddl=15000",
                  "total_wal_bytes=1800000", "workers_booted=6", "tests=120", "shards=3"):
        assert shown in message, shown


def test_fan_in_over_a_ceiling_fails(tmp_path) -> None:
    code, message = ratchet.check_fan_in("python_unit", _shards(tmp_path), 3, _py_ceilings(tmp_path, ddl_per_test=74))
    assert code == ratchet.EXIT_OVER, message
    assert "ddl_per_test" in message


def test_the_gate_is_the_whole_run_so_a_shard_that_looks_heavy_alone_does_not_flap_it(tmp_path) -> None:
    """nexus-2rhif: a shard's intensive numbers move with its mix. Shard A is the heavy mix (150 DDL a test),
    shard B the light one (30): alone, A is over the ceiling; together they are 50 a test, under it."""
    heavy = _py_report(tmp_path, name="heavy.json", tests_run=20, tests_ddl=3000)
    light = _py_report(tmp_path, name="light.json", tests_run=100, tests_ddl=3000)
    ceilings = _py_ceilings(tmp_path)
    assert ratchet.check("python_unit", heavy, ceilings)[0] == ratchet.EXIT_OVER
    code, message = ratchet.check_fan_in("python_unit", [heavy, light], 2, ceilings)
    assert code == ratchet.EXIT_OK, message
    # and the same two shards in the other order, or split another way, are the same run
    assert ratchet.check_fan_in("python_unit", [light, heavy], 2, ceilings)[0] == ratchet.EXIT_OK


def test_a_regression_in_one_light_shard_is_caught_by_the_whole_run(tmp_path) -> None:
    """The other side of the same design: no shard is a place to regress freely. Each shard alone is under
    the ceiling; one of them doubles its DDL and the run goes over."""
    ok = _shards(tmp_path, n=2)
    doubled = _py_report(tmp_path, name="doubled.json", tests_ddl=3000 * 3)
    ceilings = _py_ceilings(tmp_path, ddl_per_test=80)
    assert ratchet.check("python_unit", doubled, ceilings)[0] == ratchet.EXIT_OVER
    code, message = ratchet.check_fan_in("python_unit", [*ok, doubled], 3, ceilings)
    assert code == ratchet.EXIT_OVER, message
    assert "ddl_per_test" in message


def test_fan_in_does_not_depend_on_how_many_workers_ran_or_booted(tmp_path) -> None:
    """nexus-2rhif: the pool runs a shard at -n 4, a hosted runner serially, and a worker may never boot.
    Per-worker migration and per-test numbers are the same at every width, so none of it is a refusal."""
    serial = _py_report(tmp_path, name="serial.json", workers=1, migration_ddl=1000, wal_migration=100_000)
    wide = _py_report(tmp_path, name="wide.json", workers=8, tests_run=40, migration_ddl=8000, wal_migration=800_000)
    code, message = ratchet.check_fan_in("python_unit", [serial, wide], 2, _py_ceilings(tmp_path))
    assert code == ratchet.EXIT_OK, message
    assert "workers_booted=9" in message


def test_a_shard_that_booted_no_substrate_is_part_of_the_run_not_a_refusal(tmp_path) -> None:
    """The substrate boots lazily: a shard of tests that never touch it is legitimate. Its tests count toward
    the run (they cost nothing), and the per-shard check on it alone still says it measured nothing."""
    lazy = _unbooted_shard(tmp_path)
    booted = _shards(tmp_path, n=2)
    code, message = ratchet.check_fan_in("python_unit", [*booted, lazy], 3, _py_ceilings(tmp_path))
    assert code == ratchet.EXIT_OK, message
    assert "tests=120" in message and "workers_booted=4" in message
    assert ratchet.check("python_unit", lazy, _py_ceilings(tmp_path))[0] == ratchet.EXIT_NOT_COMPARABLE


def test_a_run_in_which_no_shard_booted_a_substrate_proves_nothing(tmp_path) -> None:
    lazy = [_unbooted_shard(tmp_path, name=f"lazy{i}.json") for i in range(3)]
    code, message = ratchet.check_fan_in("python_unit", lazy, 3, _py_ceilings(tmp_path))
    assert code == ratchet.EXIT_NOT_COMPARABLE
    assert "no worker of any shard booted" in message


def test_fan_in_refuses_a_missing_shard(tmp_path) -> None:
    code, message = ratchet.check_fan_in("python_unit", _shards(tmp_path, n=2), 3, _py_ceilings(tmp_path))
    assert code == ratchet.EXIT_NOT_COMPARABLE
    assert "2 shard report(s) given, 3 expected" in message


def test_fan_in_with_unset_ceilings_prints_the_numbers_to_commit(tmp_path) -> None:
    ceilings = _py_ceilings(tmp_path, **{name: "measure" for name in ratchet.PYTHON_METRICS})
    code, message = ratchet.check_fan_in("python_unit", _shards(tmp_path), 3, ceilings)
    assert code == ratchet.EXIT_UNSET
    for line in ("wal_bytes_per_test = 10000", "ddl_per_test = 75", "migration_wal_bytes_per_worker = 100000",
                 "migration_ddl_per_worker = 1000"):
        assert line in message


def test_fan_in_with_one_unmeasured_shard_is_not_a_total(tmp_path) -> None:
    reports = [*_shards(tmp_path, n=2), _py_report(tmp_path, name="bad.json", wal_complete=False)]
    assert ratchet.check_fan_in("python_unit", reports, 3, _py_ceilings(tmp_path))[0] == ratchet.EXIT_NOT_COMPARABLE


def test_fan_in_is_for_python_only(tmp_path) -> None:
    assert ratchet.check_fan_in("java_main", _shards(tmp_path), 3, _py_ceilings(tmp_path))[0] == ratchet.EXIT_NOT_COMPARABLE


# ── nexus-mvr3t: a run that proves nothing is exit 12 ────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [{"tests_ddl": 0}, {"migration_ddl": 0}, {"wal_tests": 0}, {"wal_migration": 0}],
    ids=["tests ddl 0", "migration ddl 0", "tests wal 0", "migration wal 0"],
)
def test_a_measured_zero_on_a_booted_cluster_is_not_a_pass(tmp_path, kwargs) -> None:
    """A booted cluster migrated a schema, so a 0 is a counter that saw nothing. Before this it read as
    'below the ceiling' and passed."""
    # a ceiling sitting far above a 0 would have called this 'below' and passed
    ceilings = _py_ceilings(tmp_path, **dict.fromkeys(PY, 10**9))
    code, message = ratchet.check("python_unit", _py_report(tmp_path, **kwargs), ceilings)
    assert code == ratchet.EXIT_NOT_COMPARABLE, message
    assert "measured 0" in message
    code, message = ratchet.check_fan_in("python_unit", [_py_report(tmp_path, name="a.json", **kwargs)], 1, ceilings)
    assert code == ratchet.EXIT_NOT_COMPARABLE, message


def test_a_worker_lost_before_it_reported_is_not_a_pass(tmp_path) -> None:
    """A worker the OS killed before sessionfinish leaves no file: its DDL and WAL are in no figure."""
    ceilings = _py_ceilings(tmp_path, **dict.fromkeys(PY, 10**9))
    code, message = ratchet.check("python_unit", _py_report(tmp_path, lost=1), ceilings)
    assert code == ratchet.EXIT_NOT_COMPARABLE
    assert "1 xdist worker(s) ended without handing over a report" in message
    shards = [*_shards(tmp_path, n=2), _py_report(tmp_path, name="lost.json", lost=1)]
    code, message = ratchet.check_fan_in("python_unit", shards, 3, ceilings)
    assert code == ratchet.EXIT_NOT_COMPARABLE
    assert "lost.json" in message


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
        ("a booted cluster that counted no DDL", lambda d: _java_fork(
            d, "ddl-main-r1-1.json", mig_ddl=0, tests_ddl=0, empty_ddl=0, dedicated_ddl=0)),
        ("a booted cluster that logged no WAL", lambda d: _java_fork(
            d, "ddl-main-r1-1.json", tests_bytes=0, dedicated_bytes=0)),
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


def test_advisory_prints_the_verdict_and_exits_zero(tmp_path, capsys) -> None:
    """nexus-2rhif: one shard's verdict is advisory; it must still say what it found, so the log reads it."""
    ceilings = _py_ceilings(tmp_path, ddl_per_test=1)
    report = str(_py_report(tmp_path))
    status = ratchet.main(["check", "--suite", "python_unit", "--report", report, "--ceilings", str(ceilings), "--advisory"])
    out = capsys.readouterr().out
    assert status == ratchet.EXIT_OK
    assert "ABOVE" in out and "ADVISORY: this would exit 10" in out
    # a passing run says nothing extra, and the flag does not turn a bad report into a clean one's output
    ok = ratchet.main(["check", "--suite", "python_unit", "--report", report, "--ceilings", str(_py_ceilings(tmp_path)), "--advisory"])
    assert ok == ratchet.EXIT_OK and "ADVISORY" not in capsys.readouterr().out


@pytest.mark.parametrize("shape", ["[]", "null", '"text"', '{"containers": [1]}'])
@pytest.mark.parametrize("suite", ["python_unit", "java_main"])
def test_advisory_exits_zero_on_a_report_of_the_wrong_shape(tmp_path, capsys, shape, suite) -> None:
    """A valid JSON report of the wrong shape must not crash an advisory step into exit 1 (nexus-ekp7w)."""
    if suite == "python_unit":
        report = tmp_path / "nx-ddl-report.json"
        report.write_text(shape)
    else:
        report = tmp_path / "ddl-counts"
        report.mkdir()
        (report / "ddl-main-r1-1.json").write_text(shape)
    args = ["check", "--suite", suite, "--report", str(report), "--ceilings", str(_py_ceilings(tmp_path)), "--advisory"]
    assert ratchet.main(args) == ratchet.EXIT_OK
    assert "ADVISORY" in capsys.readouterr().out


@pytest.mark.parametrize("shape", ["[]", "null", '"text"', '{"containers": [1]}', "{not json"])
def test_fan_in_advisory_exits_zero_on_a_report_of_the_wrong_shape(tmp_path, capsys, shape) -> None:
    """The pytest-gate fan-in runs --advisory too: a wrong-shape or malformed shard report must not red it (nexus-ekp7w)."""
    report = tmp_path / "nx-ddl-report.json"
    report.write_text(shape)
    args = ["fan-in", "--suite", "python_unit", "--report", str(report), "--shards", "1",
            "--ceilings", str(_py_ceilings(tmp_path)), "--advisory"]
    assert ratchet.main(args) == ratchet.EXIT_OK
    assert "ADVISORY" in capsys.readouterr().out


def test_advisory_covers_a_ceiling_still_set_to_measure_and_a_report_that_proves_nothing(tmp_path, capsys) -> None:
    """The Java steps run --advisory while their ceilings are 'measure' (exit 11) and the run must not be red for it."""
    d = tmp_path / "ddl-counts"
    _java_fork(d, "ddl-main-r1-1.json")
    unset = _jv_ceilings(tmp_path, **dict.fromkeys(JV, "measure"))
    args = ["check", "--suite", "java_main", "--report", str(d), "--run", "r1", "--ceilings", str(unset)]
    assert ratchet.main(args) == ratchet.EXIT_UNSET
    capsys.readouterr()
    assert ratchet.main([*args, "--advisory"]) == ratchet.EXIT_OK
    out = capsys.readouterr().out
    assert "migration_ddl_per_fork = 2000" in out and "ADVISORY: this would exit 11" in out
    assert ratchet.main(["check", "--suite", "java_main", "--report", str(tmp_path / "nowhere"), "--ceilings", str(unset), "--advisory"]) == ratchet.EXIT_OK


def test_the_cli_fan_in(tmp_path, capsys) -> None:
    reports = [str(p) for p in _shards(tmp_path)]
    status = ratchet.main(["fan-in", "--suite", "python_unit", "--report", *reports, "--shards", "3",
                           "--ceilings", str(_py_ceilings(tmp_path))])
    assert status == ratchet.EXIT_OK
    assert "python_unit fan-in" in capsys.readouterr().out


def test_fan_in_advisory_prints_the_whole_comparison_and_exits_zero(tmp_path, capsys) -> None:
    """The python ceilings are not measured on CI yet, so the fan-in must not be able to turn develop red, and
    must still print everything the first Linux run needs to set them."""
    reports = [str(p) for p in _shards(tmp_path)]
    over = _py_ceilings(tmp_path, ddl_per_test=1)
    base = ["fan-in", "--suite", "python_unit", "--report", *reports, "--shards", "3", "--ceilings", str(over)]
    assert ratchet.main(base) == ratchet.EXIT_OVER
    capsys.readouterr()
    assert ratchet.main([*base, "--advisory"]) == ratchet.EXIT_OK
    out = capsys.readouterr().out
    for shown in ("ddl_per_test=75", "wal_bytes_per_test=10000", "migration_ddl_per_worker=1000", "total_ddl=15000", "shards=3",
                  "ABOVE", "ADVISORY: this would exit 10", "measured_on", "nexus-ekp7w"):
        assert shown in out, shown
    # a missing shard is exit 12 enforced and still exit 0 advisory: unmeasured is never a red on develop
    short = ["fan-in", "--suite", "python_unit", "--report", *reports[:2], "--shards", "3", "--ceilings", str(_py_ceilings(tmp_path))]
    assert ratchet.main(short) == ratchet.EXIT_NOT_COMPARABLE
    capsys.readouterr()
    assert ratchet.main([*short, "--advisory"]) == ratchet.EXIT_OK
    assert "ADVISORY: this would exit 12" in capsys.readouterr().out


def test_fan_in_advisory_stops_telling_you_to_arm_once_the_marker_is_set(tmp_path, capsys) -> None:
    armed = _py_ceilings(tmp_path)
    armed.write_text(armed.read_text().replace("[python_unit]\n", '[python_unit]\nmeasured_on = "https://example.invalid/run/1"\n'))
    reports = [str(p) for p in _shards(tmp_path)]
    status = ratchet.main(["fan-in", "--suite", "python_unit", "--report", *reports, "--shards", "3", "--ceilings", str(armed), "--advisory"])
    assert status == ratchet.EXIT_OK
    assert "measured_on is absent" not in capsys.readouterr().out
    assert ratchet.measured_on("python_unit", armed) == "https://example.invalid/run/1"
    for blank in ('measured_on = ""\n', "measured_on = 7\n"):
        armed.write_text(f"[python_unit]\n{blank}")
        assert ratchet.measured_on("python_unit", armed) is None, blank


def test_fan_in_sums_and_prints_the_cgroup_device_writes_but_never_gates_on_them(tmp_path) -> None:
    shards = [_py_report(tmp_path, name=f"s{i}.json", io_wbytes=1_000_000 * (i + 1)) for i in range(2)]
    shards.append(_py_report(tmp_path, name="s2.json"))  # a shard that could not read io.stat
    code, message = ratchet.check_fan_in("python_unit", shards, 3, _py_ceilings(tmp_path))
    assert code == ratchet.EXIT_OK, message
    assert "cgroup_io_wbytes=3000000 summed over 2 shard(s)" in message and "never gated" in message
    # no ceiling reads it: an enormous device figure still passes, and the metrics stay the four
    huge = [_py_report(tmp_path, name=f"h{i}.json", io_wbytes=10**15) for i in range(3)]
    assert ratchet.check_fan_in("python_unit", huge, 3, _py_ceilings(tmp_path))[0] == ratchet.EXIT_OK
    assert set(ratchet.PYTHON_METRICS) == {"wal_bytes_per_test", "ddl_per_test", "migration_wal_bytes_per_worker", "migration_ddl_per_worker"}
    # none readable: said, not omitted
    _, none_message = ratchet.check_fan_in("python_unit", _shards(tmp_path), 3, _py_ceilings(tmp_path))
    assert "cgroup_io_wbytes=unavailable" in none_message


def test_a_migration_ddl_failure_names_the_changeset_procedure(tmp_path) -> None:
    """migration_ddl_per_worker has tolerance 0: once armed, a commit that adds a Liquibase changeset reds the gate
    unless it bumps the ceiling in the same commit, and the failure says so."""
    heavy = _shards(tmp_path, migration_ddl=2001)
    code, message = ratchet.check_fan_in("python_unit", heavy, 3, _py_ceilings(tmp_path))
    assert code == ratchet.EXIT_OVER and "migration_ddl_per_worker" in message
    assert "Liquibase changeset" in message and "same commit" in message
    # an unrelated overage does not nag about changesets
    code, message = ratchet.check_fan_in("python_unit", _shards(tmp_path), 3, _py_ceilings(tmp_path, ddl_per_test=74))
    assert code == ratchet.EXIT_OVER and "Liquibase" not in message


def test_a_java_check_reports_the_uncounted_cluster_start_sites_next_to_its_ceilings(tmp_path) -> None:
    d = tmp_path / "ddl-counts"
    _java_fork(d, "ddl-main-r1-1.json")
    (tmp_path / "ddl_uncounted_clusters.toml").write_text('[counted]\nsites = []\n[uncounted]\n"tests/a.py::b::pg_ctl_start" = 2\n"service/pom.xml::gen" = 1\n')
    code, message = ratchet.check("java_main", d, _jv_ceilings(tmp_path))
    assert code == ratchet.EXIT_OK, message
    assert "1 uncounted cluster start sites (static tree census" in message
