# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure model for the console Threads panel (nexus-i99vk).

Parses the per-session entries Claude sessions keep in T2 project ``threads``
and the ``needs-sam`` bead list into a board. No I/O here: callers hand in
rows and a clock, and the panel's routes do the reading.

Entry format (parsed tolerantly; a line that fits nothing is kept as a note)::

    session: nexus_c9c
    updated: 2026-10-25T18:05Z
    ## RDR-234 auto-continuation
    ref: RDR-234, nexus-qlrj7
    state: spike proven; rewriting the draft
    waiting_on: none
    needs_sam: none
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

#: A session whose newest update is older than this is greyed out.
STALE_AFTER = timedelta(hours=24)

_NO_VALUES = frozenset({"", "none", "no", "n/a", "-", "—"})
_THREAD_FIELDS = {
    "ref": "refs",
    "refs": "refs",
    "state": "state",
    "waiting_on": "waiting_on",
    "needs_sam": "needs_sam",
}


@dataclass(frozen=True)
class Thread:
    title: str
    refs: tuple[str, ...] = ()
    state: str = ""
    waiting_on: str = ""
    needs_sam: str = ""
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Session:
    name: str
    updated: datetime | None
    threads: tuple[Thread, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionCard:
    session: Session
    effective_updated: datetime | None
    age: str
    stale: bool


@dataclass(frozen=True)
class NeedsItem:
    """One open ask: a thread whose needs_sam is set, with who asked and how long ago."""

    session: str
    thread: Thread
    age: str


@dataclass(frozen=True)
class Board:
    cards: tuple[SessionCard, ...]
    #: Every thread whose needs_sam is set, newest session first.
    needs_you: tuple[NeedsItem, ...]

    @property
    def active(self) -> tuple[SessionCard, ...]:
        return tuple(c for c in self.cards if not c.stale)

    @property
    def quiet(self) -> tuple[SessionCard, ...]:
        """Sessions with no update in over 24 hours."""
        return tuple(c for c in self.cards if c.stale)


@dataclass(frozen=True)
class Bead:
    id: str
    title: str
    priority: int | None = None
    assignee: str = ""
    updated: datetime | None = None


@dataclass(frozen=True)
class BeadsResult:
    """Outcome of the needs-sam bead query: the beads, or one line saying why not."""

    beads: tuple[Bead, ...]
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def has_value(value: str) -> bool:
    """False for an empty field or one saying "none"."""
    return value.strip().lower() not in _NO_VALUES


is_needs_sam = has_value


def parse_timestamp(value: str) -> datetime | None:
    """ISO-8601 to an aware UTC datetime; naive means UTC; junk means None."""
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _split_field(line: str) -> tuple[str, str] | None:
    key, sep, value = line.partition(":")
    if not sep:
        return None
    return key.strip().lower(), value.strip()


def parse_session(name: str, content: str) -> Session:
    updated: datetime | None = None
    notes: list[str] = []
    threads: list[Thread] = []
    cur: dict[str, Any] | None = None

    def flush() -> None:
        if cur is not None:
            threads.append(Thread(
                title=cur["title"],
                refs=tuple(cur["refs"]),
                state=cur["state"],
                waiting_on=cur["waiting_on"],
                needs_sam=cur["needs_sam"],
                notes=tuple(cur["notes"]),
            ))

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            flush()
            cur = {"title": line[3:].strip(), "refs": [], "state": "", "waiting_on": "",
                   "needs_sam": "", "notes": []}
            continue
        field = _split_field(line)
        if cur is None:
            if field and field[0] == "updated":
                updated = parse_timestamp(field[1])
            elif field and field[0] == "session":
                pass  # the T2 title names the session; the header line adds nothing
            else:
                notes.append(line)
            continue
        if field and field[0] in _THREAD_FIELDS:
            slot = _THREAD_FIELDS[field[0]]
            if slot == "refs":
                cur["refs"].extend(r.strip() for r in field[1].split(",") if r.strip())
            else:
                cur[slot] = field[1]
        else:
            cur["notes"].append(line)
    flush()
    return Session(name=name, updated=updated, threads=tuple(threads), notes=tuple(notes))


def format_age(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    hours = seconds // 3600
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def build_board(entries: Iterable[Mapping[str, Any]], now: datetime) -> Board:
    """Cards newest first (no usable time sorts last), plus the cross-session needs-you list.

    ``entries`` are T2 rows: ``title``, ``content``, ``timestamp``. A session's
    freshness is the newer of its ``updated:`` header and the row's own timestamp.
    """
    built: list[SessionCard] = []
    for e in entries:
        session = parse_session(str(e.get("title", "")), str(e.get("content", "")))
        row_time = parse_timestamp(str(e.get("timestamp", "") or ""))
        times = [t for t in (session.updated, row_time) if t is not None]
        newest = max(times) if times else None
        if newest is None:
            built.append(SessionCard(session, None, "unknown", True))
        else:
            delta = now - newest
            built.append(SessionCard(session, newest, format_age(delta), delta > STALE_AFTER))
    floor = datetime.min.replace(tzinfo=timezone.utc)
    built.sort(key=lambda c: c.effective_updated or floor, reverse=True)
    needs = tuple(
        NeedsItem(c.session.name, t, c.age)
        for c in built
        for t in c.session.threads
        if is_needs_sam(t.needs_sam)
    )
    return Board(cards=tuple(built), needs_you=needs)


def parse_beads_json(raw: str) -> tuple[Bead, ...]:
    """Parse ``bd list --json`` output. ``bd`` may print a trailing note after the array."""
    try:
        data, _ = json.JSONDecoder().raw_decode(raw.lstrip())
    except ValueError as exc:
        raise ValueError(f"bd output is not JSON: {raw.strip()[:80]!r}") from exc
    if not isinstance(data, list):
        raise ValueError("bd output is not a JSON list")
    beads: list[Bead] = []
    for row in data:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        prio = row.get("priority")
        beads.append(Bead(
            id=str(row["id"]),
            title=str(row.get("title", "")),
            priority=prio if isinstance(prio, int) else None,
            assignee=str(row.get("assignee") or ""),
            updated=parse_timestamp(str(row.get("updated_at") or "")),
        ))
    return tuple(beads)
