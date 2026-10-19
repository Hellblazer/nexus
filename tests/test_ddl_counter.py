# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sz651: the DDL-per-run counter (``tests/_ddl_counter.py``).

The parser runs over a fake pg.log (``tests/fixtures/ddl/golden-pg-log.txt``, which the Java
parser's test reads too, so the two cannot count differently); the plugin runs in a
nested pytester session whose conftest stands a fake substrate (just a pgdata dir
holding that log) in for the real one.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import time
from pathlib import Path

import pytest

from tests import _ddl_counter as ddl

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_LOG = REPO_ROOT / "tests" / "fixtures" / "ddl" / "golden-pg-log.txt"
GOLDEN_JSON = REPO_ROOT / "tests" / "fixtures" / "ddl" / "golden.json"
JAVA_GOLDEN_DIR = REPO_ROOT / "service" / "src" / "test" / "resources" / "ddl"


def _epoch(seconds_in_minute: float) -> float:
    return calendar.timegm(time.strptime("2026-10-19 05:00:00", "%Y-%m-%d %H:%M:%S")) + seconds_in_minute


# ── the parser ───────────────────────────────────────────────────────────────


def test_the_golden_log_splits_boot_and_migration_from_tests_and_measures_the_wal() -> None:
    parsed = ddl.parse_log(GOLDEN_LOG.read_text())
    expected = json.loads(GOLDEN_JSON.read_text())
    assert ddl.tally(e for e in parsed.entries if e.boot) == expected["boot"]
    assert ddl.tally(e for e in parsed.entries if e.before_mark and not e.boot) == expected["migration"]
    assert ddl.tally(e for e in parsed.entries if not e.before_mark) == expected["tests"]
    assert parsed.marks == ("start", "ready", "end")
    assert ddl.wal_split(parsed.stats) == expected["wal"]


def test_a_phase_whose_marks_are_not_both_in_the_log_is_none_not_zero() -> None:
    log = (
        "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  nx_ddl_mark: start wal=100 dbsize=5\n"
        "2026-10-19 05:00:00.200 UTC [7] db=x LOG:  nx_ddl_mark: ready wal=250 dbsize=-1\n"
    )
    assert ddl.wal_split(ddl.parse_log(log).stats) == {
        "migration_bytes": 150, "tests_bytes": None, "total_bytes": None, "dbsize_growth_bytes": None,
    }
    assert ddl.wal_split(ddl.parse_log("").stats) == dict.fromkeys(
        ("migration_bytes", "tests_bytes", "total_bytes", "dbsize_growth_bytes"),
    )


def test_an_old_format_mark_with_no_figures_still_splits_the_ddl() -> None:
    log = (
        "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  nx_ddl: CREATE TABLE\n"
        "2026-10-19 05:00:00.200 UTC [7] db=x LOG:  nx_ddl_mark: ready\n"
        "2026-10-19 05:00:00.300 UTC [7] db=x LOG:  nx_ddl: CREATE TABLE\n"
    )
    parsed = ddl.parse_log(log)
    assert [e.before_mark for e in parsed.entries] == [True, False]
    assert parsed.stats[0].wal is None


@pytest.mark.parametrize(
    "sql",
    [
        "GRANT svc TO bob", "REVOKE ADMIN OPTION FOR svc FROM bob", "GRANT ra, rb TO pgu WITH ADMIN OPTION",
        "GRANT CONNECT ON DATABASE d TO r", "REVOKE CONNECT ON DATABASE d FROM r",
        "GRANT SET ON PARAMETER work_mem TO r", "GRANT CREATE ON TABLESPACE pg_default TO r",
        "COMMENT ON DATABASE d IS 'x'", "COMMENT ON ROLE r IS 'x'", "CLUSTER t USING i",
        "REASSIGN OWNED BY a TO b", "ALTER ROLE r SET work_mem='5MB'", "DROP ROLE r", "ALTER SYSTEM SET work_mem='6MB'",
        "CREATE USER bob", "ALTER USER bob SET work_mem='1MB'", "DROP USER bob",
    ],
)
def test_statements_that_fire_no_event_hold_no_claim_over_a_later_nested_event(sql: str) -> None:
    log = (
        f"2026-10-19 05:00:00.100 UTC [7] db=x LOG:  statement: {sql}\n"
        "2026-10-19 05:00:00.200 UTC [7] db=x LOG:  nx_ddl: CREATE TABLE\n"
    )
    assert [e.nested for e in ddl.parse_log(log).entries] == [False, True]


@pytest.mark.parametrize(
    "sql",
    [
        "GRANT SELECT ON t TO r", "REVOKE SELECT ON t FROM r", "GRANT USAGE ON SCHEMA public TO r",
        "GRANT ALL ON ALL TABLES IN SCHEMA public TO r", "COMMENT ON TABLE t IS 'x'",
        "ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO r", "DROP OWNED BY r", "CREATE TABLE a (id int)",
        # nexus-mvr3t: per-database, so it fires the event; it starts with USER but is not the shared-object USER
        "CREATE USER MAPPING FOR bob SERVER s", "ALTER USER MAPPING FOR bob SERVER s OPTIONS (SET user 'x')",
        "DROP USER MAPPING IF EXISTS FOR bob SERVER s", "create   user   mapping for bob server s",
    ],
)
def test_statements_that_do_fire_an_event_are_still_one_command(sql: str) -> None:
    log = (
        f"2026-10-19 05:00:00.100 UTC [7] db=x LOG:  statement: {sql}\n"
        "2026-10-19 05:00:00.101 UTC [7] db=x LOG:  nx_ddl: GRANT\n"
    )
    assert len(ddl.parse_log(log).entries) == 1


def test_reindex_is_never_logged_so_its_event_is_one_nested_command() -> None:
    (entry,) = ddl.parse_log("2026-10-19 05:00:00.100 UTC [7] db=x LOG:  nx_ddl: REINDEX\n").entries
    assert (entry.nested, entry.tag, entry.kind) == (True, "REINDEX", "other")


def test_tags_name_what_the_other_kind_is_made_of() -> None:
    entries = [e for e in ddl.parse_log(GOLDEN_LOG.read_text()).entries if not e.before_mark]
    assert ddl.tag_counts(entries) == {
        "CREATE TABLE": 3, "ALTER TABLE": 2, "CREATE DATABASE": 1, "GRANT": 3, "DROP TABLE": 1, "CREATE INDEX": 3,
        "REINDEX": 1, "COMMENT": 1, "CLUSTER NEXUS": 1, "DROP INDEX": 1,
    }


def test_java_reads_the_same_golden_files() -> None:
    """DdlLogParserTest parses the Java copies; if these drift the two parsers could disagree."""
    for name in ("golden-pg-log.txt", "golden.json"):
        assert (JAVA_GOLDEN_DIR / name).read_bytes() == (REPO_ROOT / "tests" / "fixtures" / "ddl" / name).read_bytes(), name


def test_the_java_init_script_installs_the_same_probe() -> None:
    script = (JAVA_GOLDEN_DIR / "nx-ddl-probe.sql").read_text()
    assert script.count(ddl.PROBE_SQL.strip()) == 2  # template1 and the default database
    # The start mark follows the install, outside the block SharedCluster runs in an empty database.
    assert script.rstrip().endswith("SELECT nx_ddl_probe.mark('start');")
    assert script.index("-- nx-ddl-probe-end") > script.rindex(ddl.PROBE_SQL.strip())


def test_every_wal_mark_reads_the_insert_pointer_not_the_write_pointer() -> None:
    """nexus-sz651: the write pointer lags the insert pointer by megabytes under
    synchronous_commit=off, so a mark at it leaves recent WAL out of its interval."""
    java_counts = (REPO_ROOT / "service" / "src" / "test" / "java" / "dev" / "nexus" / "service" / "DdlCounts.java").read_text()
    script = (JAVA_GOLDEN_DIR / "nx-ddl-probe.sql").read_text()
    assert "pg_current_wal_insert_lsn()" in ddl.PROBE_SQL
    assert "pg_current_wal_lsn()" not in ddl.PROBE_SQL
    assert script.count("pg_current_wal_insert_lsn()") == 2  # template1 and the default database
    assert "pg_current_wal_lsn()" not in script
    assert '"pg_current_wal_insert_lsn"' in java_counts and '"pg_current_wal_lsn"' not in java_counts


def test_the_wal_figure_is_documented_as_a_lower_bound_proxy_with_an_unmeasured_list() -> None:
    """nexus-sz651: WAL is not what a disk-backed cluster would write, and the NVMe writers it cannot
    see are named wherever the figure is documented."""
    docs = {
        "tests/_ddl_counter.py": (REPO_ROOT / "tests" / "_ddl_counter.py").read_text(),
        "tests/AGENTS.md": (REPO_ROOT / "tests" / "AGENTS.md").read_text(),
        "scripts/ddl_ratchet.py": (REPO_ROOT / "scripts" / "ddl_ratchet.py").read_text(),
        "tests/ddl_ceiling.toml": (REPO_ROOT / "tests" / "ddl_ceiling.toml").read_text(),
    }
    for name, text in docs.items():
        flat = " ".join(text.replace("#", " ").split())
        assert "lower-bound" in flat.lower() or "lower bound" in flat.lower(), name
        assert "NVMe sampler" in flat and "source of truth" in flat, name
        for unmeasured in ("journald", "jar builds", ".venv"):
            assert unmeasured in flat, (name, unmeasured)
        stripped = flat.replace("not what a disk-backed cluster would write", "")
        assert "disk-backed cluster would write" not in stripped, name  # only ever as a negation


def test_a_log_that_never_reached_ready_is_all_migration() -> None:
    log = "\n".join(line for line in GOLDEN_LOG.read_text().splitlines() if "nx_ddl_mark" not in line)
    parsed = ddl.parse_log(log)
    assert all(e.before_mark and not e.boot for e in parsed.entries)  # no start mark: no boot phase
    assert len(parsed.entries) == 29


def test_a_nested_command_with_no_client_statement_is_counted_once() -> None:
    log = "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  nx_ddl: CREATE TABLE\n"
    (entry,) = ddl.parse_log(log).entries
    assert (entry.nested, entry.kind) == (True, "create_table")


def test_a_client_statement_and_its_event_are_one_command() -> None:
    log = (
        "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  statement: DROP TABLE a\n"
        "2026-10-19 05:00:00.101 UTC [7] db=x LOG:  nx_ddl: DROP TABLE\n"
    )
    (entry,) = ddl.parse_log(log).entries
    assert (entry.nested, entry.kind) == (False, "drop")


def test_events_from_another_backend_do_not_pair_with_a_statement() -> None:
    log = (
        "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  statement: DROP TABLE a\n"
        "2026-10-19 05:00:00.101 UTC [8] db=x LOG:  nx_ddl: DROP TABLE\n"
        "2026-10-19 05:00:00.102 UTC [7] db=x LOG:  nx_ddl: DROP TABLE\n"
    )
    parsed = ddl.parse_log(log)
    assert [e.nested for e in parsed.entries] == [False, True]


def test_shared_object_ddl_holds_no_claim_over_a_later_nested_event() -> None:
    log = (
        "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  statement: CREATE ROLE r\n"
        "2026-10-19 05:00:00.200 UTC [7] db=x LOG:  nx_ddl: CREATE TABLE\n"
    )
    assert ddl.tally(ddl.parse_log(log).entries)["total"] == 2


def test_a_failed_statement_is_counted_and_releases_its_claim() -> None:
    log = (
        "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  statement: ALTER TABLE a ADD COLUMN c int\n"
        "2026-10-19 05:00:00.101 UTC [7] db=x ERROR:  relation \"a\" does not exist\n"
        "2026-10-19 05:00:00.200 UTC [7] db=x LOG:  nx_ddl: CREATE TABLE\n"
    )
    assert [e.nested for e in ddl.parse_log(log).entries] == [False, True]


def test_a_non_utc_timestamp_still_counts_but_cannot_be_attributed() -> None:
    log = "2026-10-18 22:00:00.100 PDT [7] db=x LOG:  nx_ddl: CREATE TABLE\n"
    (entry,) = ddl.parse_log(log).entries
    assert entry.ts is None


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("CREATE TABLE", "create_table"),
        ("create table if not exists a (id int)", "create_table"),
        ("CREATE UNLOGGED TABLE a (id int)", "create_table"),
        ("CREATE TEMP TABLE a (id int)", "create_table"),
        ("CREATE TABLE AS", "create_table"),
        ("CREATE INDEX", "create_index"),
        ("CREATE UNIQUE INDEX CONCURRENTLY i ON t(a)", "create_index"),
        ("DROP TABLE", "drop"),
        ("-- why\nDROP INDEX i", "drop"),
        ("/* c */ ALTER TABLE a ADD COLUMN b int", "alter"),
        ("CREATE OR REPLACE FUNCTION f() RETURNS int AS $$ SELECT 1 $$", "other"),
        ("CREATE SCHEMA nexus", "other"),
        ("GRANT", "other"),
        ("COMMENT", "other"),
        ("", "other"),
    ],
)
def test_kind_of_a_command(text: str, kind: str) -> None:
    assert ddl.classify(text) == kind


def test_the_mark_label_is_validated_before_it_reaches_sql() -> None:
    assert ddl.mark_sql("ready") == "SELECT nx_ddl_probe.mark('ready');"
    with pytest.raises(ValueError):
        ddl.mark_sql("x'; DROP TABLE y; --")


# ── per-test attribution and merging ─────────────────────────────────────────


def test_ddl_goes_to_the_test_whose_window_holds_its_timestamp() -> None:
    entries = [e for e in ddl.parse_log(GOLDEN_LOG.read_text()).entries if not e.before_mark]
    windows = [
        (_epoch(2.29), _epoch(2.45), "tests/b.py::test_b"),
        (_epoch(2.0), _epoch(2.25), "tests/a.py::test_a"),
    ]
    got = ddl.attribute(entries, windows)
    assert got["top"] == [["tests/a.py::test_a", 3], ["tests/b.py::test_b", 2]]
    assert got["unattributed"] == 12  # the 3 that were always outside both windows, plus the 9 at 05:00:02.8 on
    assert got["tests_with_ddl"] == 2
    assert got["histogram"] == {"2": 1, "3": 1}
    assert got["max"] == 3


def test_two_workers_merge_into_one_report() -> None:
    log = GOLDEN_LOG.read_text()
    a = ddl.worker_report(log, [(_epoch(2.0), _epoch(2.25), "a::t")], pid=1, tests_run=10)
    b = ddl.worker_report(log, [(_epoch(2.29), _epoch(2.45), "b::t")], pid=2, tests_run=30)
    idle = ddl.worker_report(None, [], pid=3, tests_run=5)
    merged = ddl.merge_reports([a, b, idle])
    assert merged["buckets"]["boot"]["total"] == 6
    assert merged["buckets"]["migration"]["total"] == 18
    assert merged["buckets"]["tests"]["total"] == 34
    assert merged["total"]["total"] == 52  # boot (the probe's own install) is not in it
    assert merged["total"]["by_kind"]["create_table"] == 10
    assert (merged["workers"], merged["workers_booted"], merged["tests_run"]) == (3, 2, 45)
    assert merged["workers_ready_mark_seen"] == 2
    assert merged["wal"] == {
        "migration_bytes": 2000, "tests_bytes": 9000, "total_bytes": 11000, "dbsize_growth_bytes": 6800,
        "workers_complete": 2,
    }
    assert [row[0] for row in merged["per_test"]["top"]] == ["a::t", "b::t"]
    assert ddl.summary_line(merged) == (
        "ddl: migration=18 tests=34 per_test=0.76 wal: migration=2.0KB tests=8.8KB per_test=200B"
    )
    assert ddl.summary_line(merged, uncounted=3).endswith(" | 3 uncounted cluster start sites (static tree census)")


def test_one_worker_with_an_unmeasured_phase_voids_the_wal_total() -> None:
    log = GOLDEN_LOG.read_text()
    no_end = "\n".join(line for line in log.splitlines() if "nx_ddl_mark: end" not in line)
    a = ddl.worker_report(log, [], pid=1, tests_run=1)
    b = ddl.worker_report(no_end, [], pid=2, tests_run=1)
    merged = ddl.merge_reports([a, b])
    assert merged["wal"]["tests_bytes"] is None
    assert merged["wal"]["migration_bytes"] == 2000
    assert merged["wal"]["workers_complete"] == 1
    assert "wal: migration=2.0KB tests=n/a per_test=n/a" in ddl.summary_line(merged)


def test_bytes_are_printed_in_human_sizes() -> None:
    assert [ddl.fmt_bytes(n) for n in (None, 0, 1023, 1024, 1536, 5 * 1024**2, 3 * 1024**3, 5000 * 1024**3)] == [
        "n/a", "0B", "1023B", "1.0KB", "1.5KB", "5.0MB", "3.0GB", "5000.0GB",
    ]


def test_the_cgroup_io_reader_sums_wbytes_over_devices(tmp_path: Path) -> None:
    root = tmp_path / "cg"
    (root / "job.slice").mkdir(parents=True)
    (root / "job.slice" / "io.stat").write_text(
        "259:0 rbytes=10 wbytes=4096 rios=1 wios=2 dbytes=0 dios=0\n8:16 rbytes=0 wbytes=1000 rios=0 wios=1\n"
    )
    proc = tmp_path / "cgroup"
    proc.write_text("0::/job.slice\n")
    got = ddl.cgroup_io_wbytes(proc, root)
    assert got == {"cgroup": "job.slice", "wbytes": 5096, "per_device": {"259:0": 4096, "8:16": 1000}}
    (root / "job.slice" / "io.stat").write_text("259:0 rbytes=10 wbytes=9096\n8:16 wbytes=1000\n")
    after = ddl.cgroup_io_wbytes(proc, root)
    assert ddl.cgroup_io_delta(got, after) == {
        "cgroup": "job.slice", "wbytes_delta": 5000, "per_device_delta": {"259:0": 5000, "8:16": 0},
    }


def test_the_cgroup_io_reader_says_none_where_it_cannot_read(tmp_path: Path) -> None:
    assert ddl.cgroup_io_wbytes(tmp_path / "missing", tmp_path) is None
    proc = tmp_path / "cgroup"
    proc.write_text("12:io:/legacy\n")  # cgroup v1 line, no 0:: entry
    assert ddl.cgroup_io_wbytes(proc, tmp_path) is None
    proc.write_text("0::/no-such-group\n")
    assert ddl.cgroup_io_wbytes(proc, tmp_path) is None
    assert ddl.cgroup_io_delta(None, {"cgroup": "a", "wbytes": 1, "per_device": {}}) is None
    other = {"cgroup": "b", "wbytes": 1, "per_device": {}}
    assert ddl.cgroup_io_delta({"cgroup": "a", "wbytes": 1, "per_device": {}}, other) is None


def test_the_uncounted_cluster_count_is_the_allowlists_sum(tmp_path: Path) -> None:
    allow = tmp_path / "u.toml"
    allow.write_text('[counted]\nsites = ["x::y"]\n[uncounted]\n"a::b" = 2\n"c::d" = 3\n')
    assert ddl.uncounted_cluster_count(allow) == 5
    assert ddl.uncounted_cluster_count(tmp_path / "missing.toml") is None
    assert isinstance(ddl.uncounted_cluster_count(), int)  # the committed file


def test_a_dead_workers_file_is_recovered_from_the_default_layout(tmp_path: Path, monkeypatch) -> None:
    """Without NX_DDL_REPORT_DIR a worker's basetemp is the controller's /popen-gwN, so its file lands
    there, not in the controller's directory. CI exports NX_DDL_REPORT_DIR for the whole shard (nexus-ekp7w),
    which would send this test to a directory it did not fill, so it unsets the variable."""
    monkeypatch.delenv(ddl.REPORT_DIR_ENV, raising=False)
    class _Factory:
        def getbasetemp(self) -> Path:
            return tmp_path

    class _Config:
        _tmp_path_factory = _Factory()

    rec = ddl._Recorder(_Config())  # type: ignore[arg-type]
    for directory, pid in ((tmp_path / "popen-gw0", 11), (tmp_path / "popen-gw1", 12), (tmp_path, 13)):
        directory.mkdir(exist_ok=True)
        (directory / ddl.WORKER_FILE.format(run=rec.run_id, pid=pid)).write_text(
            json.dumps(ddl.worker_report(GOLDEN_LOG.read_text(), [], pid=pid, tests_run=1))
        )
    (tmp_path / "popen-gw1" / ddl.WORKER_FILE.format(run="someone-else", pid=14)).write_text("{}")
    rec.reported.append({"pid": 13})  # handed over, so not recovered
    assert sorted(r["pid"] for r in rec.recovered()) == [11, 12]


# ── the plugin, in a nested session ──────────────────────────────────────────

_FAKE_SUBSTRATE_CONFTEST = """
import os, pathlib
from tests import _ddl_counter

# The real substrate's pg.log is replaced by a fixture log; nothing boots.
_ddl_counter.substrate_log_text = lambda: pathlib.Path(os.environ["NX_TEST_FAKE_PG_LOG"]).read_text()
"""


def _fake_substrate_run(pytester, monkeypatch, *, tmp_path: Path, args: tuple[str, ...], subprocess: bool):
    monkeypatch.setenv("NX_TEST_FAKE_PG_LOG", str(GOLDEN_LOG))
    monkeypatch.setenv("NX_DDL_REPORT_DIR", str(tmp_path / "ddl-report"))
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(REPO_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]),
    )
    if not subprocess:
        monkeypatch.setattr(ddl, "substrate_log_text", lambda: GOLDEN_LOG.read_text())
    pytester.makeconftest(_FAKE_SUBSTRATE_CONFTEST)
    pytester.makepyfile(test_inner="""
import pytest

@pytest.mark.parametrize("i", range(8))
def test_it(i):
    pass
""")
    run = pytester.runpytest_subprocess if subprocess else pytester.runpytest_inprocess
    return run("-p", "tests._ddl_counter", "-p", "no:cacheprovider", "-q", *args)


def test_a_serial_run_prints_and_writes_the_counts(pytester, monkeypatch, tmp_path) -> None:
    result = _fake_substrate_run(pytester, monkeypatch, tmp_path=tmp_path, args=(), subprocess=False)
    result.assert_outcomes(passed=8)
    result.stdout.fnmatch_lines([
        "ddl: migration=9 tests=17 per_test=2.12 wal: migration=1000B tests=4.4KB per_test=562B"
        # a cgroup (Linux, under lab-run) adds " io_wbytes=<n>" before the bar
        "*| * uncounted cluster start sites (static tree census) (report: *nx-ddl-report.json)",
    ])
    report = json.loads((tmp_path / "ddl-report" / ddl.REPORT_NAME).read_text())
    assert report["total"]["total"] == 26
    assert report["tests_run"] == 8
    assert report["buckets"]["migration"]["by_kind"]["drop"] == 2
    assert report["buckets"]["boot"]["total"] == 3
    assert report["wal"]["tests_bytes"] == 4500
    assert report["workers_ready_mark_seen"] == 1
    assert isinstance(report["uncounted_cluster_start_sites"], int)


def test_xdist_workers_merge_into_one_report(pytester, monkeypatch, tmp_path) -> None:
    pytest.importorskip("xdist")
    result = _fake_substrate_run(pytester, monkeypatch, tmp_path=tmp_path, args=("-n", "2"), subprocess=True)
    result.assert_outcomes(passed=8)
    result.stdout.fnmatch_lines([
        "ddl: migration=18 tests=34 per_test=4.25 wal: migration=2.0KB tests=8.8KB per_test=1.1KB"
        # a cgroup (Linux, under lab-run) adds " io_wbytes=<n>" before the bar
        "*| * uncounted cluster start sites (static tree census) (report: *nx-ddl-report.json)",
    ])
    report = json.loads((tmp_path / "ddl-report" / ddl.REPORT_NAME).read_text())
    assert (report["workers"], report["workers_booted"], report["recovered_workers"]) == (2, 2, 0)
    assert report["workers_ready_mark_seen"] == 2
    assert len(list((tmp_path / "ddl-report").glob("nx-ddl-worker-*.json"))) == 2


def test_xdist_workers_write_under_the_controllers_basetemp_by_default(pytester, monkeypatch, tmp_path) -> None:
    """The layout recovery depends on: with no NX_DDL_REPORT_DIR, each worker's file is in <basetemp>/popen-gwN."""
    pytest.importorskip("xdist")
    monkeypatch.setenv("NX_TEST_FAKE_PG_LOG", str(GOLDEN_LOG))
    monkeypatch.delenv("NX_DDL_REPORT_DIR", raising=False)
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(REPO_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]),
    )
    pytester.makeconftest(_FAKE_SUBSTRATE_CONFTEST)
    pytester.makepyfile(test_inner="""
import pytest

@pytest.mark.parametrize("i", range(8))
def test_it(i):
    pass
""")
    base = tmp_path / "bt"
    result = pytester.runpytest_subprocess(
        "-p", "tests._ddl_counter", "-p", "no:cacheprovider", "-q", "-n", "2", f"--basetemp={base}",
    )
    result.assert_outcomes(passed=8)
    assert (base / ddl.REPORT_NAME).is_file()
    files = sorted(p.relative_to(base).parts[0] for p in base.glob("popen-gw*/nx-ddl-worker-*.json"))
    assert files == ["popen-gw0", "popen-gw1"]


def test_a_session_that_never_booted_a_substrate_prints_nothing_but_still_reports_its_tests(
    pytester, monkeypatch, tmp_path,
) -> None:
    """The substrate boots lazily, so a shard of tests that never touch it is legitimate. It prints no line,
    but its report is written (nexus-2rhif): ``ddl_ratchet.py fan-in`` needs the shard to add up the run, and a
    missing file would read as a missing shard."""
    monkeypatch.setenv("NX_DDL_REPORT_DIR", str(tmp_path / "ddl-report"))
    monkeypatch.setattr(ddl, "substrate_log_text", lambda: None)
    pytester.makepyfile(test_inner="def test_it():\n    pass\n")
    result = pytester.runpytest_inprocess("-p", "tests._ddl_counter", "-p", "no:cacheprovider", "-q")
    result.assert_outcomes(passed=1)
    assert not re.search(r"^ddl:", result.stdout.str(), re.MULTILINE)
    report = json.loads((tmp_path / "ddl-report" / ddl.REPORT_NAME).read_text())
    assert (report["workers_booted"], report["tests_run"], report["workers_lost"]) == (0, 1, 0)


# ── the off switch (nexus-3cwsw) ─────────────────────────────────────────────


def test_the_off_switch_defaults_on_and_parses_strictly() -> None:
    assert ddl.enabled({}) is True
    assert ddl.enabled({ddl.COUNT_ENV: "1"}) is True
    assert ddl.enabled({ddl.COUNT_ENV: "0"}) is False
    for typo in ("", "off", "false", "no", "00"):
        with pytest.raises(ValueError, match=ddl.COUNT_ENV):
            ddl.enabled({ddl.COUNT_ENV: typo})  # a typo must not leave the counter silently on


def test_log_conf_is_empty_with_the_counter_off(monkeypatch) -> None:
    monkeypatch.delenv(ddl.COUNT_ENV, raising=False)
    assert ddl.log_conf() == ddl.LOG_CONF != ""
    monkeypatch.setenv(ddl.COUNT_ENV, "0")
    assert ddl.log_conf() == ""


def test_a_session_with_the_counter_off_prints_and_writes_nothing(pytester, monkeypatch, tmp_path) -> None:
    """The positive control is test_a_serial_run_prints_and_writes_the_counts: same fake substrate,
    same plugin, counter on. With NX_DDL_COUNT=0 the plugin is not registered at all."""
    monkeypatch.setenv(ddl.COUNT_ENV, "0")
    result = _fake_substrate_run(pytester, monkeypatch, tmp_path=tmp_path, args=(), subprocess=False)
    result.assert_outcomes(passed=8)
    assert not re.search(r"^ddl:", result.stdout.str(), re.MULTILINE)
    assert not (tmp_path / "ddl-report").exists()
    assert not any(tmp_path.rglob(ddl.REPORT_NAME))


_GATED_NAMES = frozenset({"PROBE_SQL", "mark_sql", "LOG_CONF"})


def _ungated_counter_uses(source: str) -> tuple[list[int], int]:
    """Lines where ``source`` uses a counter piece (the probe SQL, a mark, the raw logging lines) outside
    an ``if ddl_count_enabled():`` body, and how many uses it found in all."""
    import ast  # noqa: PLC0415

    def is_guard(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.If) and isinstance(node.test, ast.Call)
            and isinstance(node.test.func, ast.Name) and node.test.func.id == "ddl_count_enabled"
        )

    ungated: list[int] = []
    total = 0

    def walk(node: ast.AST, guarded: bool) -> None:
        nonlocal total
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return
        if isinstance(node, ast.Name) and node.id in _GATED_NAMES:
            total += 1
            if not guarded:
                ungated.append(node.lineno)
        for field, value in ast.iter_fields(node):
            children = value if isinstance(value, list) else [value]
            for child in children:
                if isinstance(child, ast.AST):
                    walk(child, guarded or (is_guard(node) and field == "body"))

    walk(ast.parse(source), False)
    return ungated, total


def test_the_substrate_uses_the_probe_and_marks_only_behind_the_switch() -> None:
    source = (REPO_ROOT / "tests" / "_engine_substrate.py").read_text()
    ungated, total = _ungated_counter_uses(source)
    assert total == 3, f"expected PROBE_SQL once and mark_sql twice, found {total} uses"  # non-vacuity
    assert ungated == [], f"counter pieces used outside `if ddl_count_enabled():` at lines {ungated}"
    assert "log_conf()" in source and "LOG_CONF" not in source.replace("log_conf", "")  # the conf goes via the gated helper


def test_the_switch_check_sees_an_unguarded_use() -> None:
    """Revert-failing control: take the substrate source, drop the guard from the probe install, and the check reds."""
    source = (REPO_ROOT / "tests" / "_engine_substrate.py").read_text()
    assert source.count("        if ddl_count_enabled():\n            _psql_command(bin_dir, pg_port, pg_user, PROBE_SQL)") == 1
    broken = source.replace("        if ddl_count_enabled():\n            _psql_command(bin_dir, pg_port, pg_user, PROBE_SQL)",
                            "        if True:\n            _psql_command(bin_dir, pg_port, pg_user, PROBE_SQL)")
    ungated, _ = _ungated_counter_uses(broken)
    assert len(ungated) >= 1


class _Node:
    def __init__(self, workeroutput) -> None:
        if workeroutput is not None:
            self.workeroutput = workeroutput


def test_a_node_that_goes_down_without_a_report_is_a_lost_worker(tmp_path: Path) -> None:
    """nexus-mvr3t: a worker killed before sessionfinish writes no file and hands nothing over, so the
    file-based recovery cannot see it. xdist reports the node down all the same."""
    class _Config:
        _tmp_path_factory = type("F", (), {"getbasetemp": lambda self: tmp_path})()

    rec = ddl._Recorder(_Config())  # type: ignore[arg-type]
    rec.pytest_testnodedown(_Node({ddl._WORKEROUTPUT_KEY: {"pid": 1}}), None)
    assert (rec.lost, len(rec.reported)) == (0, 1)
    rec.pytest_testnodedown(_Node({}), None)        # a worker that finished without our payload
    rec.pytest_testnodedown(_Node(None), "crashed")  # no workeroutput at all: it died
    assert (rec.lost, len(rec.reported)) == (2, 1)


def test_a_killed_xdist_worker_is_counted_as_lost_in_the_report(pytester, monkeypatch, tmp_path) -> None:
    pytest.importorskip("xdist")
    monkeypatch.setenv("NX_TEST_FAKE_PG_LOG", str(GOLDEN_LOG))
    monkeypatch.setenv("NX_DDL_REPORT_DIR", str(tmp_path / "ddl-report"))
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(REPO_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]),
    )
    pytester.makeconftest(_FAKE_SUBSTRATE_CONFTEST)
    pytester.makepyfile(test_inner="""
import os, pytest

@pytest.mark.parametrize("i", range(8))
def test_it(i):
    if i == 3:
        os._exit(1)  # the OS killing a worker: no sessionfinish, no report
""")
    result = pytester.runpytest_subprocess("-p", "tests._ddl_counter", "-p", "no:cacheprovider", "-q", "-n", "2")
    result.assert_outcomes(passed=7, failed=1)
    result.stdout.fnmatch_lines(["*LOST 1 worker(s) that died before reporting*"])
    report = json.loads((tmp_path / "ddl-report" / ddl.REPORT_NAME).read_text())
    assert report["workers_lost"] == 1
    assert report["workers_booted"] >= 2  # the survivor and the replacement both reported
