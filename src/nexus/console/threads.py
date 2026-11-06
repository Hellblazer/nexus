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
the key is spelled some other way. ``updated:`` lines are ignored, and the
entry-level ``session:`` line is read for answer routing only: a session's
freshness is the T2 row's own time, which the engine refreshes on every write,
and nothing typed into the body can move it.

Structured asks (RDR-245): an ``ask:`` line opens a block of ask-level lines
(see ``ASK_KEYS``) that ends at the next ``ask:``, a heading or a thread field.
Inside a block the Sam-mention backstop is off. ``ask_version``, ``classify``
and ``lint`` are pure functions over parsed entries; the caller supplies the
clock and the answer records.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

#: A session whose newest update is older than this is greyed out.
STALE_AFTER = timedelta(hours=24)

_HEADING = re.compile(r"^#{1,3}\s+(\S.*)$")
_IGNORED_KEYS = frozenset({"updated"})

_NO_VALUES = frozenset({"", "none", "no", "n/a", "-", "—"})
_THREAD_FIELDS = {
    "ref": "refs",
    "refs": "refs",
    "state": "state",
    "waiting_on": "waiting_on",
    "needs_sam": "needs_sam",
}

#: Ask-level keys. Only inside an ``ask:`` block are they ask fields.
ASK_KEYS = frozenset({
    "ask", "asked", "question", "context", "option", "rec", "why", "reversible", "blocking", "after", "by",
})
_ID = re.compile(r"[a-z0-9-]{1,40}")
#: Trailing marker, any case, with or without a space before it: missing an irreversible option costs more
#: than flagging one too many.
_IRREVERSIBLE = re.compile(r"\s*\[irreversible\]\s*$", re.IGNORECASE)
_REC = re.compile(r"[0-9]{1,3}")
_POINTER = re.compile(r"ask\s+(\S+)", re.IGNORECASE)


@dataclass(frozen=True)
class Option:
    n: int  #: 1..n by position
    text: str  #: without the trailing ``[irreversible]`` marker
    irreversible: bool = False


@dataclass(frozen=True)
class Ask:
    """One structured ask (RDR-245), read from an ``ask:`` block.

    ``version`` is the hash of the block's ask lines except ``by:``; ``lines``
    are those lines plus ``by:``, stripped, in file order. Fields the block
    leaves out stay empty (``reversible`` and ``blocking`` read False) and the
    required ones are named in ``missing``.
    """

    id: str
    asked: str = ""
    question: str = ""
    context: str = ""
    options: tuple[Option, ...] = ()
    rec: int | None = None
    why: str = ""
    reversible: bool = False
    blocking: bool = False
    after: str = ""
    by: str = ""
    version: str = ""
    entry: str = ""  #: the T2 title of the entry holding the ask
    session_id: str = ""  #: the asker, from the entry's ``session:`` line
    thread: str = ""  #: title of the thread holding the ask
    refs: tuple[str, ...] = ()  #: that thread's refs
    lines: tuple[str, ...] = ()
    rec_raw: str = ""
    missing: tuple[str, ...] = ()
    repeated: tuple[str, ...] = ()  #: single-valued keys given more than once (the first wins)
    bad_values: tuple[str, ...] = ()  #: yes/no keys holding something else, and a ``rec:`` that is not 1-3 digits

    @property
    def conditional(self) -> bool:
        """True when ``after:`` holds a value: the ask is never answerable until the asker deletes the line.

        An empty ``after:``, or one reading "none", is not a condition (``has_value``).
        """
        return has_value(self.after)


@dataclass(frozen=True)
class Thread:
    title: str
    refs: tuple[str, ...] = ()
    state: str = ""
    waiting_on: str = ""
    needs_sam: str = ""
    notes: tuple[str, ...] = ()
    asks: tuple[Ask, ...] = ()


@dataclass(frozen=True)
class Session:
    name: str
    threads: tuple[Thread, ...] = ()
    notes: tuple[str, ...] = ()
    session_id: str = ""


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
    #: One item per thread holding structured asks (RDR-245). ``classify`` lists the asks themselves;
    #: this exists so ``merge_asks`` can fold a thread into the needs-sam bead its refs name.
    structured: tuple[NeedsItem, ...] = ()

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
    """Outcome of the needs-sam bead query: the beads, or one line saying why not.

    ``skipped`` is not a failure: the source does not exist on this machine (no
    ``bd``), so there is nothing to read and nothing the board is missing.
    """

    beads: tuple[Bead, ...]
    error: str = ""
    skipped: str = ""

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


def _mentions_sam(line: str) -> bool:
    """A free-text line naming Sam (no colon, or an unknown key) is treated as an ask, never only a note."""
    return re.search(r"\bsam\b", line, re.IGNORECASE) is not None


def ask_version(lines: Iterable[str]) -> str:
    """The version of an ask: the first 16 hex characters of a SHA-256 over its lines.

    Algorithm, exactly:

    1. ``lines`` are the block's ask-key lines in file order, the ``ask:`` line first. The parser passes only
       those: a non-ask line inside the block (a note) and an ``updated:`` or ``session:`` line are not in it.
    2. Strip leading and trailing whitespace from each line. Nothing else is normalized: the case of a key,
       the spaces inside a line and the value are hashed as typed, so ``Question:`` and ``question:`` differ and
       so do ``question:   q`` and ``question: q``.
    3. Drop every line whose key is ``by``. The key is compared the way the parser reads keys: text before the
       first colon, stripped, lower-cased, runs of space, ``_`` and ``-`` read as one ``_``. So ``by :`` and
       ``By:`` are dropped too.
    4. Join what is left with ``"\\n"``, encode as UTF-8, SHA-256, take the first 16 hex characters.

    An empty ``after:`` line is an ask line and is hashed. Pinned by ``tests/fixtures/asks/version_vectors.json``.
    """
    kept = []
    for line in lines:
        text = line.strip()
        field = _split_field(text)
        if field is not None and field[0] == "by":
            continue
        kept.append(text)
    return hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()[:16]


def _yes_no(value: str) -> bool:
    return value.strip().lower() == "yes"


def _build_ask(
    block: list[tuple[str, str, str]], *, entry: str, session_id: str, thread: str, refs: tuple[str, ...]
) -> Ask:
    """``block`` is ``(normalized key, value, stripped line)`` in file order, the ``ask:`` line first."""
    first: dict[str, str] = {}
    seen: set[str] = set()
    repeated: list[str] = []
    options: list[Option] = []
    for key, value, _line in block:
        if key == "option":
            marked = _IRREVERSIBLE.search(value)
            text = value[: marked.start()].strip() if marked else value
            options.append(Option(len(options) + 1, text, marked is not None))
            continue
        if key in seen:
            if key not in repeated:
                repeated.append(key)
            continue
        seen.add(key)
        first[key] = value
    rec_raw = first.get("rec", "")
    bad = [k for k in ("reversible", "blocking") if first.get(k) and first[k].strip().lower() not in ("yes", "no")]
    if rec_raw and not _REC.fullmatch(rec_raw):
        bad.append("rec")
    required = ["asked", "question", "context", "reversible", "blocking"]
    if rec_raw:
        required.append("why")
    return Ask(
        id=first.get("ask", ""),
        asked=first.get("asked", ""),
        question=first.get("question", ""),
        context=first.get("context", ""),
        options=tuple(options),
        rec=int(rec_raw) if _REC.fullmatch(rec_raw) else None,
        why=first.get("why", ""),
        # a repeated key fails safe: reversible reads no, blocking reads yes (the lint also flags it)
        reversible=_yes_no(first.get("reversible", "")) and "reversible" not in repeated,
        blocking=_yes_no(first.get("blocking", "")) or "blocking" in repeated,
        after=first.get("after", ""),
        by=first.get("by", ""),
        version=ask_version(line for _k, _v, line in block),
        entry=entry,
        session_id=session_id,
        thread=thread,
        refs=refs,
        lines=tuple(line for _k, _v, line in block),
        rec_raw=rec_raw,
        missing=tuple(k for k in required if not first.get(k)),
        repeated=tuple(repeated),
        bad_values=tuple(bad),
    )


def _new_thread(title: str) -> dict[str, Any]:
    return {"title": title, "refs": [], "state": "", "waiting_on": "", "needs_sam": "", "notes": [],
            "ask_next": False, "ops": [], "block": None, "blocks": []}


def _apply(current: str, kind: str, text: str) -> str:
    """One step of the legacy ``needs_sam`` string: ``add`` appends an ask, ``set`` keeps a "none"-style value."""
    if kind == "add":
        return f"{current}; {text}" if has_value(current) else text
    return current or text


def _add_ask(cur: dict[str, Any], text: str, droppable: tuple[str, str] | None = None) -> None:
    cur["ops"].append(("add", text, droppable))
    cur["needs_sam"] = _apply(cur["needs_sam"], "add", text)


def _pointer(text: str) -> tuple[str, str] | None:
    """``needs_sam: ask <id>``: droppable when the thread turns out to hold that ask."""
    m = _POINTER.fullmatch(text.strip())
    return ("pointer", m.group(1).lower()) if m else None


def parse_session(name: str, content: str) -> Session:
    notes: list[str] = []
    threads: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    session_id = ""

    def close_block() -> None:
        if cur is not None and cur["block"] is not None:
            cur["blocks"].append(cur["block"])
            cur["block"] = None

    def flush() -> None:
        if cur is not None:
            close_block()
            threads.append(cur)

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if heading := _HEADING.match(line):
            flush()
            cur = _new_thread(heading.group(1).strip())
            continue
        field = _split_field(line)
        if field and field[0] == "session":
            session_id = session_id or field[1]
            continue
        if field and field[0] in _IGNORED_KEYS:
            continue
        known = field is not None and field[0] in _THREAD_FIELDS
        if known and cur is not None:
            close_block()
        # Only `ask: <id>` opens a block; `ask: Sam please pick the port` is prose and parses as it always did,
        # as does an `ask:` line that is the value of a `needs_sam:` left empty on the line above.
        if (
            field is not None
            and field[0] == "ask"
            and _ID.fullmatch(field[1])
            and not (cur is not None and cur["ask_next"])
        ):
            if cur is None:
                cur = _new_thread(name or "(unnamed session)")
            close_block()
            cur["ask_next"] = False
            cur["block"] = [("ask", field[1], line)]
            continue
        if cur is not None and cur["block"] is not None:
            # inside an ask block the Sam-mention backstop is off
            if field is not None and field[0] in ASK_KEYS and field[0] != "ask":
                cur["block"].append((field[0], field[1], line))
            else:
                cur["notes"].append(line)
            continue
        ask = field is not None and not known and _is_ask_key(field[2]) and has_value(field[1])
        # an ask key answered "none" stays a non-ask; only other text naming Sam is surfaced
        free_ask = not known and not (field is not None and _is_ask_key(field[2])) and _mentions_sam(line)
        if cur is None:
            if not (known or ask or free_ask):
                notes.append(line)
                continue
            cur = _new_thread(name or "(unnamed session)")  # fields before any heading
        if cur["ask_next"] and not known:
            # "needs_sam:" with its value on the next line
            cur["ask_next"] = False
            _add_ask(cur, line, _pointer(line))
            continue
        cur["ask_next"] = False
        if free_ask:
            _add_ask(cur, line)
        elif field is None:
            cur["notes"].append(line)
        elif known:
            slot = _THREAD_FIELDS[field[0]]
            if slot == "refs":
                cur["refs"].extend(r.strip() for r in field[1].split(",") if r.strip())
            elif slot == "needs_sam":
                if has_value(field[1]):
                    _add_ask(cur, field[1], _pointer(field[1]))
                elif not field[1]:
                    cur["ask_next"] = True
                else:
                    cur["ops"].append(("set", field[1], None))
                    cur["needs_sam"] = _apply(cur["needs_sam"], "set", field[1])
            else:
                cur[slot] = field[1]
                if slot == "waiting_on" and _mentions_sam(field[1]):
                    _add_ask(cur, f"waiting on: {field[1]}", ("waiting", "") if field[1].strip().lower() == "sam" else None)
        elif ask:
            _add_ask(cur, line)
        else:
            cur["notes"].append(line)
    flush()
    return Session(
        name=name,
        threads=tuple(_finish_thread(t, name, session_id) for t in threads),
        notes=tuple(notes),
        session_id=session_id,
    )


def _finish_thread(t: dict[str, Any], entry: str, session_id: str) -> Thread:
    refs = tuple(t["refs"])
    asks = tuple(
        _build_ask(b, entry=entry, session_id=session_id, thread=t["title"], refs=refs) for b in t["blocks"]
    )
    needs_sam = t["needs_sam"]
    if asks:
        # a pointer to one of this thread's asks, and "waiting_on: sam", are state, not legacy asks
        ids = {a.id.lower() for a in asks}
        kept = [
            op for op in t["ops"]
            if not (op[2] is not None and (op[2][0] == "waiting" or op[2][1] in ids))
        ]
        if len(kept) != len(t["ops"]):
            needs_sam = ""
            for kind, text, _drop in kept:
                needs_sam = _apply(needs_sam, kind, text)
    return Thread(
        title=t["title"],
        refs=refs,
        state=t["state"],
        waiting_on=t["waiting_on"],
        needs_sam=needs_sam,
        notes=tuple(t["notes"]),
        asks=asks,
    )


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
    structured = tuple(
        NeedsItem(c.session.name, t, c.age)
        for c in built
        for t in c.session.threads
        if t.asks
    )
    return Board(cards=tuple(built), needs_you=needs, unreadable=tuple(unreadable), structured=structured)


@dataclass(frozen=True)
class BeadAsk:
    """A needs-sam bead, with the session threads that raise the same decision."""

    bead: Bead
    raised_by: tuple[NeedsItem, ...] = ()
    #: ``<entry>/<ask id>`` of each structured ask folded into this bead. The ask itself stays in its
    #: own ``classify`` group; this is only the pointer from the bead to it.
    ask_pointers: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpenAsk:
    """A structured ask that no listed bead absorbed, with who asked and how long ago the entry was written."""

    ask: Ask
    session: str
    age: str


@dataclass(frozen=True)
class Asks:
    """Everything waiting on Sam, each decision once."""

    threads: tuple[NeedsItem, ...]
    beads: tuple[BeadAsk, ...]
    #: Structured asks not folded into a bead and not conditional. Answered asks are not removed here: the
    #: board has no answer records; ``classify`` does.
    structured: tuple[OpenAsk, ...] = ()

    @property
    def count(self) -> int:
        return len(self.threads) + len(self.beads) + len(self.structured)


def merge_asks(
    needs_you: Iterable[NeedsItem], beads: Iterable[Bead], structured: Iterable[NeedsItem] = ()
) -> Asks:
    """A thread whose refs name a listed bead is that bead's ask, not a second one.

    ``structured`` (``Board.structured``) folds the same way by the same rule: the thread joins the
    bead's ``raised_by`` once and each of its asks adds an ``ask_pointers`` entry. A structured ask
    that matches no bead is returned in ``Asks.structured`` (and counted) unless it is conditional;
    ``classify`` gives the full classes.
    """
    bead_list = tuple(beads)
    by_id = {b.id.lower(): [] for b in bead_list}
    pointers: dict[str, list[str]] = {b.id.lower(): [] for b in bead_list}
    lone: list[NeedsItem] = []
    open_asks: list[OpenAsk] = []
    for item in needs_you:
        hits = [r.lower() for r in item.thread.refs if r.lower() in by_id]
        if hits:
            by_id[hits[0]].append(item)
        else:
            lone.append(item)
    for item in structured:
        hits = [r.lower() for r in item.thread.refs if r.lower() in by_id]
        if not hits:
            open_asks.extend(OpenAsk(a, item.session, item.age) for a in item.thread.asks if not a.conditional)
            continue
        if not any(prior.thread is item.thread for prior in by_id[hits[0]]):
            by_id[hits[0]].append(item)
        pointers[hits[0]].extend(f"{a.entry}/{a.id}" for a in item.thread.asks)
    return Asks(
        threads=tuple(lone),
        beads=tuple(
            BeadAsk(b, tuple(by_id[b.id.lower()]), tuple(pointers[b.id.lower()])) for b in bead_list
        ),
        structured=tuple(open_asks),
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


# --- structured asks: classification and lint (RDR-245) ---------------------------------


@dataclass(frozen=True)
class ClassedAsk:
    ask: Ask
    group: str
    #: "entry N h old" on a blocking ask in a greyed entry; "entry age unknown" when the row has no usable time.
    mark: str = ""


@dataclass(frozen=True)
class Classification:
    """Every parsed ask in exactly one group; each group is sorted by ``by:`` (earliest first, none last)."""

    interrupt: tuple[ClassedAsk, ...] = ()
    batch: tuple[ClassedAsk, ...] = ()
    quiet: tuple[ClassedAsk, ...] = ()
    conditional: tuple[ClassedAsk, ...] = ()
    answered: tuple[ClassedAsk, ...] = ()
    #: Titles of entries that could not be read; their asks are in no group, so a caller must say so.
    unreadable: tuple[str, ...] = ()


@dataclass(frozen=True)
class Finding:
    """One lint result. ``ask`` is empty for a finding about the entry itself."""

    entry: str
    ask: str
    rule: str
    message: str


def answer_title(ask: Ask) -> str:
    """T2 title of the answer record for this ask at this version: ``<asker session id>/<ask id>/<version>``.

    The session id is lower-cased, the canonical form records are written under. An ask in an entry with no
    ``session:`` line has no title (empty): no record can exist for it.
    """
    if not ask.session_id:
        return ""
    return f"{ask.session_id.lower()}/{ask.id}/{ask.version}"


def _record_status(record: Any) -> str:
    if isinstance(record, str):
        try:
            record = json.loads(record)
        except ValueError:
            return ""
    if not isinstance(record, Mapping):
        return ""
    if "status" not in record and "content" in record:
        return _record_status(record["content"])
    status = record.get("status", "")
    return status if isinstance(status, str) else ""


def _record_statuses(records: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Status by record title. Records are a title-keyed mapping or T2 rows with ``title`` and JSON ``content``."""
    pairs = records.items() if isinstance(records, Mapping) else (
        (r.get("title", ""), r) for r in records if isinstance(r, Mapping)
    )
    return {str(title): _record_status(rec) for title, rec in pairs}


def _read_entries(
    entries: Iterable[Mapping[str, Any]],
) -> tuple[list[tuple[str, Session, datetime | None]], list[str]]:
    """Parsed entries, and the titles of those that could not be read at all (never silently dropped)."""
    out: list[tuple[str, Session, datetime | None]] = []
    unreadable: list[str] = []
    for e in entries:
        try:
            title = str(e.get("title", ""))
            content = str(e.get("content", ""))
            stamp = parse_timestamp(str(e.get("timestamp", "") or ""))
            out.append((title, parse_session(title, content), stamp))
        except Exception:  # noqa: BLE001 -- one bad entry must not take the listing down
            unreadable.append(_safe_title(e))
    return out, unreadable


def _by_key(ask: Ask) -> tuple[int, datetime]:
    when = parse_timestamp(ask.by)
    return (0, when) if when is not None else (1, datetime.min.replace(tzinfo=timezone.utc))


def classify(
    entries: Iterable[Mapping[str, Any]],
    now: datetime,
    records: Mapping[str, Any] | Iterable[Mapping[str, Any]] = (),
) -> Classification:
    """Put every ask of every entry in one group; the first rule that applies wins.

    conditional (``after:`` set) > answered (a ``sent`` record for this exact version) > interrupt
    (``blocking: yes``, marked when the entry is greyed) > quiet (not blocking, entry greyed) > batch.
    ``by:`` only orders a group.
    """
    statuses = _record_statuses(records)
    groups: dict[str, list[ClassedAsk]] = {g: [] for g in ("interrupt", "batch", "quiet", "conditional", "answered")}
    read, unreadable = _read_entries(entries)
    for _title, session, stamp in read:
        delta = None if stamp is None else now - stamp
        greyed = delta is None or delta > STALE_AFTER
        for thread in session.threads:
            for ask in thread.asks:
                mark = ""
                if ask.conditional:
                    group = "conditional"
                elif ask.session_id and statuses.get(answer_title(ask)) == "sent":
                    group = "answered"
                elif ask.blocking:
                    group = "interrupt"
                    if greyed:
                        mark = "entry age unknown" if delta is None else f"entry {int(delta.total_seconds() // 3600)} h old"
                elif greyed:
                    group = "quiet"
                else:
                    group = "batch"
                groups[group].append(ClassedAsk(ask, group, mark))
    return Classification(
        **{g: tuple(sorted(items, key=lambda c: _by_key(c.ask))) for g, items in groups.items()},
        unreadable=tuple(unreadable),
    )


def _unopened_asks(entry: str, thread: Thread) -> list[Finding]:
    """An ``ask:`` line that opened no block (its value is not an id) in a thread that otherwise holds ask lines.

    Prose such as ``ask: Sam please pick the port`` in a legacy thread is left alone; a thread with ask blocks,
    or with other ask-key lines among its notes, shows the writer meant a structured ask.
    """
    keys = [f for f in (_split_field(n) for n in thread.notes) if f is not None]
    structured = bool(thread.asks) or any(k in ASK_KEYS and k != "ask" for k, _v, _r in keys)
    if not structured:
        return []
    return [
        Finding(entry, v, "bad-id", f"id {v!r} is not [a-z0-9-]{{1,40}}, so the ask: block was not opened")
        for k, v, _r in keys
        if k == "ask"
    ]


def lint(entries: Iterable[Mapping[str, Any]]) -> tuple[Finding, ...]:
    """Problems with structured asks, each naming its entry and ask (entry-level findings carry an empty ask)."""
    found: list[Finding] = []
    read, unreadable = _read_entries(entries)
    for title in unreadable:
        found.append(Finding(title, "", "unreadable-entry", "the entry could not be read, so its asks are not checked"))
    by_session: dict[str, list[str]] = {}
    has_asks: dict[str, bool] = {}
    for title, session, _stamp in read:
        asks = [a for t in session.threads for a in t.asks]
        if session.session_id:
            by_session.setdefault(session.session_id.lower(), []).append(title)
        has_asks[title] = has_asks.get(title, False) or bool(asks)
        if asks and not session.session_id:
            found.append(Finding(title, "", "no-session", "no session: line, so answers cannot be routed"))
        for t in session.threads:
            found.extend(_unopened_asks(title, t))
        seen: set[str] = set()
        reported: set[str] = set()
        for a in asks:
            def add(rule: str, message: str, a: Ask = a) -> None:
                found.append(Finding(title, a.id, rule, message))

            if a.id in seen and a.id not in reported:
                reported.add(a.id)
                add("duplicate-id", f"id {a.id!r} is used more than once in this entry")
            seen.add(a.id)
            for field in a.missing:
                add("missing-field", f"missing required field: {field}")
            if len(a.options) < 2:
                add("few-options", f"{len(a.options)} option line(s); an ask needs at least two")
            if a.rec is not None and not 1 <= a.rec <= len(a.options):
                add("rec-range", f"rec: {a.rec_raw} is outside 1..{len(a.options)}")
            if any(not o.text for o in a.options):
                for o in a.options:
                    if not o.text:
                        add("empty-option", f"option {o.n} has no text")
            if a.reversible and any(o.irreversible for o in a.options):
                add("reversible-conflict", "reversible: yes but an option is marked [irreversible]")
            for field in a.bad_values:
                add("bad-value", "rec: must be a number of one to three digits" if field == "rec" else f"{field}: must be yes or no")
            for field in a.repeated:
                add("repeated-field", f"{field}: given more than once; the first one counts")
    for sid, titles in by_session.items():
        if len(titles) > 1 and any(has_asks[t] for t in titles):
            for title in titles:
                found.append(Finding(title, "", "duplicate-session", f"session: {sid} is also carried by another entry"))
    return tuple(found)
