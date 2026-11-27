# SPDX-License-Identifier: AGPL-3.0-or-later
"""The flake quarantine registry: format, checks, and the candidates reader (RDR-258 Phase 5, nexus-2jomg.6).

``tests/quarantine.toml`` is the one list of quarantined tests. Each ``[[quarantine]]`` entry names a pytest
nodeid and carries ``since``, ``deadline`` (since + 14 days, checked here, never trusted), ``bead``, ``reason``
and ``source`` (the sha256 of the candidates file the nodeid came from). Sam's ruling, 2026-11-27: a quarantine
lasts 14 days, then the test is fixed or deleted.

Standard library only: ``scripts/quarantine_report.py`` and the nightly workflow's first job import this
without pytest installed. The pytest plugin that applies the marker lives in ``tests/_quarantine.py``.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

QUARANTINE_DAYS = 14
REGISTRY_PATH = Path(__file__).with_name("quarantine.toml")

_TABLE = "quarantine"
_KEYS = ("nodeid", "since", "deadline", "bead", "reason", "source")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class RegistryError(ValueError):
    """The registry, or a candidates file, is not what the quarantine rules allow."""


def deadline_for(since: date) -> date:
    return since + timedelta(days=QUARANTINE_DAYS)


@dataclass(frozen=True)
class Entry:
    nodeid: str
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
    for key in ("nodeid", "bead", "reason", "source"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise RegistryError(f"{where}: {key} must be a non-empty string")
    nodeid = raw["nodeid"]
    where = f"quarantine entry {index} ({nodeid})"
    if "::" not in nodeid:
        raise RegistryError(f"{where}: nodeid must name a test (file::test), not a file")
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
    return Entry(nodeid, since, deadline, raw["bead"], raw["reason"], raw["source"])


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
        f"since = {entry.since.isoformat()}\n"
        f"deadline = {entry.deadline.isoformat()}\n"
        f"bead = {_toml_str(entry.bead)}\n"
        f"reason = {_toml_str(entry.reason)}\n"
        f"source = {_toml_str(entry.source)}\n"
    )


@dataclass(frozen=True)
class Candidates:
    """What ``red_run_report.py candidates`` printed: the snapshot window, the sha256 it names, and two arrays of
    rows of one shape. ``flake_infra`` is what ``add`` may quarantine; ``unattributed`` is a class Sam decides."""

    sha256: str  # of the candidates file's bytes: the registry's ``source``
    window: Any
    flake_infra: tuple[dict[str, Any], ...]
    unattributed: tuple[dict[str, Any], ...]

    def nodeids(self) -> set[str]:
        """The nodeids ``add`` may quarantine: ``flake_infra`` only."""
        return {row["nodeid"] for row in self.flake_infra}

    def unattributed_nodeids(self) -> set[str]:
        return {row["nodeid"] for row in self.unattributed}


def _rows(doc: dict[str, Any], key: str, path: Path) -> tuple[dict[str, Any], ...]:
    rows = doc.get(key)
    if not isinstance(rows, list) or not all(isinstance(r, dict) and isinstance(r.get("nodeid"), str) for r in rows):
        raise RegistryError(f"candidates file {path}: {key} must be a list of rows, each with a nodeid")
    return tuple(rows)


def read_candidates(path: Path) -> Candidates:
    """The only reader of the candidates file shape: a top object with ``window``, ``sha256`` and the two row
    arrays ``flake_infra`` and ``unattributed`` (rows of {nodeid, red_runs, first_red, last_red, venues, rule})."""
    raw = Path(path).read_bytes()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"candidates file {path} is not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise RegistryError(f"candidates file {path}: want a top object with flake_infra and unattributed row arrays")
    return Candidates(
        hashlib.sha256(raw).hexdigest(),
        doc.get("window"),
        _rows(doc, "flake_infra", path),
        _rows(doc, "unattributed", path),
    )


def add_entries(
    registry: Path, candidates: Path, nodeids: Iterable[str], *, bead: str, reason: str, since: date
) -> list[Entry]:
    """Append an entry per nodeid, each of which the candidates file must name. Writes nothing on any refusal."""
    wanted = list(nodeids)
    if not wanted:
        raise RegistryError("no nodeid given to quarantine")
    cand = read_candidates(candidates)
    only_unattributed = [n for n in wanted if n in cand.unattributed_nodeids() and n not in cand.nodeids()]
    if only_unattributed:
        raise RegistryError(
            f"{', '.join(only_unattributed)}: only in the candidates file's unattributed class, "
            "which is Sam's to decide; add reads flake_infra only"
        )
    unknown = [n for n in wanted if n not in cand.nodeids()]
    if unknown:
        raise RegistryError(f"not named by the candidates file {candidates}: {', '.join(unknown)}")
    new = [Entry(n, since, deadline_for(since), bead, reason, cand.sha256) for n in dict.fromkeys(wanted)]
    existing = registry.read_text(encoding="utf-8")
    if existing and not existing.endswith("\n"):
        existing += "\n"
    joined = existing + ("\n" if existing else "") + "\n".join(render(e) for e in new)
    parse(joined)  # refuses a nodeid already in the registry (named twice) and any malformed field, before the write
    registry.write_text(joined, encoding="utf-8")
    return new


def arg_covers(nodeid: str, arg: str) -> bool:
    """True when a pytest path argument (rootdir-relative, posix) could have collected *nodeid*."""
    path, sep, _ = arg.partition("::")
    file = nodeid.partition("::")[0]
    if not sep:
        return path in ("", ".") or file == path or file.startswith(path.rstrip("/") + "/")
    return file == path and (nodeid == arg or nodeid.startswith((arg + "::", arg + "[")))


def run_covers(nodeid: str, args: Iterable[str]) -> bool:
    return any(arg_covers(nodeid, a) for a in args)
