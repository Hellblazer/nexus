# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure model for the console Threads panel (nexus-i99vk).

Parses the per-session entries Claude sessions keep in T2 project ``threads``
and the ``needs-sam`` bead list into a board. No I/O here: callers hand in
rows and a clock, and the panel's routes do the reading.

Entry format (parsed tolerantly; a line that fits nothing is kept as a note)::

    # RDR-234 auto-continuation
    ref: RDR-234, nexus-qlrj7
    state: spike proven; rewriting the draft
    waiting_on: none
    needs_sam: none

Rules the parser keeps so an ask is not lost to spelling: ``#``, ``##`` and
``###`` all start a thread; ``-``, space and ``_`` in a key are the same;
fields before any heading belong to a thread titled with the session's name;
and a line whose key mentions "sam" or "needs" is surfaced as an ask even when
the key is spelled some other way. ``session:`` and ``updated:`` lines are
ignored: a session's freshness is the T2 row's own time, which the engine
refreshes on every write, and nothing typed into the body can move it.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

#: A session whose newest update is older than this is greyed out.
STALE_AFTER = timedelta(hours=24)

_HEADING = re.compile(r"^#{1,3}\s+(\S.*)$")
_IGNORED_KEYS = frozenset({"session", "updated"})

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
    #: Titles of entries that could not be read at all; the page names each in one line.
    unreadable: tuple[str, ...] = ()

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



def parse_timestamp(value: str) -> datetime | None:
    """ISO-8601 to an aware UTC datetime; naive means UTC; junk means None."""
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    try:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except OverflowError:  # an offset pushes the instant outside year 1..9999
        return None


def _split_field(line: str) -> tuple[str, str, str] | None:
    """``(normalized key, value, raw lower-case key)``; None when the line has no colon."""
    key, sep, value = line.partition(":")
    if not sep:
        return None
    raw = key.strip().lower()
    return re.sub(r"[\s_-]+", "_", raw), value.strip(), raw


def _is_ask_key(raw_key: str) -> bool:
    """Backstop: a key that mentions Sam or "needs" is an ask however it is spelled."""
    return "sam" in raw_key or "needs" in raw_key


def _new_thread(title: str) -> dict[str, Any]:
    return {"title": title, "refs": [], "state": "", "waiting_on": "", "needs_sam": "", "notes": []}


def _add_ask(cur: dict[str, Any], text: str) -> None:
    cur["needs_sam"] = f"{cur['needs_sam']}; {text}" if has_value(cur["needs_sam"]) else text


def parse_session(name: str, content: str) -> Session:
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
        if heading := _HEADING.match(line):
            flush()
            cur = _new_thread(heading.group(1).strip())
            continue
        field = _split_field(line)
        if field and field[0] in _IGNORED_KEYS:
            continue
        known = field is not None and field[0] in _THREAD_FIELDS
        ask = field is not None and not known and _is_ask_key(field[2]) and has_value(field[1])
        if cur is None:
            if not (known or ask):
                notes.append(line)
                continue
            cur = _new_thread(name or "(unnamed session)")  # fields before any heading
        if field is None:
            cur["notes"].append(line)
        elif known:
            slot = _THREAD_FIELDS[field[0]]
            if slot == "refs":
                cur["refs"].extend(r.strip() for r in field[1].split(",") if r.strip())
            elif slot == "needs_sam":
                if has_value(field[1]):
                    _add_ask(cur, field[1])
                elif not cur["needs_sam"]:
                    cur["needs_sam"] = field[1]
            else:
                cur[slot] = field[1]
        elif ask:
            _add_ask(cur, line)
        else:
            cur["notes"].append(line)
    flush()
    return Session(name=name, threads=tuple(threads), notes=tuple(notes))


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


def _safe_title(entry: Mapping[str, Any]) -> str:
    try:
        return str(entry.get("title", "")) or "(untitled entry)"
    except Exception:  # noqa: BLE001
        return "(untitled entry)"


def build_board(entries: Iterable[Mapping[str, Any]], now: datetime) -> Board:
    """Cards newest first (no usable time sorts last), plus the cross-session needs-you list.

    ``entries`` are T2 rows: ``title``, ``content``, ``timestamp``. A session's
    freshness is the row's own ``timestamp`` and nothing in the body.
    """
    built: list[SessionCard] = []
    unreadable: list[str] = []
    for e in entries:
        try:
            session = parse_session(str(e.get("title", "")), str(e.get("content", "")))
            newest = parse_timestamp(str(e.get("timestamp", "") or ""))
        except Exception:  # noqa: BLE001 -- one bad entry must not take the page down
            unreadable.append(_safe_title(e))
            continue
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
        if has_value(t.needs_sam)
    )
    return Board(cards=tuple(built), needs_you=needs, unreadable=tuple(unreadable))


@dataclass(frozen=True)
class BeadAsk:
    """A needs-sam bead, with the session threads that raise the same decision."""

    bead: Bead
    raised_by: tuple[NeedsItem, ...] = ()


@dataclass(frozen=True)
class Asks:
    """Everything waiting on Sam, each decision once."""

    threads: tuple[NeedsItem, ...]
    beads: tuple[BeadAsk, ...]

    @property
    def count(self) -> int:
        return len(self.threads) + len(self.beads)


def merge_asks(needs_you: Iterable[NeedsItem], beads: Iterable[Bead]) -> Asks:
    """A thread whose refs name a listed bead is that bead's ask, not a second one."""
    bead_list = tuple(beads)
    by_id = {b.id.lower(): [] for b in bead_list}
    lone: list[NeedsItem] = []
    for item in needs_you:
        hits = [r.lower() for r in item.thread.refs if r.lower() in by_id]
        if hits:
            by_id[hits[0]].append(item)
        else:
            lone.append(item)
    return Asks(
        threads=tuple(lone),
        beads=tuple(BeadAsk(b, tuple(by_id[b.id.lower()])) for b in bead_list),
    )


def headline(board: Board, asks: Asks, checked_everything: bool) -> str:
    """The page's one sentence. It claims "nothing" only when both sources were read and a session reported."""
    n = asks.count
    if n:
        lead = "At least " if not checked_everything else ""
        return f"{lead}{n} thing needs you." if n == 1 else f"{lead}{n} things need you."
    if not checked_everything:
        return "Can't tell what needs you right now."
    if not board.cards:
        return "No session has reported yet, so I can't tell what needs you."
    count = len(board.cards)
    noun = "session" if count == 1 else "sessions"
    return f"Nothing needs you. {count} {noun} reported, newest {board.cards[0].age}."


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
