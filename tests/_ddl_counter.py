# SPDX-License-Identifier: AGPL-3.0-or-later
"""DDL-statements-per-run counter for the engine test substrate (nexus-sz651).

The test suites rebuild schema over and over (epic nexus-aca0q). This module
makes the number visible: how many DDL statements one run executes, split into
the migration the substrate boots with and the DDL the tests themselves cause,
and by kind.

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
golden log, ``tests/data/ddl_golden-pg-log.txt`` and its copy under
``service/src/test/resources``): ``total = logged + nested``, where ``logged`` is
the number of ``statement:`` / ``execute ...:`` lines and ``nested`` is the
number of event-trigger lines NOT matched to a logged statement on the same
backend. A logged statement that will fire the event trigger claims the next
event from its backend; shared-object DDL, a failed statement (an ``ERROR``
line from the backend) and a re-logged statement release the claim.

The migration/test split is a ``nx_ddl_mark: <label>`` LOG line the substrate
emits once it is ready; everything before it is migration. Per-test
attribution compares each DDL line's ``%m`` timestamp (``log_timezone`` is
pinned to UTC) with the wall-clock window the plugin recorded for each test.

A pytest plugin (registered from ``tests/conftest.py``), shaped like
``tests/_storage_touch_guard.py``: each worker writes its report to a JSON
file and hands it to the controller through ``workeroutput``; the controller
merges them, prints ``ddl: migration=<n> tests=<n> per_test=<x>`` and writes
``nx-ddl-report.json`` under ``NX_DDL_REPORT_DIR`` (default: the controller's
basetemp). Clusters other than the substrate's (``throwaway_pg_cluster``, a
test's own engine) are not counted.
"""
from __future__ import annotations

import bisect
import calendar
import contextlib
import json
import os
import re
import time
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPORT_DIR_ENV = "NX_DDL_REPORT_DIR"
RUN_ID_ENV = "NX_DDL_RUN_ID"
REPORT_NAME = "nx-ddl-report.json"
WORKER_FILE = "nx-ddl-worker-{run}-{pid}.json"
_WORKEROUTPUT_KEY = "nx_ddl_report"

EVENT_PREFIX = "nx_ddl: "
MARK_PREFIX = "nx_ddl_mark: "
READY_LABEL = "ready"

#: Appended to the substrate's postgresql.conf. ``%d`` is the database name
#: (empty for background processes, hence the ``db=`` anchor); the clock is UTC
#: so the log's timestamps compare with ``time.time()``.
LOG_CONF = (
    "log_statement = 'ddl'\n"
    "log_line_prefix = '%m [%p] db=%d '\n"
    "log_timezone = 'UTC'\n"
)

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
BEGIN
  RAISE LOG 'nx_ddl_mark: %', label;
END
$probe$;
CREATE EVENT TRIGGER nx_ddl_probe ON ddl_command_end EXECUTE FUNCTION nx_ddl_probe.fire();
"""


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
#: DDL the event trigger never sees (shared objects, or the trigger itself).
_NO_EVENT = re.compile(
    r"^(?:CREATE|ALTER|DROP)\s+(?:DATABASE|ROLE|USER|GROUP|TABLESPACE|EVENT\s+TRIGGER)\b"
    r"|^ALTER\s+SYSTEM\b",
    re.IGNORECASE,
)
_COMMENT = re.compile(r"\A(?:\s+|--[^\n]*\n?|/\*.*?\*/)+", re.DOTALL)


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
    #: Entries in the log before the ``ready`` mark, i.e. boot/migration time.
    before_mark: bool
    tag: str = ""


@dataclass(frozen=True)
class ParsedLog:
    entries: tuple[Entry, ...]
    marks: tuple[str, ...]  # labels seen, in order


def _timestamp(m: re.Match[str]) -> float | None:
    if m["tz"] != "UTC":
        return None
    try:
        whole = calendar.timegm(time.strptime(f"{m['date']} {m['time']}", "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None
    ms = m["ms"]
    return whole + (int(ms) / 10 ** len(ms) if ms else 0.0)


def parse_log(text: str, *, mark: str = READY_LABEL) -> ParsedLog:
    """Count the DDL in a PG log; entries before the first ``mark`` line are flagged ``before_mark``.

    A log with no such mark is all ``before_mark`` (never reached ready). A logged
    statement whose first line is empty (``psql -c`` handed a leading newline) is
    read from its tab-indented continuation lines."""
    entries: list[Entry] = []
    marks: list[str] = []
    claim: dict[int, bool] = {}  # pid -> a logged statement is waiting for its event
    seen_mark = False
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
            label = msg[len(MARK_PREFIX):].strip()
            marks.append(label)
            seen_mark = seen_mark or label == mark
            continue
        before = not seen_mark
        if msg.startswith(EVENT_PREFIX):
            tag = msg[len(EVENT_PREFIX):].strip()
            if claim.pop(pid, False):
                continue  # the command the client sent; already counted from its statement line
            entries.append(Entry(_timestamp(m), pid, m["db"], classify(tag), True, before, tag_of(tag)))
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
        entries.append(Entry(_timestamp(m), pid, m["db"], classify(sql), False, before, tag_of(sql)))
    return ParsedLog(tuple(entries), tuple(marks))


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
    usable timestamp). ``windows`` is (start, end, nodeid) in any order."""
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
            "schema": 1, "source": "python-worker", "pid": pid, "booted": False,
            "tests_run": tests_run,
            "buckets": {"migration": empty_tally(), "tests": empty_tally()},
            "per_test": attribute([], windows),
        }
    parsed = parse_log(log_text)
    after = [e for e in parsed.entries if not e.before_mark]
    return {
        "schema": 1, "source": "python-worker", "pid": pid, "booted": True,
        "ready_mark_seen": READY_LABEL in parsed.marks,
        "tests_run": tests_run,
        "buckets": {
            "migration": tally(e for e in parsed.entries if e.before_mark),
            "tests": tally(after),
        },
        "tags": {
            "migration": tag_counts(e for e in parsed.entries if e.before_mark),
            "tests": tag_counts(after),
        },
        "log_bytes": len(log_text.encode("utf-8", errors="replace")),
        "per_test": attribute(after, windows),
    }


def merge_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum worker (or Java fork) reports into one: buckets add, per-test summaries combine."""
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
    total = add_tallies(*buckets.values()) if buckets else empty_tally()
    tests_run = sum(r.get("tests_run", 0) for r in reports)
    return {
        "schema": 1,
        "workers": len(reports),
        "workers_booted": sum(1 for r in reports if r.get("booted", True)),
        "tests_run": tests_run,
        "log_bytes": sum(r.get("log_bytes", 0) for r in reports),
        "buckets": buckets,
        "total": total,
        "tags": {b: dict(c.most_common(_TOP_TAGS)) for b, c in tags.items()},
        "per_test": {
            "tests_with_ddl": with_ddl,
            "unattributed": unattributed,
            "max": peak,
            "histogram": dict(sorted(histogram.items(), key=lambda kv: int(kv[0]))),
            "top": top[:_TOP_N],
        },
    }


def summary_line(merged: dict[str, Any]) -> str:
    b = merged["buckets"]
    migration = b.get("migration", empty_tally())["total"]
    tests = b.get("tests", empty_tally())["total"]
    run = merged["tests_run"]
    per_test = f"{tests / run:.2f}" if run else "n/a"
    return f"ddl: migration={migration} tests={tests} per_test={per_test}"


# ── plugin ───────────────────────────────────────────────────────────────────

_STASH = pytest.StashKey["_Recorder"]()


def _basetemp(config: pytest.Config) -> Path:
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None:
        return factory.getbasetemp()
    import tempfile

    return Path(tempfile.mkdtemp(prefix="nx-ddl-"))


def substrate_log_text() -> str | None:
    """The live substrate's pg.log, or None when this process never booted one."""
    from tests import _engine_substrate  # noqa: PLC0415 — deferred: the plugin loads in every session

    state = _engine_substrate._state
    if state is None:
        return None
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
        self.run_id = os.environ.get(RUN_ID_ENV) or uuid.uuid4().hex[:8]

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
        """Worker files this run left that no worker handed over (a worker died)."""
        known = {r["pid"] for r in self.reported}
        found = []
        for path in sorted(self.report_dir().glob(WORKER_FILE.format(run=self.run_id, pid="*"))):
            with contextlib.suppress(OSError, ValueError, KeyError):
                report = json.loads(path.read_text())
                if report["pid"] not in known:
                    found.append(report)
        return found

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config):
        if hasattr(config, "workerinput") or not self.reported:
            return
        recovered = self.recovered()
        merged = merge_reports([*self.reported, *recovered])
        merged["recovered_workers"] = len(recovered)
        if not merged["workers_booted"]:
            return
        path = self.report_dir() / REPORT_NAME
        path.write_text(json.dumps(merged, indent=1))
        lost = f" RECOVERED {len(recovered)} worker file(s) no worker handed over;" if recovered else ""
        terminalreporter.write_line(f"{summary_line(merged)} (report: {path}){lost}")


def pytest_configure(config: pytest.Config) -> None:
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
