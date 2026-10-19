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


def test_the_golden_log_splits_migration_from_tests() -> None:
    parsed = ddl.parse_log(GOLDEN_LOG.read_text())
    expected = json.loads(GOLDEN_JSON.read_text())
    assert ddl.tally(e for e in parsed.entries if e.before_mark) == expected["migration"]
    assert ddl.tally(e for e in parsed.entries if not e.before_mark) == expected["tests"]
    assert parsed.marks == ("ready",)


def test_tags_name_what_the_other_kind_is_made_of() -> None:
    entries = [e for e in ddl.parse_log(GOLDEN_LOG.read_text()).entries if not e.before_mark]
    assert ddl.tag_counts(entries) == {
        "CREATE TABLE": 2, "ALTER TABLE": 1, "CREATE DATABASE": 1, "GRANT": 1, "DROP TABLE": 1, "CREATE INDEX": 2,
    }


def test_java_reads_the_same_golden_files() -> None:
    """DdlLogParserTest parses the Java copies; if these drift the two parsers could disagree."""
    for name in ("golden-pg-log.txt", "golden.json"):
        assert (JAVA_GOLDEN_DIR / name).read_bytes() == (REPO_ROOT / "tests" / "fixtures" / "ddl" / name).read_bytes(), name


def test_the_java_init_script_installs_the_same_probe() -> None:
    script = (JAVA_GOLDEN_DIR / "nx-ddl-probe.sql").read_text()
    assert script.count(ddl.PROBE_SQL.strip()) == 2  # template1 and the default database


def test_a_log_that_never_reached_ready_is_all_migration() -> None:
    log = "\n".join(line for line in GOLDEN_LOG.read_text().splitlines() if "nx_ddl_mark" not in line)
    parsed = ddl.parse_log(log)
    assert all(e.before_mark for e in parsed.entries)
    assert len(parsed.entries) == 17


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
    assert got["unattributed"] == 3
    assert got["tests_with_ddl"] == 2
    assert got["histogram"] == {"2": 1, "3": 1}
    assert got["max"] == 3


def test_two_workers_merge_into_one_report() -> None:
    log = GOLDEN_LOG.read_text()
    a = ddl.worker_report(log, [(_epoch(2.0), _epoch(2.25), "a::t")], pid=1, tests_run=10)
    b = ddl.worker_report(log, [(_epoch(2.29), _epoch(2.45), "b::t")], pid=2, tests_run=30)
    idle = ddl.worker_report(None, [], pid=3, tests_run=5)
    merged = ddl.merge_reports([a, b, idle])
    assert merged["buckets"]["migration"]["total"] == 18
    assert merged["buckets"]["tests"]["total"] == 16
    assert merged["total"]["total"] == 34
    assert merged["total"]["by_kind"]["create_table"] == 8
    assert (merged["workers"], merged["workers_booted"], merged["tests_run"]) == (3, 2, 45)
    assert [row[0] for row in merged["per_test"]["top"]] == ["a::t", "b::t"]
    assert ddl.summary_line(merged) == "ddl: migration=18 tests=16 per_test=0.36"


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
    result.stdout.fnmatch_lines(["ddl: migration=9 tests=8 per_test=1.00 (report: *nx-ddl-report.json)"])
    report = json.loads((tmp_path / "ddl-report" / ddl.REPORT_NAME).read_text())
    assert report["total"]["total"] == 17
    assert report["tests_run"] == 8
    assert report["buckets"]["migration"]["by_kind"]["drop"] == 2


def test_xdist_workers_merge_into_one_report(pytester, monkeypatch, tmp_path) -> None:
    pytest.importorskip("xdist")
    result = _fake_substrate_run(pytester, monkeypatch, tmp_path=tmp_path, args=("-n", "2"), subprocess=True)
    result.assert_outcomes(passed=8)
    result.stdout.fnmatch_lines(["ddl: migration=18 tests=16 per_test=2.00 (report: *nx-ddl-report.json)"])
    report = json.loads((tmp_path / "ddl-report" / ddl.REPORT_NAME).read_text())
    assert (report["workers"], report["workers_booted"], report["recovered_workers"]) == (2, 2, 0)
    assert len(list((tmp_path / "ddl-report").glob("nx-ddl-worker-*.json"))) == 2


def test_a_session_that_never_booted_a_substrate_prints_nothing(pytester, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NX_DDL_REPORT_DIR", str(tmp_path / "ddl-report"))
    monkeypatch.setattr(ddl, "substrate_log_text", lambda: None)
    pytester.makepyfile(test_inner="def test_it():\n    pass\n")
    result = pytester.runpytest_inprocess("-p", "tests._ddl_counter", "-p", "no:cacheprovider", "-q")
    result.assert_outcomes(passed=1)
    assert not re.search(r"^ddl:", result.stdout.str(), re.MULTILINE)
    assert not (tmp_path / "ddl-report" / ddl.REPORT_NAME).exists()
