# SPDX-License-Identifier: AGPL-3.0-or-later
"""The flake quarantine registry: format, checks, candidates reader, junit tally (RDR-258 Phase 5, nexus-2jomg.6).

``tests/quarantine.toml`` is the one list of quarantined tests. Each ``[[quarantine]]`` entry names a test and
carries ``kind`` (``pytest`` or ``java``), ``since``, ``deadline`` (since + 14 days, checked here, never trusted),
``bead``, ``reason`` and ``source`` (the sha256 of the candidates file the test came from). Sam's ruling,
2026-11-27: a quarantine lasts 14 days, then the test is fixed or deleted.

A pytest nodeid is ``path/to/test_x.py::Class::test[param]``; a java one is ``Class::method``, which surefire
selects as ``-Dtest=Class#method``.

Standard library only: ``scripts/quarantine_report.py`` and the nightly workflow import this without pytest
installed. The pytest plugin that applies the marker lives in ``tests/_quarantine.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

QUARANTINE_DAYS = 14
REGISTRY_PATH = Path(__file__).with_name("quarantine.toml")
KINDS = ("pytest", "java")

_TABLE = "quarantine"
_KEYS = ("nodeid", "kind", "since", "deadline", "bead", "reason", "source")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PYTEST_ID = re.compile(r"[^\s:]+\.py::\S+")
_JAVA_ID = re.compile(r"[A-Za-z_]\w*::[A-Za-z_]\w*")
#: colophon's row ``kind`` -> the registry's
_ROW_KINDS = {"test": "pytest", "java": "java"}


class RegistryError(ValueError):
    """The registry, a candidates file or a results file is not what the quarantine rules allow."""


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def deadline_for(since: date) -> date:
    return since + timedelta(days=QUARANTINE_DAYS)


@dataclass(frozen=True)
class Entry:
    nodeid: str
    kind: str
    since: date
    deadline: date
    bead: str
    reason: str
    source: str

    def age_days(self, today: date) -> int:
        return (today - self.since).days

    def days_left(self, today: date) -> int:
        return (self.deadline - today).days

    def overdue(self, today: date) -> bool:
        return today > self.deadline


def _entry(raw: Any, index: int) -> Entry:
    where = f"quarantine entry {index}"
    if not isinstance(raw, dict):
        raise RegistryError(f"{where}: not a table")
    for key in raw.keys() - set(_KEYS):
        raise RegistryError(f"{where}: unknown key {key!r}")
    for key in _KEYS:
        if key not in raw:
            raise RegistryError(f"{where}: missing key {key!r}")
    for key in ("nodeid", "kind", "bead", "reason", "source"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise RegistryError(f"{where}: {key} must be a non-empty string")
    nodeid, kind = raw["nodeid"], raw["kind"]
    where = f"quarantine entry {index} ({nodeid})"
    if kind not in KINDS:
        raise RegistryError(f"{where}: kind must be one of {', '.join(KINDS)}, not {kind!r}")
    if kind == "pytest" and not _PYTEST_ID.fullmatch(nodeid):
        raise RegistryError(f"{where}: a pytest nodeid is path/to/test_x.py::test (the file and the test)")
    if kind == "java" and not _JAVA_ID.fullmatch(nodeid):
        raise RegistryError(f"{where}: a java nodeid is Class::method")
    if not _SHA256.fullmatch(raw["source"]):
        raise RegistryError(f"{where}: source must be a lowercase sha256 hex digest")
    for key in ("since", "deadline"):
        # tomllib gives datetime.datetime for a timestamp, datetime.date for a date; a string is a mistake.
        if not isinstance(raw[key], date) or isinstance(raw[key], datetime):
            raise RegistryError(f"{where}: {key} must be a TOML date (2026-11-27), not {raw[key]!r}")
    since, deadline = raw["since"], raw["deadline"]
    if deadline != deadline_for(since):
        raise RegistryError(
            f"{where}: deadline {deadline} is not since {since} + {QUARANTINE_DAYS} days ({deadline_for(since)})"
        )
    return Entry(nodeid, kind, since, deadline, raw["bead"], raw["reason"], raw["source"])


def parse(text: str) -> list[Entry]:
    """Parse and check registry text. Raises ``RegistryError`` on anything the quarantine rules refuse."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"quarantine registry is not valid TOML: {exc}") from exc
    for key in doc.keys() - {_TABLE}:
        raise RegistryError(f"quarantine registry: unknown top-level key {key!r} (only [[{_TABLE}]] entries)")
    raw = doc.get(_TABLE, [])
    if not isinstance(raw, list):
        raise RegistryError(f"quarantine registry: {_TABLE} must be an array of tables ([[{_TABLE}]])")
    entries = [_entry(item, i) for i, item in enumerate(raw, start=1)]
    seen: set[str] = set()
    for e in entries:
        if e.nodeid in seen:
            raise RegistryError(f"quarantine registry names {e.nodeid} twice")
        seen.add(e.nodeid)
    return entries


def load(path: Path | None = None) -> list[Entry]:
    return parse((path or REGISTRY_PATH).read_text(encoding="utf-8"))


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def render(entry: Entry) -> str:
    return (
        f"[[{_TABLE}]]\n"
        f"nodeid = {_toml_str(entry.nodeid)}\n"
        f"kind = {_toml_str(entry.kind)}\n"
        f"since = {entry.since.isoformat()}\n"
        f"deadline = {entry.deadline.isoformat()}\n"
        f"bead = {_toml_str(entry.bead)}\n"
        f"reason = {_toml_str(entry.reason)}\n"
        f"source = {_toml_str(entry.source)}\n"
    )


def surefire_selector(entries: Iterable[Entry]) -> str:
    """The ``-Dtest=`` value for the java entries: ``Class#method,Class#method``."""
    return ",".join(e.nodeid.replace("::", "#") for e in entries if e.kind == "java")


def surefire_exclusion(entries: Iterable[Entry]) -> str:
    """The ``-Dtest=`` value that runs everything EXCEPT the java entries: ``!Class#method,!Class#method``."""
    return ",".join("!" + e.nodeid.replace("::", "#") for e in entries if e.kind == "java")


# ── candidates file ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidates:
    """What ``red_run_report.py candidates`` printed: a header, the ``candidates`` rows (flaky by the report's
    rules) and the ``unattributed`` rows (a class the 2-week review decides one test at a time)."""

    sha256: str  # of the candidates file's bytes: the registry's ``source``
    header: Any
    candidates: tuple[dict[str, Any], ...]
    unattributed: tuple[dict[str, Any], ...]

    def nodeids(self) -> set[str]:
        return {row["nodeid"] for row in self.candidates}

    def unattributed_nodeids(self) -> set[str]:
        return {row["nodeid"] for row in self.unattributed}

    def kind_of(self, nodeid: str) -> str:
        for row in (*self.candidates, *self.unattributed):
            if row["nodeid"] == nodeid:
                return _ROW_KINDS[row["kind"]]
        raise KeyError(nodeid)


def _rows(doc: dict[str, Any], key: str, path: Path) -> tuple[dict[str, Any], ...]:
    rows = doc.get(key)
    ok = isinstance(rows, list) and all(
        isinstance(r, dict) and isinstance(r.get("nodeid"), str) and r.get("kind") in _ROW_KINDS for r in rows
    )
    if not ok:
        raise RegistryError(
            f"candidates file {path}: {key} must be a list of rows, each with a nodeid and a kind ({'/'.join(_ROW_KINDS)})"
        )
    return tuple(rows)


def read_candidates(path: Path) -> Candidates:
    """The only reader of the candidates file shape: a top object with ``header``, ``candidates`` and ``unattributed``."""
    raw = Path(path).read_bytes()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"candidates file {path} is not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise RegistryError(f"candidates file {path}: want a top object with candidates and unattributed row arrays")
    return Candidates(
        hashlib.sha256(raw).hexdigest(), doc.get("header"), _rows(doc, "candidates", path), _rows(doc, "unattributed", path)
    )


def add_entries(
    registry: Path,
    candidates: Path,
    nodeids: Iterable[str],
    *,
    bead: str,
    reason: str,
    today: date,
    since: date | None = None,
    review: str | None = None,
) -> list[Entry]:
    """Append an entry per nodeid, each of which the candidates file must name. Writes nothing on any refusal.

    ``candidates`` rows are accepted as they are. An ``unattributed`` row is decided one test at a time at the
    2-week review (Sam's ruling): it is accepted only with ``review`` (the review's reference, stored in the
    entry's reason) and as the only nodeid of the call. ``since`` defaults to today (UTC) and may be neither in
    the future nor earlier than yesterday: a backdated entry would shorten the 14 days.
    """
    wanted = list(dict.fromkeys(nodeids))
    if not wanted:
        raise RegistryError("no nodeid given to quarantine")
    start = today if since is None else since
    if start > today:
        raise RegistryError(f"since {start} is in the future (today is {today})")
    if start < today - timedelta(days=1):
        raise RegistryError(
            f"since {start} is more than a day before today ({today}): a backdated entry shortens the {QUARANTINE_DAYS} days"
        )
    cand = read_candidates(candidates)
    if review is not None and len(wanted) != 1:
        raise RegistryError("--review names one decision: one nodeid per call")
    use_review = False
    for n in wanted:
        if n in cand.nodeids():
            continue
        if n in cand.unattributed_nodeids():
            if review is None:
                raise RegistryError(
                    f"{n}: only in the candidates file's unattributed class, which the 2-week review decides one test "
                    "at a time (Sam's ruling); pass --review <the review's reference>, with this one nodeid"
                )
            use_review = True
            continue
        raise RegistryError(f"not named by the candidates file {candidates}: {n}")
    note = f"{reason} (2-week review {review})" if use_review else reason
    new = [Entry(n, cand.kind_of(n), start, deadline_for(start), bead, note, cand.sha256) for n in wanted]
    existing = registry.read_text(encoding="utf-8")
    if existing and not existing.endswith("\n"):
        existing += "\n"
    joined = existing + ("\n" if existing else "") + "\n".join(render(e) for e in new)
    parse(joined)  # refuses a nodeid already in the registry (named twice) and any malformed field, before the write
    fd, tmp = tempfile.mkstemp(dir=registry.parent, prefix=f".{registry.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(joined)
        os.replace(tmp, registry)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return new


# ── junit results ────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Tally:
    passed: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def runs(self) -> int:
        return self.passed + self.failed + self.skipped

    @property
    def examined(self) -> int:
        """Runs that exercised the test: a skip examined nothing."""
        return self.passed + self.failed

    def text(self) -> str:
        if self.runs == 0:
            return "no result"
        parts = [f"passed {self.passed} of {self.runs}"]
        if self.failed:
            parts.append(f"failed {self.failed} of {self.runs}")
        if self.skipped:
            parts.append(f"skipped {self.skipped} of {self.runs}" + ("" if self.examined else " (not examined)"))
        return ", ".join(parts)


#: (classname, name, outcome) of one junit testcase; outcome is passed, failed or skipped
Case = tuple[str, str, str]


def junit_files(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        out.extend(sorted(p.rglob("*.xml")) if p.is_dir() else [p])
    return out


def read_junit(paths: Iterable[Path]) -> list[Case]:
    """Every testcase of the given junit files (a directory is searched for ``*.xml``). A missing or malformed file is an error."""
    cases: list[Case] = []
    for path in junit_files(paths):
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as exc:
            raise RegistryError(f"cannot read the junit file {path}: {exc}") from exc
        for tc in root.iter("testcase"):
            kids = {child.tag for child in tc}
            outcome = "failed" if kids & {"failure", "error"} else "skipped" if "skipped" in kids else "passed"
            cases.append((tc.get("classname") or "", tc.get("name") or "", outcome))
    return cases


def pytest_junit_key(nodeid: str) -> tuple[str, str]:
    """pytest's own junit address (``_pytest.junitxml.mangle_test_address``) of a nodeid: (classname, name)."""
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = names[0].replace("/", ".").removesuffix(".py")
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def _case_matches(entry: Entry, classname: str, name: str) -> bool:
    if entry.kind == "pytest":
        return (classname, name) == pytest_junit_key(entry.nodeid)
    cls, method = entry.nodeid.split("::")
    # surefire names a case by FQCN and, under JUnit 5, `method()` or `method(args)[n]`
    return classname.rsplit(".", 1)[-1] == cls and (name == method or name.startswith((method + "(", method + "[")))


def tally(entry: Entry, cases: Iterable[Case]) -> Tally:
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for classname, name, outcome in cases:
        if _case_matches(entry, classname, name):
            counts[outcome] += 1
    return Tally(**counts)


def classify_exit(rc: int, cases: Iterable[Case]) -> tuple[bool, str]:
    """Is a test runner's exit code acceptable for a quarantine pass? Returns (ok, why).

    0 is fine. 1 is fine only when at least one testcase failed or errored: a quarantined test failing is the
    expected state. Exit 1 with no failed testcase is a guard or session verdict (a conftest guard, a collection
    error, a build error), and every other code is a runner failure; both redden the job.
    """
    if rc == 0:
        return True, "exit 0"
    failed = sum(1 for _, _, outcome in cases if outcome == "failed")
    if rc == 1:
        if failed:
            return True, f"exit 1 with {failed} failed or errored testcase(s): quarantined tests failed, reported, not a red job"
        return False, "exit 1 with no failed or errored testcase: a guard or session verdict, not a test failure"
    if rc == 5:
        return False, "exit 5: no tests selected although the registry lists tests"
    return False, f"exit {rc}"
