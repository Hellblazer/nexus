# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bytes-written and DDL-per-run counter for the engine test substrate (nexus-sz651).

The test suites rebuild schema over and over (epic nexus-aca0q), and what that costs is disk
thrashing, so the TARGET is bytes written. This module counts two things per run, each split
into the migration the substrate boots with and what the tests themselves cause:

* WAL bytes the substrate cluster generated: the probe's ``mark()`` logs
  ``pg_current_wal_insert_lsn()`` (as a byte count) and the summed database size at the ``start``,
  ``ready`` and ``end`` marks; ``ready - start`` is the migration and ``end - ready`` is the
  tests. WAL is a RELATIVE, LOWER-BOUND churn proxy, not what a disk-backed cluster would write:
  the clusters run ``fsync = off`` and ``full_page_writes = off`` (so no full-page images), the
  Python substrate also ``wal_level = minimal`` (so WAL-skipped bulk operations log nothing), and
  the Java containers keep the default ``wal_level``, so Python and Java WAL are not comparable
  with each other. The clusters sit on tmpfs on Linux with headroom (else the tempdir), where
  their data files are not NVMe writes at all. Use WAL to compare one run with another of the
  same suite, never as a disk-write figure. The job cgroup's ``io.stat`` ``wbytes`` is read around
  the pytest window where readable (Linux, cgroup v2); it is informational, not ratcheted, and
  only means something when the run owns its cgroup.

  NOT MEASURED here: the job workspace and checkout, ``target/``, ``.venv``, jar builds,
  journald, anything before pytest starts, and the docker-run and e2e shell clusters. The labrun
  NVMe sampler is the source of truth for bytes written to disk.
* DDL statements, the secondary diagnostic that explains where the bytes come from, split by
  kind.

Two signals, because one is blind to the churn that matters:

* ``log_statement = 'ddl'`` logs every TOP-LEVEL DDL statement a client sends
  (migrations, ``CREATE DATABASE``, ``CREATE ROLE``). It does NOT log DDL that a
  function runs through ``EXECUTE``. The per-tenant partition leaves
  (``nexus.ensure_tenant_partitions`` / ``nexus.drop_tenant_partitions``) are
  exactly that, so a tenant mint per test is invisible to it. Measured on a
  scratch PG 17.5 cluster on 2026-10-19: ``SELECT mk('a')`` running
  ``EXECUTE 'CREATE TABLE ... PARTITION OF'`` left no ``statement:`` line.
* a database-level EVENT TRIGGER (``ddl_command_end``) ``RAISE LOG``s the tag of
  every DDL command that completes, nested or not. It does not fire for
  shared-object DDL (``CREATE DATABASE`` / ``ROLE`` / ``TABLESPACE``), which
  ``log_statement`` covers.

Counting rule (identical in ``DdlLogParser.java``; the two parsers share one
golden log, ``tests/fixtures/ddl/golden-pg-log.txt`` and its copy under
``service/src/test/resources/ddl``): ``total = logged + nested``, where ``logged`` is
the number of ``statement:`` / ``execute ...:`` lines and ``nested`` is the
number of event-trigger lines NOT matched to a logged statement on the same
backend. A logged statement that will fire the event trigger claims the next
event from its backend; shared-object DDL, a failed statement (an ``ERROR``
line from the backend) and a re-logged statement release the claim.

The phases are ``nx_ddl_mark: <label> wal=<bytes> dbsize=<bytes>`` LOG lines the substrate
emits: ``start`` once the probe is installed (everything before it is ``boot``, which holds the
probe's own install and is never counted), ``ready`` once the engine has migrated (start to
ready is ``migration``), and ``end`` at session end (ready to end is ``tests``). A log with no
``start`` mark has no boot phase. Per-test
attribution compares each DDL line's ``%m`` timestamp (``log_timezone`` is
pinned to UTC) with the wall-clock window the plugin recorded for each test.

A pytest plugin (registered from ``tests/conftest.py``), shaped like
``tests/_storage_touch_guard.py``: each worker writes its report to a JSON
file and hands it to the controller through ``workeroutput``; the controller
merges them, prints ``ddl: migration=<n> tests=<n> per_test=<x>`` and writes
``nx-ddl-report.json`` under ``NX_DDL_REPORT_DIR`` (default: the controller's
basetemp). A worker the OS killed before its ``sessionfinish`` leaves no file and hands over nothing;
the controller sees it only as an xdist node going down without a report, counts it as
``workers_lost`` and ``ddl_ratchet.py`` refuses the report. Clusters other than the substrate's (``throwaway_pg_cluster``, a
test's own engine) are not counted; ``tests/ddl_uncounted_clusters.toml`` lists the start sites a
static scan of ``tests/`` and ``scripts/`` finds (docker-run and e2e shell clusters are not scanned),
and the terminal line prints how many there are. That figure is a census of call sites, printed on
every run whether or not the tests that reach them ran, and unweighted by size.
"""
from __future__ import annotations

import bisect
import calendar
import contextlib
import json
import os
import re
import tempfile
import time
import tomllib
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

#: ``NX_DDL_COUNT=0`` turns the counter fully off (nexus-3cwsw): the substrate adds no logging
#: settings, installs no probe, logs no marks, and the plugin registers nothing, prints nothing
#: and writes no report, so a run can be compared with one at the same sha. Unset or ``1``: on.
COUNT_ENV = "NX_DDL_COUNT"
REPORT_DIR_ENV = "NX_DDL_REPORT_DIR"
RUN_ID_ENV = "NX_DDL_RUN_ID"
REPORT_NAME = "nx-ddl-report.json"
WORKER_FILE = "nx-ddl-worker-{run}-{pid}.json"
_WORKEROUTPUT_KEY = "nx_ddl_report"

EVENT_PREFIX = "nx_ddl: "
MARK_PREFIX = "nx_ddl_mark: "
START_LABEL = "start"
READY_LABEL = "ready"
END_LABEL = "end"

#: Appended to the substrate's postgresql.conf. ``%d`` is the database name
#: (empty for background processes, hence the ``db=`` anchor); the clock is UTC
#: so the log's timestamps compare with ``time.time()``.
LOG_CONF = (
    "log_statement = 'ddl'\n"
    "log_line_prefix = '%m [%p] db=%d '\n"
    "log_timezone = 'UTC'\n"
)

#: The mark reads the WAL INSERT position, not ``pg_current_wal_lsn()`` (the write pointer): under
#: ``synchronous_commit = off``, which the test clusters run, the write pointer lags the insert
#: pointer by megabytes, so a mark at the write pointer leaves recent WAL out of its interval.
#: ``service/src/test/resources/ddl/nx-ddl-probe.sql`` carries the same SQL; a test keeps them equal.
#:
#: Run once per database, as the cluster owner, before anything migrates it. The two SET clauses
#: keep each event to one short log line: without them every line also carries the whole PL/pgSQL
#: call stack and the triggering statement, 114 KB per tenant mint (measured 2026-10-19), which on
#: a 32-worker box is gigabytes of tmpfs for the run.
PROBE_SQL = """
CREATE SCHEMA IF NOT EXISTS nx_ddl_probe;
CREATE OR REPLACE FUNCTION nx_ddl_probe.fire() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
  SET log_error_verbosity = terse SET log_min_error_statement = panic AS $probe$
BEGIN
  RAISE LOG 'nx_ddl: %', tg_tag;
END
$probe$;
CREATE OR REPLACE FUNCTION nx_ddl_probe.mark(label text) RETURNS void
LANGUAGE plpgsql SET log_error_verbosity = terse SET log_min_error_statement = panic AS $probe$
DECLARE
  wal numeric := pg_wal_lsn_diff(pg_current_wal_insert_lsn(), '0/0');
  dbsize numeric := -1;
BEGIN
  BEGIN
    SELECT coalesce(sum(pg_database_size(oid)), 0) INTO dbsize FROM pg_database;
  EXCEPTION WHEN OTHERS THEN
    dbsize := -1;
  END;
  RAISE LOG 'nx_ddl_mark: % wal=% dbsize=%', label, wal, dbsize;
END
$probe$;
CREATE EVENT TRIGGER nx_ddl_probe ON ddl_command_end EXECUTE FUNCTION nx_ddl_probe.fire();
"""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the counter is on (``NX_DDL_COUNT``). Strict: anything but unset, ``1`` or ``0`` raises,
    because a typo that left the counter on would make an A/B run compare nothing."""
    value = (os.environ if environ is None else environ).get(COUNT_ENV)
    if value is None or value == "1":
        return True
    if value == "0":
        return False
    raise ValueError(f"{COUNT_ENV} must be 0 or 1, got {value!r}")


def log_conf() -> str:
    """The postgresql.conf lines the counter adds: ``LOG_CONF``, or nothing with the counter off."""
    return LOG_CONF if enabled() else ""


def mark_sql(label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
        raise ValueError(f"mark label must be [A-Za-z0-9_.-]+, got {label!r}")
    return f"SELECT nx_ddl_probe.mark('{label}');"


KINDS = ("create_table", "create_index", "drop", "alter", "other")

_LINE = re.compile(
    r"^(?P<date>\d{4}-\d\d-\d\d) (?P<time>\d\d:\d\d:\d\d)(?:\.(?P<ms>\d+))? (?P<tz>\S+) "
    r"\[(?P<pid>\d+)\] db=(?P<db>\S*) (?P<level>[A-Z]+[0-9]*):  (?P<msg>.*)$"
)
_LOGGED = re.compile(r"^(?:statement|execute [^:]*): (?P<sql>.*)$", re.DOTALL)
_CREATE_MODIFIERS = frozenset({"OR", "REPLACE", "UNIQUE", "TEMP", "TEMPORARY", "UNLOGGED", "GLOBAL", "LOCAL"})
#: Statements ``log_statement = 'ddl'`` logs that never fire the event trigger, so they must not
#: claim the next event of their backend (measured on PG 17.5, 2026-10-19): shared-object DDL
#: (databases, roles, tablespaces), the trigger itself, ``ALTER SYSTEM``, ``REASSIGN OWNED``,
#: ``CLUSTER``, ``COMMENT ON`` a shared object, ``GRANT``/``REVOKE`` of a role membership (no
#: ``ON``) or on a database, tablespace or parameter. ``CREATE``/``ALTER``/``DROP USER MAPPING`` is
#: NOT in that family despite starting with ``USER``: it is per-database and fires the event
#: (nexus-mvr3t), hence the ``(?!\s+MAPPING\b)``. ``REINDEX`` is the opposite case: it fires
#: the event but ``log_statement = 'ddl'`` does not log it, so it is counted once, as nested.
#: The two ``(?:(?!\bON\b).)*`` clauses stop at the first ``ON`` keyword.
_NO_EVENT = re.compile(
    r"^(?:CREATE|ALTER|DROP)\s+(?:DATABASE|ROLE|USER(?!\s+MAPPING\b)|GROUP|TABLESPACE|EVENT\s+TRIGGER)\b"
    r"|^ALTER\s+SYSTEM\b"
    r"|^REASSIGN\s+OWNED\b"
    r"|^CLUSTER\b"
    r"|^COMMENT\s+ON\s+(?:DATABASE|ROLE|TABLESPACE)\b"
    r"|^(?:GRANT|REVOKE)\b(?:(?!\bON\b)(?!\b(?:TO|FROM)\b).)*\b(?:TO|FROM)\b"
    r"|^(?:GRANT|REVOKE)\b(?:(?!\bON\b).)*\bON\s+(?:DATABASE|TABLESPACE|PARAMETER)\b",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT = re.compile(r"\A(?:\s+|--[^\n]*\n?|/\*.*?\*/)+", re.DOTALL)
_START_MARK = re.compile(r"\bnx_ddl_mark: " + START_LABEL + r"\b")


def _head(sql: str) -> str:
    return _COMMENT.sub("", sql, count=1).lstrip()


def classify(text: str) -> str:
    """Kind of a DDL command, from an event-trigger tag (``CREATE TABLE``) or the
    head of a logged statement (``create unique index ...``)."""
    words = re.findall(r"[A-Za-z_]+", _head(text)[:200].upper())
    if not words:
        return "other"
    first = words[0]
    if first == "DROP":
        return "drop"
    if first == "ALTER":
        return "alter"
    if first == "CREATE":
        rest = [w for w in words[1:] if w not in _CREATE_MODIFIERS]
        if rest and rest[0] == "TABLE":
            return "create_table"
        if rest and rest[0] == "INDEX":
            return "create_index"
    return "other"


def tag_of(text: str) -> str:
    """``CREATE TABLE``-style tag: the first two words of a command, modifiers dropped."""
    words = [w for w in re.findall(r"[A-Za-z_]+", _head(text)[:200].upper()) if w not in _CREATE_MODIFIERS]
    if words and words[0] in ("GRANT", "REVOKE", "COMMENT"):
        return words[0]  # the event tag is the bare verb
    return " ".join(words[:2]) or "UNKNOWN"


@dataclass(frozen=True)
class Entry:
    """One counted DDL command. ``nested`` is True for a command that ran inside a
    function or block rather than being sent by a client."""

    ts: float | None
    pid: int
    db: str
    kind: str
    nested: bool
    #: Logged before the ``ready`` mark, i.e. boot or migration time.
    before_mark: bool
    tag: str = ""
    #: Logged before the ``start`` mark (the probe's own install and the cluster bootstrap):
    #: never counted. Always False for a log with no ``start`` mark.
    boot: bool = False


@dataclass(frozen=True)
class MarkStat:
    """A ``nx_ddl_mark`` line: the WAL position (bytes since LSN 0/0) and the summed size of
    every database when the mark ran. None when the line did not carry the figure."""

    label: str
    wal: int | None
    dbsize: int | None


@dataclass(frozen=True)
class ParsedLog:
    entries: tuple[Entry, ...]
    marks: tuple[str, ...]  # labels seen, in order
    stats: tuple[MarkStat, ...] = ()


def _timestamp(m: re.Match[str]) -> float | None:
    if m["tz"] != "UTC":
        return None
    try:
        whole = calendar.timegm(time.strptime(f"{m['date']} {m['time']}", "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None
    ms = m["ms"]
    return whole + (int(ms) / 10 ** len(ms) if ms else 0.0)


def _figure(text: str | None) -> int | None:
    """A non-negative integer figure from a mark line; ``-1`` (the probe's 'could not read') is None."""
    if text is None:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value >= 0 else None


def _parse_mark(msg: str) -> MarkStat:
    parts = msg[len(MARK_PREFIX):].split()
    fields = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
    return MarkStat(parts[0] if parts else "", _figure(fields.get("wal")), _figure(fields.get("dbsize")))


def parse_log(text: str, *, mark: str = READY_LABEL) -> ParsedLog:
    """Count the DDL in a PG log; entries before the first ``mark`` line are flagged ``before_mark``,
    and, when the log has a ``start`` mark, entries before it are flagged ``boot``.

    A log with no ``mark`` line is all ``before_mark`` (never reached ready). A logged
    statement whose first line is empty (``psql -c`` handed a leading newline) is
    read from its tab-indented continuation lines."""
    entries: list[Entry] = []
    marks: list[str] = []
    stats: list[MarkStat] = []
    claim: dict[int, bool] = {}  # pid -> a logged statement is waiting for its event
    seen_mark = False
    has_start = _START_MARK.search(text) is not None
    seen_start = False
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        m = _LINE.match(raw)
        if m is None:
            continue
        level, msg, pid = m["level"], m["msg"], int(m["pid"])
        if level in ("ERROR", "FATAL", "PANIC"):
            claim.pop(pid, None)
            continue
        if level != "LOG":
            continue
        if msg.startswith(MARK_PREFIX):
            stat = _parse_mark(msg)
            marks.append(stat.label)
            stats.append(stat)
            seen_mark = seen_mark or stat.label == mark
            seen_start = seen_start or stat.label == START_LABEL
            continue
        before = not seen_mark
        boot = has_start and not seen_start
        if msg.startswith(EVENT_PREFIX):
            tag = msg[len(EVENT_PREFIX):].strip()
            if claim.pop(pid, False):
                continue  # the command the client sent; already counted from its statement line
            entries.append(Entry(_timestamp(m), pid, m["db"], classify(tag), True, before, tag_of(tag), boot))
            continue
        logged = _LOGGED.match(msg)
        if logged is None:
            continue
        sql = logged["sql"]
        if not _head(sql):
            # The statement starts on a later line: psql -c with a leading newline, or a
            # Liquibase changeset whose first line is a comment.
            parts = [sql]
            for cont in lines[i + 1:i + 61]:
                if not cont.startswith("\t"):
                    break
                parts.append(cont[1:])
            sql = "\n".join(parts)
        if _NO_EVENT.match(_head(sql)):
            claim.pop(pid, None)
        else:
            claim[pid] = True
        entries.append(Entry(_timestamp(m), pid, m["db"], classify(sql), False, before, tag_of(sql), boot))
    return ParsedLog(tuple(entries), tuple(marks), tuple(stats))


def wal_split(stats: Iterable[MarkStat]) -> dict[str, int | None]:
    """WAL bytes and database growth by phase, from the ``start``, ``ready`` and ``end`` marks (the
    first of each, except ``end``, which is the last). A phase whose two marks are not both in the
    log is None, never 0: an unmeasured phase must not read as a free one."""
    first: dict[str, MarkStat] = {}
    last: dict[str, MarkStat] = {}
    for s in stats:
        first.setdefault(s.label, s)
        last[s.label] = s
    start, ready, end = first.get(START_LABEL), first.get(READY_LABEL), last.get(END_LABEL)

    def diff(b: MarkStat | None, a: MarkStat | None, field: str) -> int | None:
        if a is None or b is None:
            return None
        x, y = getattr(a, field), getattr(b, field)
        return None if x is None or y is None else y - x

    return {
        "migration_bytes": diff(ready, start, "wal"),
        "tests_bytes": diff(end, ready, "wal"),
        "total_bytes": diff(end, start, "wal"),
        "dbsize_growth_bytes": diff(end, ready, "dbsize"),
    }


def empty_tally() -> dict[str, Any]:
    return {"total": 0, "toplevel": 0, "nested": 0, "by_kind": {k: 0 for k in KINDS}}


def tally(entries: Iterable[Entry]) -> dict[str, Any]:
    t = empty_tally()
    for e in entries:
        t["total"] += 1
        t["nested" if e.nested else "toplevel"] += 1
        t["by_kind"][e.kind] += 1
    return t


def tag_counts(entries: Iterable[Entry]) -> dict[str, int]:
    """Commands by tag, most frequent first: what ``other`` is made of."""
    return dict(Counter(e.tag for e in entries).most_common(_TOP_TAGS))


def add_tallies(*tallies: dict[str, Any]) -> dict[str, Any]:
    out = empty_tally()
    for t in tallies:
        for key in ("total", "toplevel", "nested"):
            out[key] += t.get(key, 0)
        for k in KINDS:
            out["by_kind"][k] += t.get("by_kind", {}).get(k, 0)
    return out


# ── per-test attribution ─────────────────────────────────────────────────────

_TOP_N = 25
_TOP_TAGS = 20


def attribute(
    entries: Iterable[Entry], windows: list[tuple[float, float, str]],
) -> dict[str, Any]:
    """DDL per test: each entry goes to the test whose [start, end] window holds its
    timestamp, else to ``unattributed`` (a session or module fixture, or a line with no
    usable timestamp). ``windows`` is (start, end, nodeid) in any order.

    WAL bytes have no per-test figure here: the substrate would need an LSN read at every test
    boundary, a round trip per test on a 20k-test run. The per-test WAL number is the run's
    ``tests`` bytes over its test count."""
    ordered = sorted(windows)
    starts = [w[0] for w in ordered]
    per: Counter[str] = Counter()
    unattributed = 0
    for e in entries:
        idx = bisect.bisect_right(starts, e.ts) - 1 if e.ts is not None else -1
        if idx >= 0 and e.ts <= ordered[idx][1]:
            per[ordered[idx][2]] += 1
        else:
            unattributed += 1
    histogram = Counter(per.values())
    return {
        "tests_with_ddl": len(per),
        "unattributed": unattributed,
        "max": max(per.values(), default=0),
        "histogram": {str(k): v for k, v in sorted(histogram.items())},
        "top": [[nodeid, n] for nodeid, n in per.most_common(_TOP_N)],
    }


def worker_report(
    log_text: str | None,
    windows: list[tuple[float, float, str]],
    *,
    pid: int,
    tests_run: int,
) -> dict[str, Any]:
    """One process's report. ``log_text`` None means the substrate never booted here."""
    if log_text is None:
        return {
            "schema": 2, "source": "python-worker", "pid": pid, "booted": False,
            "tests_run": tests_run,
            "buckets": {"boot": empty_tally(), "migration": empty_tally(), "tests": empty_tally()},
            "per_test": attribute([], windows),
        }
    parsed = parse_log(log_text)
    boot = [e for e in parsed.entries if e.boot]
    migration = [e for e in parsed.entries if e.before_mark and not e.boot]
    after = [e for e in parsed.entries if not e.before_mark]
    return {
        "schema": 2, "source": "python-worker", "pid": pid, "booted": True,
        "ready_mark_seen": READY_LABEL in parsed.marks,
        "tests_run": tests_run,
        "buckets": {"boot": tally(boot), "migration": tally(migration), "tests": tally(after)},
        "wal": wal_split(parsed.stats),
        "tags": {"migration": tag_counts(migration), "tests": tag_counts(after)},
        "log_bytes": len(log_text.encode("utf-8", errors="replace")),
        "per_test": attribute(after, windows),
    }


def _sum_known(values: Iterable[int | None]) -> int | None:
    """Sum, or None when any term is unknown: one unmeasured worker voids the total."""
    total = 0
    for v in values:
        if v is None:
            return None
        total += v
    return total


def merge_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum worker reports into one: buckets add, per-test summaries combine. ``total`` is
    migration plus tests; ``boot`` (the probe's own install) is reported but not in it."""
    buckets: dict[str, dict[str, Any]] = {}
    for r in reports:
        for name, t in r.get("buckets", {}).items():
            buckets[name] = add_tallies(buckets.get(name, empty_tally()), t)
    tags: dict[str, Counter[str]] = {}
    for r in reports:
        for bucket, counts in r.get("tags", {}).items():
            tags.setdefault(bucket, Counter()).update(counts)
    histogram: Counter[str] = Counter()
    top: list[list[Any]] = []
    with_ddl = unattributed = peak = 0
    for r in reports:
        pt = r.get("per_test") or {}
        with_ddl += pt.get("tests_with_ddl", 0)
        unattributed += pt.get("unattributed", 0)
        peak = max(peak, pt.get("max", 0))
        histogram.update(pt.get("histogram", {}))
        top.extend(pt.get("top", []))
    top.sort(key=lambda row: -row[1])
    total = add_tallies(buckets.get("migration", empty_tally()), buckets.get("tests", empty_tally()))
    tests_run = sum(r.get("tests_run", 0) for r in reports)
    booted = [r for r in reports if r.get("booted", True)]
    wals = [r.get("wal") or {} for r in booted]
    return {
        "schema": 2,
        "workers": len(reports),
        "workers_booted": len(booted),
        "workers_ready_mark_seen": sum(1 for r in booted if r.get("ready_mark_seen")),
        "tests_run": tests_run,
        "log_bytes": sum(r.get("log_bytes", 0) for r in reports),
        "buckets": buckets,
        "total": total,
        "wal": {
            "migration_bytes": _sum_known(w.get("migration_bytes") for w in wals),
            "tests_bytes": _sum_known(w.get("tests_bytes") for w in wals),
            "total_bytes": _sum_known(w.get("total_bytes") for w in wals),
            "dbsize_growth_bytes": _sum_known(w.get("dbsize_growth_bytes") for w in wals),
            "workers_complete": sum(
                1 for w in wals if w.get("migration_bytes") is not None and w.get("tests_bytes") is not None
            ),
        },
        "tags": {b: dict(c.most_common(_TOP_TAGS)) for b, c in tags.items()},
        "per_test": {
            "tests_with_ddl": with_ddl,
            "unattributed": unattributed,
            "max": peak,
            "histogram": dict(sorted(histogram.items(), key=lambda kv: int(kv[0]))),
            "top": top[:_TOP_N],
        },
    }


def fmt_bytes(n: int | None) -> str:
    """Human size for the terminal line; the report carries exact integers."""
    if n is None:
        return "n/a"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"  # pragma: no cover


def summary_line(merged: dict[str, Any], *, uncounted: int | None = None) -> str:
    b = merged["buckets"]
    migration = b.get("migration", empty_tally())["total"]
    tests = b.get("tests", empty_tally())["total"]
    run = merged["tests_run"]
    per_test = f"{tests / run:.2f}" if run else "n/a"
    wal = merged.get("wal") or {}
    wal_tests = wal.get("tests_bytes")
    wal_per_test = fmt_bytes(round(wal_tests / run)) if wal_tests is not None and run else "n/a"
    line = (
        f"ddl: migration={migration} tests={tests} per_test={per_test}"
        f" wal: migration={fmt_bytes(wal.get('migration_bytes'))} tests={fmt_bytes(wal_tests)}"
        f" per_test={wal_per_test}"
    )
    io = merged.get("cgroup_io")
    if io is not None and io.get("wbytes_delta") is not None:
        line += f" io_wbytes={fmt_bytes(io['wbytes_delta'])}"
    if uncounted is not None:
        line += f" | {uncounted} uncounted cluster start sites (static tree census)"
    return line


# ── cgroup block-device writes ───────────────────────────────────────────────


def cgroup_io_wbytes(
    proc_cgroup: Path = Path("/proc/self/cgroup"), cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, Any] | None:
    """The calling process's cgroup (v2) ``io.stat``, ``wbytes`` summed over its block devices,
    or None where it cannot be read (not Linux, cgroup v1, no ``io`` controller). Only a run that
    owns its cgroup (a Nomad task, a container, a systemd scope) gets a figure that is the run's;
    on a shared cgroup the delta also counts every sibling's writes. A tmpfs write is not a
    block-device write, so a run entirely on tmpfs reads near zero here. This figure covers the
    pytest window only (not the checkout, ``target/``, ``.venv``, jar builds or journald) and is
    informational: the labrun NVMe sampler is the source of truth for disk writes."""
    try:
        for line in proc_cgroup.read_text().splitlines():
            hierarchy, _, rest = line.partition(":")
            if hierarchy == "0":
                path = rest.partition(":")[2].lstrip("/")
                break
        else:
            return None
        stat = (cgroup_root / path / "io.stat").read_text()
    except OSError:
        return None
    per_device: dict[str, int] = {}
    for row in stat.splitlines():
        device, _, rest = row.partition(" ")
        fields = dict(p.split("=", 1) for p in rest.split() if "=" in p)
        if "wbytes" in fields:
            per_device[device] = int(fields["wbytes"])
    return {"cgroup": path or "/", "wbytes": sum(per_device.values()), "per_device": per_device}


def cgroup_io_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any] | None:
    if before is None or after is None or before["cgroup"] != after["cgroup"]:
        return None
    return {
        "cgroup": after["cgroup"],
        "wbytes_delta": after["wbytes"] - before["wbytes"],
        "per_device_delta": {
            d: v - before["per_device"].get(d, 0) for d, v in after["per_device"].items()
        },
    }


# ── uncounted clusters ───────────────────────────────────────────────────────

UNCOUNTED_FILE = Path(__file__).resolve().parent / "ddl_uncounted_clusters.toml"


def uncounted_cluster_count(path: Path = UNCOUNTED_FILE) -> int | None:
    """How many cluster start sites in the test tree the counter does not see: the sum of the
    ``[uncounted]`` table of ``tests/ddl_uncounted_clusters.toml`` (a lint keeps that file equal to
    the tree). None when the file cannot be read."""
    try:
        return sum(int(n) for n in tomllib.loads(path.read_text()).get("uncounted", {}).values())
    except (OSError, ValueError):
        return None


# ── plugin ───────────────────────────────────────────────────────────────────

_STASH = pytest.StashKey["_Recorder"]()


def _basetemp(config: pytest.Config) -> Path:
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None:
        return factory.getbasetemp()
    return Path(tempfile.mkdtemp(prefix="nx-ddl-"))


def substrate_log_text() -> str | None:
    """The live substrate's pg.log, or None when this process never booted one. Logs the ``end``
    mark first (best effort: a cluster that is already gone just has no end figure)."""
    from tests import _engine_substrate  # noqa: PLC0415 — deferred: the plugin loads in every session

    state = _engine_substrate._state
    if state is None:
        return None
    try:
        _engine_substrate._psql_command(
            state["pg_bin"], state["pg_port"], state["pg_user"], mark_sql(END_LABEL), timeout=30,
        )
    except (RuntimeError, OSError, KeyError):
        pass
    try:
        return (Path(state["pgdata"]) / "pg.log").read_text(errors="replace")
    except OSError:
        return None


class _Recorder:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.windows: list[tuple[float, float, str]] = []
        self._open: dict[str, float] = {}
        self.reported: list[dict[str, Any]] = []
        #: Workers xdist reported down that handed over no report: killed before ``sessionfinish``
        #: (nexus-mvr3t). Such a worker leaves no file either, since the file is written in the same hook.
        self.lost = 0
        self.run_id = os.environ.get(RUN_ID_ENV) or uuid.uuid4().hex[:8]
        self.cgroup_start = cgroup_io_wbytes() if not hasattr(config, "workerinput") else None

    def pytest_runtest_logstart(self, nodeid, location):
        self._open[nodeid] = time.time()

    def pytest_runtest_logfinish(self, nodeid, location):
        start = self._open.pop(nodeid, None)
        if start is not None:
            self.windows.append((start, time.time(), nodeid))

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        payload = (getattr(node, "workeroutput", None) or {}).get(_WORKEROUTPUT_KEY)
        if payload:
            self.reported.append(payload)
        else:
            self.lost += 1

    def own_report(self) -> dict[str, Any]:
        return worker_report(
            substrate_log_text(), self.windows, pid=os.getpid(), tests_run=len(self.windows),
        )

    def report_dir(self) -> Path:
        configured = os.environ.get(REPORT_DIR_ENV)
        directory = Path(configured) if configured else _basetemp(self.config)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def pytest_sessionfinish(self, session):
        if session.config.option.collectonly:
            return
        report = self.own_report()
        workeroutput = getattr(session.config, "workeroutput", None)
        if workeroutput is not None:
            # The file survives a worker that dies before the controller collects workeroutput.
            path = self.report_dir() / WORKER_FILE.format(run=self.run_id, pid=report["pid"])
            path.write_text(json.dumps(report))
            workeroutput[_WORKEROUTPUT_KEY] = report
            return
        # An xdist controller runs no tests itself (it only replays the workers' start/finish
        # events), so its own report would count a log it never wrote to.
        if not session.config.pluginmanager.has_plugin("dsession"):
            self.reported.append(report)

    def recovered(self) -> list[dict[str, Any]]:
        """Worker files this run left that no worker handed over (a worker died). Without
        ``NX_DDL_REPORT_DIR`` a worker's basetemp is the controller's ``/popen-gwN`` (xdist names it
        so), so those are searched too."""
        known = {r["pid"] for r in self.reported}
        directory = self.report_dir()
        pattern = WORKER_FILE.format(run=self.run_id, pid="*")
        paths = sorted({*directory.glob(pattern), *directory.glob(f"popen-gw*/{pattern}")})
        found = []
        for path in paths:
            with contextlib.suppress(OSError, ValueError, KeyError):
                report = json.loads(path.read_text())
                if report["pid"] not in known:
                    found.append(report)
        return found

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config):
        if hasattr(config, "workerinput") or not (self.reported or self.lost):
            return
        recovered = self.recovered()
        merged = merge_reports([*self.reported, *recovered])
        merged["recovered_workers"] = len(recovered)
        # A worker that wrote its file and died before handing it over is in ``recovered``, so it is not lost.
        merged["workers_lost"] = max(0, self.lost - len(recovered))
        merged["cgroup_io"] = cgroup_io_delta(self.cgroup_start, cgroup_io_wbytes())
        merged["uncounted_cluster_start_sites"] = uncounted_cluster_count()
        # The report is written even when no worker booted a substrate: the substrate boots lazily, so a
        # shard of tests that never touch it is legitimate, and ``ddl_ratchet.py fan-in`` needs that
        # shard's test count and its (absent) clusters to add up the run.
        path = self.report_dir() / REPORT_NAME
        path.write_text(json.dumps(merged, indent=1))
        note = f" RECOVERED {len(recovered)} worker file(s) no worker handed over;" if recovered else ""
        if merged["workers_lost"]:
            note += (
                f" LOST {merged['workers_lost']} worker(s) that died before reporting (their DDL and WAL are "
                "not counted; ddl_ratchet.py refuses this report);"
            )
        if not merged["workers_booted"]:
            if merged["workers_lost"]:
                terminalreporter.write_line(f"ddl: no substrate counted (report: {_summary_report_path(path)}){note}")
            return
        terminalreporter.write_line(
            f"{summary_line(merged, uncounted=merged['uncounted_cluster_start_sites'])} "
            f"(report: {_summary_report_path(path)}){note}"
        )


def _summary_report_path(path: Path) -> Path:
    """The path to PRINT for a report written to *path*: inside a lab-run grant the
    basetemp is on tmpfs and is deleted after the summary, with the ``nx-*`` reports
    copied to disk first (``tests/_tmp_root.py``, nexus-kyujr), so the line names the
    copy that survives. *path* itself outside that case."""
    try:
        from tests._tmp_root import kept_report_path  # noqa: PLC0415 — summary-time only; the module imports the substrate

        return kept_report_path(path)
    except Exception:  # noqa: BLE001 — a summary line is never worth a failure
        return path


def pytest_configure(config: pytest.Config) -> None:
    if not enabled():
        return
    rec = _Recorder(config)
    config.stash[_STASH] = rec
    config.pluginmanager.register(rec, "nx-ddl-counter")
    if not hasattr(config, "workerinput"):
        os.environ.setdefault(RUN_ID_ENV, rec.run_id)


def pytest_unconfigure(config: pytest.Config) -> None:
    rec = config.stash.get(_STASH, None)
    if rec is not None:
        del config.stash[_STASH]
        config.pluginmanager.unregister(rec)
