# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ask``: the mediator's tool and Sam's fallback for structured asks (RDR-245 Step 2, nexus-wc4jj.7).

An ask is a structured block in a session's T2 ``threads`` entry (parsed by ``nexus.console.threads``).
``list`` and ``show`` read the board; ``answer`` records Sam's answer in T2 project ``asks`` and mails it
to the asking session; ``digest`` lists what was answered. The record shape and the mail body are the
contract pinned by ``tests/fixtures/asks/``.

Every external effect goes through ``Seams``: the T2 reader/writer, the tuple writer, the TTY probe and the
own-session resolver, plus the clock, the beads reader, the ``cli`` sender id, the answer-id source and the lock
around a batch record's read-modify-write. The click context carries them (``obj=Seams(...)``); a run without them builds the live ones.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, ContextManager, Protocol

import click

from nexus.console.threads import (
    Ask,
    BeadsResult,
    ClassedAsk,
    answer_title,
    build_board,
    classify,
    format_age,
    lint,
    merge_asks,
    parse_session,
    parse_timestamp,
)

THREADS_PROJECT = "threads"
ASKS_PROJECT = "asks"

#: A ``sending`` record older than this is a send that died; a retry needs no ``--replace``.
SENDING_STALE = timedelta(minutes=10)
#: Answers whose batch words match and are newer than this share one batch record.
BATCH_WINDOW = timedelta(minutes=10)
LIST_DIGEST_WINDOW = timedelta(hours=48)
DIGEST_WINDOW = timedelta(days=7)
SAM_WORDS_MAX = 500
BATCH_WORDS_MAX = 2000

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_CANONICAL_UUID = re.compile(_UUID)
_ANSWER_TITLE = re.compile(rf"({_UUID})/([a-z0-9-]{{1,40}})/([0-9a-f]{{16}})")
_NUMBER = re.compile(r"[0-9]+")
_GROUPS = ("interrupt", "batch", "quiet", "conditional", "answered")


# --- seams ------------------------------------------------------------------------------------------


class T2(Protocol):
    """The T2 reader/writer seam; ``HttpMemoryStore`` has these methods."""

    def get_all(self, project: str) -> list[dict[str, Any]]: ...
    def get(self, project: str, title: str) -> dict[str, Any] | None: ...
    def put(self, project: str, title: str, content: str, tags: str = "") -> Any: ...


class LiveT2:
    """The CLI's own T2 handle, opened per call (``nexus.commands._helpers.t2_handle``)."""

    def get_all(self, project: str) -> list[dict[str, Any]]:
        from nexus.commands._helpers import t2_handle  # noqa: PLC0415 -- deferred: CLI startup cost

        with t2_handle() as db:
            return list(db.memory.get_all(project=project))

    def get(self, project: str, title: str) -> dict[str, Any] | None:
        from nexus.commands._helpers import t2_handle  # noqa: PLC0415 -- deferred: CLI startup cost

        with t2_handle() as db:
            return db.memory.get(project=project, title=title)

    def put(self, project: str, title: str, content: str, tags: str = "") -> Any:
        from nexus.commands._helpers import t2_handle  # noqa: PLC0415 -- deferred: CLI startup cost

        with t2_handle() as db:
            return db.memory.put(project=project, title=title, content=content, tags=tags)


def live_tuple_out(subspace: str, keys: dict[str, str], dims: dict[str, str], body: str, nonce: str) -> str:
    """Raw ``tuple_out``, the path ``nx tuple out`` uses. Not ``mailbox_send``: its own-``from`` check refuses
    the CLI's sender id."""
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 -- deferred: CLI startup cost

    return HttpTupleStore().out(subspace, keys, dims, body, nonce=nonce)


def live_tty() -> str | None:
    """The terminal name when stdin is a TTY (``isatty`` also works on Windows), else None."""
    try:
        if not sys.stdin.isatty():
            return None
    except (AttributeError, ValueError):
        return None
    try:
        return os.ttyname(sys.stdin.fileno())
    except (AttributeError, OSError, ValueError):
        return "tty"


def live_session() -> str:
    """This session's own id the way ``mailbox_send`` finds it (tuple-watch marker, then ``NX_T1_SESSION_ID``, then ``NX_SESSION_ID``);
    empty when nothing resolves."""
    from nexus.tuple_directory import resolve_default_from  # noqa: PLC0415 -- deferred: CLI startup cost

    try:
        return resolve_default_from()
    except Exception:  # noqa: BLE001 -- "resolves nothing" is a refusal upstream, never a traceback
        return ""


def live_beads() -> BeadsResult:
    from nexus.console.routes.threads import run_bd_needs_sam  # noqa: PLC0415 -- deferred: console import is heavy

    return run_bd_needs_sam()


LOCK_WAIT = 5.0  #: seconds ``live_lock`` waits for a holder before giving up


@contextlib.contextmanager
def live_lock(timeout: float = LOCK_WAIT) -> Iterator[None]:
    """Exclusive advisory lock (``nexus._locking``) on ``ask-answer.lock`` in the nexus config dir.

    It serializes the read-modify-write of a batch record across ``nx ask answer`` processes on this machine.
    A holder that dies releases it (the OS drops the lock with the descriptor). Waits at most ``timeout`` seconds,
    then raises ``TimeoutError``: a hung holder must not stall a process whose mail already went out. A lock file
    that cannot be opened is a warning, not a failure: the block runs unlocked, since a lost batch record costs
    more than the narrow race the lock closes.
    """
    from nexus import config as _nx_config  # noqa: PLC0415 -- deferred: CLI startup cost
    from nexus._locking import lock_fd, unlock_fd  # noqa: PLC0415 -- deferred: CLI startup cost

    path = _nx_config.nexus_config_dir() / "ask-answer.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as exc:
        click.echo(_out(f"warning: cannot open the answer lock at {path}: {exc}; continuing without it"), err=True)
        yield
        return
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                lock_fd(fd, blocking=False)  # lifecycle-gate-allow: a bounded mutex over one batch record's read-modify-write, not a lifecycle election
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"the answer lock {path} is held by another process") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            unlock_fd(fd)
    finally:
        os.close(fd)


def default_cli_sender() -> str:
    """The ``from`` of a ``--via cli`` mail: a UUID minted once, kept in the nexus config dir.

    An empty or invalid file counts as absent and is replaced. The write goes to a temp file and
    ``os.replace`` (atomic on Windows too), so a crash never leaves a half-written id.
    """
    from nexus import config as _nx_config  # noqa: PLC0415 -- deferred: CLI startup cost

    path = _nx_config.nexus_config_dir() / "ask-cli-sender"

    def kept() -> str | None:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return None
        return value if _CANONICAL_UUID.fullmatch(value) else None

    if (value := kept()) is not None:
        return value
    fresh = str(uuid.uuid4())
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(fresh + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise click.ClickException(f"cannot keep the cli sender id at {path}: {exc}") from exc
    return kept() or fresh


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_answer_id() -> str:
    return secrets.token_hex(8)


@dataclass(frozen=True)
class Seams:
    t2: T2 = field(default_factory=LiveT2)
    tuple_out: Callable[[str, dict[str, str], dict[str, str], str, str], str] = live_tuple_out
    tty: Callable[[], str | None] = live_tty
    session: Callable[[], str] = live_session
    now: Callable[[], datetime] = _utcnow
    beads: Callable[[], BeadsResult] = live_beads
    cli_sender: Callable[[], str] = default_cli_sender
    answer_id: Callable[[], str] = _new_answer_id
    lock: Callable[[], ContextManager[None]] = live_lock


def _seams(ctx: click.Context) -> Seams:
    found = ctx.find_object(Seams)
    if found is None:
        found = ctx.meta.setdefault("nx.ask.seams", Seams())
    return found


# --- reading the board ------------------------------------------------------------------------------


def _fmt_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_obj(raw: Any) -> dict[str, Any] | None:
    """A T2 row's JSON content as an object; None when it is not one."""
    content = raw.get("content") if isinstance(raw, dict) else raw
    if not isinstance(content, str):
        return None
    try:
        obj = json.loads(content)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _read_rows(s: Seams) -> list[dict[str, Any]]:
    return [r for r in s.t2.get_all(project=THREADS_PROJECT) if isinstance(r, dict)]


def _all_asks(rows: list[dict[str, Any]]) -> list[tuple[Ask, datetime | None]]:
    """Every ask of every readable entry, with the entry row's own time."""
    found: list[tuple[Ask, datetime | None]] = []
    for row in rows:
        try:
            title = str(row.get("title", ""))
            session = parse_session(title, str(row.get("content", "")))
            stamp = parse_timestamp(str(row.get("timestamp", "") or ""))
        except Exception:  # noqa: BLE001 -- one bad entry must not take the command down; lint names it
            continue
        found.extend((a, stamp) for t in session.threads for a in t.asks)
    return found


def _session_of(row: dict[str, Any]) -> str:
    try:
        return parse_session(str(row.get("title", "")), str(row.get("content", ""))).session_id.lower()
    except Exception:  # noqa: BLE001 -- an unreadable entry carries no session we can name
        return ""


def _ref(ask: Ask) -> str:
    return f"{ask.entry}/{ask.id}"


def _resolve(asks: list[tuple[Ask, datetime | None]], ref: str) -> tuple[Ask, datetime | None]:
    """``<entry>/<id>``; a bare ``<id>`` only when exactly one entry carries it."""
    ref = ref.strip()
    if "/" in ref:
        entry, _, ask_id = ref.rpartition("/")
        hits = [a for a in asks if a[0].entry == entry and a[0].id == ask_id]
    else:
        hits = [a for a in asks if a[0].id == ref]
    if not hits:
        raise _refuse(f"no ask {ref!r} on the board (nx ask list names them)")
    if len(hits) > 1:
        names = ", ".join(_c(_ref(a)) for a, _ in hits)
        if "/" in ref:
            raise _refuse(f"{ref!r} names more than one ask in that entry: {names}")
        raise _refuse(f"{ref!r} is held by more than one entry; name one of: {names}")
    return hits[0]


def _option_text(ask: Ask, n: int) -> str:
    o = ask.options[n - 1]
    return o.text + (" [irreversible]" if o.irreversible else "")


def _ask_json(ask: Ask, group: str = "", mark: str = "") -> dict[str, Any]:
    return {
        "ref": _ref(ask),
        "id": ask.id,
        "entry": ask.entry,
        "version": ask.version,
        "session_id": ask.session_id,
        "thread": ask.thread,
        "refs": list(ask.refs),
        "asked": ask.asked,
        "question": ask.question,
        "context": ask.context,
        "options": [{"n": o.n, "text": o.text, "irreversible": o.irreversible} for o in ask.options],
        "rec": ask.rec,
        "rec_raw": ask.rec_raw,
        "why": ask.why,
        "reversible": ask.reversible,
        "blocking": ask.blocking,
        "after": ask.after,
        "by": ask.by,
        "conditional": ask.conditional,
        "lines": list(ask.lines),
        "missing": list(ask.missing),
        "repeated": list(ask.repeated),
        "bad_values": list(ask.bad_values),
        "group": group,
        "mark": mark,
    }


def _width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def _fit(line: str, width: int) -> str:
    line = " ".join(line.split()) if "\n" in line else line
    return line if len(line) <= width else line[: max(width - 1, 1)] + "…"


#: C0, DEL and C1 controls (an ESC sequence or a carriage return in a field can repaint a terminal), plus the
#: bidi controls and zero-width characters that make text read differently from what it is.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_LINE_BREAKS = frozenset("\t\n\r\x0b\x0c\x1c\x1d\x1e\x85")


def _c(value: Any) -> str:
    """One display field: controls dropped, tabs and line breaks turned into a space."""
    return _CONTROL.sub(lambda m: " " if m.group() in _LINE_BREAKS else "", str(value))


def _out(value: str) -> str:
    """Final pass over text output: every control character except newline and tab dropped."""
    return _CONTROL.sub(lambda m: m.group() if m.group() in "\n\t" else "", value)


def _say(value: str) -> None:
    click.echo(_out(value))


def _one_line(value: str) -> str:
    return " ".join(_c(value).split())


# --- answer records and the digest ------------------------------------------------------------------


def _answer_rows(s: Seams) -> list[dict[str, Any]]:
    return [r for r in s.t2.get_all(project=ASKS_PROJECT) if isinstance(r, dict)]


def _answer_entries(
    rows: list[dict[str, Any]], now: datetime, window: timedelta
) -> tuple[list[dict[str, Any]], list[str]]:
    """Answer records inside ``window``, newest first, each with the words of the batch it came from; and the
    titles of the rows that are neither a readable answer nor a readable batch record.

    A row is skipped, not hidden: the digest is the audit trail, so a record it cannot read is named. An
    unreadable row whose own T2 time is older than the window is left out of that list.
    """
    batches: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []

    def skip(row: dict[str, Any]) -> None:
        stamp = parse_timestamp(str(row.get("timestamp", "") or ""))
        if stamp is None or now - stamp <= window:
            skipped.append(str(row.get("title", "")))

    for row in rows:
        title = str(row.get("title", ""))
        if title.startswith("batch/"):
            if (rec := _json_obj(row)) is not None:
                batches[title] = rec
            else:
                skip(row)
    out: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        title = str(row.get("title", ""))
        if title.startswith("batch/"):
            continue
        m = _ANSWER_TITLE.fullmatch(title)
        rec = _json_obj(row) if m else None
        if m is None or rec is None:
            skip(row)
            continue
        when = parse_timestamp(str(rec.get("time", ""))) or parse_timestamp(str(row.get("timestamp", "") or ""))
        if when is None:
            skipped.append(title)
            continue
        if now - when > window:
            continue
        batch = batches.get(str(rec.get("batch_title") or ""))
        out.append((when, {
            "title": title,
            "asker": m.group(1),
            "ask": m.group(2),
            "version": m.group(3),
            "time": str(rec.get("time", "")),
            "via": rec.get("via", ""),
            "choice": rec.get("choice"),
            "note": rec.get("note", ""),
            "sam_words": rec.get("sam_words", ""),
            "batch_words": batch.get("batch_words", "") if batch else "",
            "tuple_id": rec.get("tuple_id"),
            "status": rec.get("status", ""),
            "error": rec.get("error"),
        }))
    out.sort(key=lambda p: p[0], reverse=True)
    return [e for _, e in out], sorted(skipped)


_SKIPPED_SHOWN = 5


def _skipped_line(skipped: list[str]) -> list[str]:
    """One line naming rows of T2 project ``asks`` the digest could not read; nothing when there are none."""
    if not skipped:
        return []
    shown = ", ".join(_c(t) for t in skipped[:_SKIPPED_SHOWN]) + (", ..." if len(skipped) > _SKIPPED_SHOWN else "")
    noun = "row" if len(skipped) == 1 else "rows"
    return [f"skipped {len(skipped)} {noun} in T2 project asks that are not readable answer or batch records: {shown}"]


def _digest_lines(entries: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for e in entries:
        choice = "none" if e["choice"] is None else _c(e["choice"])
        head = f"  {_c(e['time'])}  {_c(e['via'])}  {e['ask']}@{e['version']}  choice {choice}"
        tail = (
            f"tuple {_c(e['tuple_id'])}" if e["status"] == "sent"
            else f"[{_c(e['status'])}{': ' + _c(e['error']) if e['error'] else ''}]"
        )
        lines.append(f'{head}  "{_one_line(str(e["sam_words"]))}"  {tail}')
        if e["batch_words"]:
            lines.append(f'      batch: "{_one_line(str(e["batch_words"]))}"')
    return lines


# --- list -------------------------------------------------------------------------------------------


def _answer_mark(records: dict[str, dict[str, Any]], ask: Ask) -> str:
    """What the list shows of an answer record that is not ``sent``: a send in progress, a failure, or junk."""
    status = _answer_status(records, ask)
    if status in ("", "sent"):
        return ""
    if status == "unreadable":
        return "answer record unreadable"
    if status == "sending" and _is_outcome_unknown(_json_obj(records[answer_title(ask)])):
        return "answer sending: outcome unknown"
    return f"answer {status}"


def _answer_status(records: dict[str, dict[str, Any]], ask: Ask) -> str:
    """The status of the answer record for this ask version: sending, sent, failed, unreadable, or empty (none)."""
    title = answer_title(ask)
    row = records.get(title) if title else None
    if row is None:
        return ""
    rec = _json_obj(row)
    status = rec.get("status") if rec else None
    return status if isinstance(status, str) and status in _ANSWER_STATUS else "unreadable"


def _group_line(c: ClassedAsk, width: int, records: dict[str, dict[str, Any]]) -> str:
    a = c.ask
    meta = "".join(f" [{m}]" for m in ((f"by {a.by}" if a.by else ""), c.mark, _answer_mark(records, a)) if m)
    return _fit(_c(f"  {_ref(a)}  {a.version}{meta}  {a.question}"), width)


@click.group(name="ask")
def ask_group() -> None:
    """Structured asks on the threads board: list / show / answer / digest (RDR-245)."""


@ask_group.command(name="list")
@click.option("--json", "as_json", is_flag=True, help="One JSON object: every group with every parsed field and the version.")
@click.option("--all", "show_all", is_flag=True, help="List the quiet, conditional and answered asks, not only count them.")
@click.pass_context
def list_cmd(ctx: click.Context, as_json: bool, show_all: bool) -> None:
    """What waits on Sam: interrupts, the batch, counts, legacy asks and beads, lint, recent answers."""
    s = _seams(ctx)
    now = s.now()
    rows = _read_rows(s)
    answer_rows = _answer_rows(s)  # one read serves both the classes and the digest
    records = {str(r.get("title", "")): r for r in answer_rows}
    classes = classify(rows, now, records)
    board = build_board(rows, now)
    beads = s.beads()
    merged = merge_asks(board.needs_you, beads.beads, board.structured, records)
    findings = lint(rows)
    digest, skipped = _answer_entries(answer_rows, now, LIST_DIGEST_WINDOW)
    if as_json:
        click.echo(json.dumps(_list_json(classes, merged, beads, findings, digest, board.unreadable, records, skipped), indent=2))
        return
    width = _width()
    out: list[str] = []
    for name in ("interrupt", "batch"):
        group = getattr(classes, name)
        out.append(f"{name.capitalize()} ({len(group)})")
        out.extend(_group_line(c, width, records) for c in group)
    counts = ", ".join(f"{n} {len(getattr(classes, n))}" for n in ("quiet", "conditional", "answered"))
    out.append(f"Also: {counts}")
    if show_all:
        for name in ("quiet", "conditional", "answered"):
            group = getattr(classes, name)
            if group:
                out.append(f"{name.capitalize()} ({len(group)})")
                out.extend(_group_line(c, width, records) for c in group)
    if classes.malformed:
        out.append(f"Malformed asks ({len(classes.malformed)}): no block opened, so they cannot be answered")
        out.extend(
            _fit(_c(f"  {u.entry}/{u.id or '(no id)'}: {u.reason}" + (f": {u.question}" if u.question else "")), width)
            for u in classes.malformed
        )
    if merged.threads:
        out.append(f"Legacy asks ({len(merged.threads)})")
        out.extend(
            _fit(_c(f"  {i.session} / {i.thread.title}: {i.thread.needs_sam} ({i.age})"), width) for i in merged.threads
        )
    if merged.beads or beads.error:
        out.append(f"Needs-sam beads ({len(merged.beads)})")
        if beads.error:
            out.append(_c(f"  could not be read: {beads.error}"))
        for b in merged.beads:
            pointer = f"  -> {', '.join(b.ask_pointers)}" if b.ask_pointers else ""
            prio = f"  P{b.bead.priority}" if b.bead.priority is not None else ""
            out.append(_fit(_c(f"  {b.bead.id}{prio}{pointer}  {b.bead.title}"), width))
    if findings:
        out.append(f"Lint ({len(findings)})")
        out.extend(_fit(_c(f"  {f.entry}{'/' + f.ask if f.ask else ''}: {f.rule}: {f.message}"), width) for f in findings)
    if classes.unreadable:
        out.append("Unreadable entries (their asks are not listed): " + ", ".join(_c(t) for t in classes.unreadable))
    out.append(f"Digest, last 48 hours ({len(digest)})")
    out.extend(_digest_lines(digest))
    out.extend(_skipped_line(skipped))
    _say("\n".join(out))


def _list_json(classes: Any, merged: Any, beads: BeadsResult, findings: Any, digest: list[dict[str, Any]],
               unreadable: tuple[str, ...], records: dict[str, dict[str, Any]], skipped: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {
        name: [
            {**_ask_json(c.ask, c.group, c.mark), "answer_status": _answer_status(records, c.ask)}
            for c in getattr(classes, name)
        ]
        for name in _GROUPS
    }
    data["legacy"] = [
        {"session": i.session, "thread": i.thread.title, "needs_sam": i.thread.needs_sam, "age": i.age,
         "refs": list(i.thread.refs)}
        for i in merged.threads
    ]
    data["malformed"] = [
        {"entry": u.entry, "thread": u.thread, "id": u.id, "question": u.question, "reason": u.reason}
        for u in classes.malformed
    ]
    data["beads"] = [
        {
            "id": b.bead.id,
            "title": b.bead.title,
            "priority": b.bead.priority,
            "assignee": b.bead.assignee,
            "raised_by": [{"session": i.session, "thread": i.thread.title} for i in b.raised_by],
            "ask_pointers": list(b.ask_pointers),
        }
        for b in merged.beads
    ]
    data["beads_error"] = beads.error
    data["lint"] = [{"entry": f.entry, "ask": f.ask, "rule": f.rule, "message": f.message} for f in findings]
    data["unreadable"] = sorted({*classes.unreadable, *unreadable})
    data["digest"] = digest
    data["digest_skipped"] = skipped
    return data


_ANSWER_STATUS = frozenset({"sending", "sent", "failed"})
#: The start of the ``error`` of a record left at ``sending`` because the ``tuple_out`` outcome is unknown.
OUTCOME_UNKNOWN = "outcome unknown: "
_VIA = re.compile(rf"cli|mediator:{_UUID}")
_ISO_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})")
_HEX16 = re.compile(r"[0-9a-f]{16}")
_TUPLE_ID = re.compile(r"[0-9a-f-]{1,64}")


def _is_outcome_unknown(rec: dict[str, Any] | None) -> bool:
    """A ``sending`` record whose ``error`` says the ``tuple_out`` outcome is unknown (the CLI's own prefix)."""
    if rec is None:
        return False
    error = rec.get("error")
    return rec.get("status") == "sending" and isinstance(error, str) and error.startswith(OUTCOME_UNKNOWN)


def _closed_answer(rec: dict[str, Any] | None) -> dict[str, Any] | None:
    """The answer record's fields that match a closed pattern, or None when any of them does not.

    ``show`` is what an asker runs to check an answer is fresh, so nothing a writer typed freely
    (``sam_words``, ``note``, ``tty``, ``error``, the question and options) is passed on (RDR-245 Decision 10).
    """
    if rec is None:
        return None
    status, via, when, choice = rec.get("status"), rec.get("via"), rec.get("time"), rec.get("choice")
    answer_id, tuple_id = rec.get("answer_id"), rec.get("tuple_id")
    ok = (
        isinstance(status, str) and status in _ANSWER_STATUS
        and isinstance(via, str) and _VIA.fullmatch(via) is not None
        and isinstance(when, str) and _ISO_TIME.fullmatch(when) is not None
        and (choice is None or (type(choice) is int and 1 <= choice <= 999))
        and isinstance(answer_id, str) and _HEX16.fullmatch(answer_id) is not None
        and (tuple_id is None or (isinstance(tuple_id, str) and _TUPLE_ID.fullmatch(tuple_id) is not None))
    )
    if not ok:
        return None
    closed = {"answer_id": answer_id, "choice": choice, "via": via, "time": when, "status": status, "tuple_id": tuple_id}
    if _is_outcome_unknown(rec):
        closed["outcome_unknown"] = True  # a flag derived from the CLI's own prefix; the free text stays behind
    return closed


# --- show -------------------------------------------------------------------------------------------


@ask_group.command(name="show")
@click.argument("ref")
@click.option("--json", "as_json", is_flag=True, help="The ask as one JSON object, with the entry age and its answer record.")
@click.pass_context
def show_cmd(ctx: click.Context, ref: str, as_json: bool) -> None:
    """Every field of one ask: numbered options, the recommendation, version, asker session, entry age."""
    s = _seams(ctx)
    now = s.now()
    ask, stamp = _resolve(_all_asks(_read_rows(s)), ref)
    age = "unknown" if stamp is None else format_age(now - stamp)
    title = answer_title(ask)
    row = s.t2.get(project=ASKS_PROJECT, title=title) if title else None
    # The asker reads this for its freshness check, so only closed-pattern values of the record leave here.
    answer = None if row is None else (_closed_answer(_json_obj(row)) or {"unreadable": True})
    if as_json:
        click.echo(json.dumps({**_ask_json(ask), "entry_age": age, "answer": answer}, indent=2))
        return
    out = [
        _c(_ref(ask)),
        f"version: {_c(ask.version)}",
        f"asker session: {_c(ask.session_id) or '(none: the entry has no session: line)'}",
        f"entry: {_c(ask.entry)} (updated {age})",
        f"thread: {_c(ask.thread)}",
    ]
    if ask.refs:
        out.append(f"refs: {_c(', '.join(ask.refs))}")
    out += [
        f"asked: {_c(ask.asked)}",
        f"question: {_c(ask.question)}",
        f"context: {_c(ask.context)}",
        "options:",
    ]
    for o in ask.options:
        mark = " (recommended)" if ask.rec == o.n else ""
        out.append(f"  {o.n}. {_c(o.text)}{' [irreversible]' if o.irreversible else ''}{mark}")
    out.append(f"rec: {_c(ask.rec_raw) or '-'}")
    out.append(f"why: {_c(ask.why) or '-'}")
    out.append(f"reversible: {'yes' if ask.reversible else 'no'}")
    out.append(f"blocking: {'yes' if ask.blocking else 'no'}")
    out.append(f"after: {_c(ask.after) or '-'}{' (conditional: not answerable until the asker deletes it)' if ask.conditional else ''}")
    out.append(f"by: {_c(ask.by) or '-'}")
    if ask.missing:
        out.append(f"missing: {', '.join(ask.missing)}")
    if ask.repeated:
        out.append(f"repeated: {_c(', '.join(ask.repeated))} (given more than once; the first one counts)")
    if ask.bad_values:
        out.append(f"bad values: {_c(', '.join(ask.bad_values))}")
    out.append("lines:")
    out.extend(f"  {_c(line)}" for line in ask.lines)
    if answer == {"unreadable": True}:
        out.append("answer record present, unreadable")
    elif answer is not None:
        choice = "none" if answer["choice"] is None else answer["choice"]
        out.append(f"answer: {answer['status']} at {answer['time']} via {answer['via']} (choice {choice})")
        if answer.get("outcome_unknown"):
            out.append("outcome unknown: the mail may have been delivered; a re-send is refused for 10 minutes")
    _say("\n".join(out))


# --- digest -----------------------------------------------------------------------------------------


@ask_group.command(name="digest")
@click.pass_context
def digest_cmd(ctx: click.Context) -> None:
    """Every answer of the last seven days, newest first: when, via, Sam's words, the batch reply, the tuple id."""
    s = _seams(ctx)
    entries, skipped = _answer_entries(_answer_rows(s), s.now(), DIGEST_WINDOW)
    lines = _digest_lines(entries) if entries else ["no answers in the last 7 days"]
    _say("\n".join(lines + _skipped_line(skipped)))


# --- answer -----------------------------------------------------------------------------------------


def _refuse(message: str) -> click.ClickException:
    return click.ClickException(_out(message))


def _current_text(ask: Ask) -> str:
    lines = [f"  question: {_c(ask.question)}", f"  context: {_c(ask.context)}"]
    lines += [f"  {o.n}. {_c(_option_text(ask, o.n))}" for o in ask.options]
    return "\n".join(lines)


def _gate_existing(row: dict[str, Any] | None, now: datetime, replace: bool, title: str) -> None:
    """First record wins: refuse unless the record at this title is absent, failed, a dead send, or ``--replace``."""
    if row is None:
        return
    rec = _json_obj(row)
    status = rec.get("status") if rec else None
    if status == "sent":
        if not replace:
            raise _refuse(f"already answered at {_c(rec.get('time', '?'))} via {_c(rec.get('via', '?'))}; --replace supersedes it")
        return
    if status == "sending":
        sent = parse_timestamp(str(rec.get("time", "")))
        if sent is None or now - sent <= SENDING_STALE:
            raise _refuse(f"send in progress (record {title} is still sending); retry after 10 minutes")
        return
    if status == "failed":
        return
    if not replace:
        raise _refuse(f"the record {title} is not one this command wrote; --replace overwrites it")


def _batch_title_for(s: Seams, mediator: str, words: str, now: datetime) -> tuple[str, dict[str, Any] | None]:
    """The batch record this reply belongs to: an earlier answer of the same reply (same mediator, same
    words, recent), else a new title stamped now."""
    prefix = f"batch/{mediator}/"
    taken: dict[str, dict[str, Any]] = {}
    for row in s.t2.get_all(project=ASKS_PROJECT):
        title = str(row.get("title", ""))
        rec = _json_obj(row) if title.startswith(prefix) else None
        if rec is not None:
            taken[title] = rec
    best: tuple[datetime, str] | None = None
    for title, rec in taken.items():
        when = parse_timestamp(str(rec.get("time", "")))
        titles = rec.get("answer_titles")
        reusable = isinstance(titles, list) and all(isinstance(t, str) for t in titles)  # a malformed record is never joined
        if reusable and rec.get("batch_words") == words and when is not None and abs(now - when) <= BATCH_WINDOW:
            if best is None or when > best[0]:
                best = (when, title)
    if best is not None:
        return best[1], taken[best[1]]
    moment = now
    while f"{prefix}{moment.strftime('%Y%m%dT%H%M%SZ')}" in taken:
        moment += timedelta(seconds=1)
    return f"{prefix}{moment.strftime('%Y%m%dT%H%M%SZ')}", None


def _outcome_unknown(exc: BaseException) -> bool:
    """True when the failed ``tuple_out`` may still have committed: the request went out and the answer was lost
    (a read or write timeout, a dropped connection, a half-read response, a 502, 503 or 504 from the edge).

    The whole exception chain is read (``__cause__`` and ``__context__``): a retry layer that saw a ReadTimeout
    and then failed to connect raises the ConnectError with the ReadTimeout behind it, and the first attempt may
    have committed. Any ambiguous link makes the outcome unknown. A 503 counts as ambiguous too: the client's own
    gateway retry re-sends a 502/503/504 outside its except block, so a 504 followed by a 503 reaches here as a
    bare 503 with no chain. A refusal the engine answered (a typed tuple error, a 4xx, a 500), a client
    pre-check, and a connect failure or timeout (nothing was sent) are definite: the mail was not posted. The one
    residual is a 504 the gateway retry follows with a 500 or 4xx, which reads as definite."""
    import httpx  # noqa: PLC0415 -- deferred: CLI startup cost

    never_sent = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError, httpx.UnsupportedProtocol)
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        link = stack.pop()
        if id(link) in seen:
            continue
        seen.add(id(link))
        if isinstance(link, httpx.HTTPStatusError):
            if link.response.status_code in (502, 503, 504):
                return True
        elif isinstance(link, httpx.TransportError):
            if not isinstance(link, never_sent):
                return True
        elif isinstance(link, (TimeoutError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return True
        stack.extend(x for x in (link.__cause__, link.__context__) if x is not None)
    return False


def _join_batch(s: Seams, batch_title: str, batch_words: str, via: str, answer_title_: str, now: datetime) -> None:
    """Add this answer's title to its batch record. The read-modify-write is under ``Seams.lock`` and re-reads
    the record inside it, so two answers joining one batch at once both keep their title."""
    with s.lock():
        row = s.t2.get(project=ASKS_PROJECT, title=batch_title)
        prior = _json_obj(row) if row is not None else None
        titles = prior.get("answer_titles") if prior else None
        titles = list(titles) if isinstance(titles, list) and all(isinstance(t, str) for t in titles) else []
        if answer_title_ not in titles:
            titles.append(answer_title_)
        stamp = prior.get("time") if prior else None
        batch = {
            "batch_words": batch_words,
            "via": via,
            "time": stamp if isinstance(stamp, str) and stamp else _fmt_time(now),
            "answer_titles": titles,
        }
        s.t2.put(project=ASKS_PROJECT, title=batch_title, content=json.dumps(batch, ensure_ascii=False), tags="ask-batch")


def _mail_body(ask: Ask, choice: int | None, via: str, answer_id: str) -> str:
    return "\n".join([
        "nx-ask-answer v1",
        f"ask: {ask.id}",
        f"version: {ask.version}",
        f"choice: {'none' if choice is None else choice}",
        f"via: {via}",
        f"answer: {answer_id}",
    ]) + "\n"


@ask_group.command(name="answer")
@click.argument("ref")
@click.argument("reply")
@click.option("--expect", required=True, metavar="VERSION", help="The version of the ask Sam saw; a different current version is refused.")
@click.option("--note", default=None, help="A note on a chosen option (recorded as Sam's words for --via cli).")
@click.option("--via", type=click.Choice(["cli", "mediator"]), default="cli", show_default=True,
              help="cli: Sam at a terminal. mediator: a session relaying Sam's words.")
@click.option("--sam-words", default=None, help="--via mediator: the contiguous span Sam typed for this ask (1-500 characters).")
@click.option("--batch-words", default=None, help="--via mediator: Sam's whole reply when it covered several asks (at most 2000 characters).")
@click.option("--replace", is_flag=True, help="Supersede a sent answer to the same version.")
@click.pass_context
def answer_cmd(
    ctx: click.Context, ref: str, reply: str, expect: str, note: str | None, via: str,
    sam_words: str | None, batch_words: str | None, replace: bool,
) -> None:
    """Answer one ask. REPLY is an option number, or free text for a note-only answer that authorizes no option."""
    stripped = reply.strip()
    if not stripped:
        raise click.UsageError("the reply is empty: give an option number, or the text of a note-only answer")
    s = _seams(ctx)
    now = s.now()
    ask, _stamp = _resolve(_all_asks(rows := _read_rows(s)), ref)
    if expect.strip() != ask.version:
        raise _refuse(
            f"--expect {expect.strip()} is not the current version {ask.version}; the ask now reads:\n{_current_text(ask)}"
        )
    if ask.conditional:
        raise _refuse(f"{_c(_ref(ask))} has after: {ask.after!r}: it is not answerable until the asker deletes that line")
    if not ask.session_id:
        raise _refuse(f"the entry {ask.entry!r} has no session: line, so the answer cannot be routed")
    if not _CANONICAL_UUID.fullmatch(ask.session_id):
        raise _refuse(f"session: {ask.session_id!r} is not a lower-case canonical UUID, so the answer cannot be routed")
    carrying = sorted({str(r.get("title", "")) for r in rows if _session_of(r) == ask.session_id})
    if len(carrying) > 1:
        raise _refuse(f"session {ask.session_id} is carried by more than one entry ({', '.join(_c(t) for t in carrying)}); no answer is routed")

    choice: int | None = None
    if _NUMBER.fullmatch(stripped):
        try:
            choice = int(stripped)
        except ValueError:  # a numeric string past Python's int digit limit
            raise click.UsageError("the reply is a number too large to be an option") from None
        if not 1 <= choice <= len(ask.options):
            raise _refuse(f"choice {choice} is outside 1..{len(ask.options)}: the ask has {len(ask.options)} options")
        recorded_note = note or ""
    else:
        if note is not None:
            raise click.UsageError("--note adds a note to a chosen option; a free-text reply is already the note")
        recorded_note = reply

    tty: str | None = None
    if via == "cli":
        if sam_words is not None or batch_words is not None:
            raise click.UsageError("--sam-words and --batch-words are for --via mediator; --via cli records the note")
        tty = s.tty()
        if tty is None:
            raise _refuse("--via cli needs stdin to be a TTY; a session relaying Sam's words uses --via mediator")
        via_value = "cli"
        sender = s.cli_sender()
        words = recorded_note
    else:
        if sam_words is None or not sam_words.strip():
            raise _refuse("--via mediator needs --sam-words: the words Sam typed for this ask")
        if len(sam_words) > SAM_WORDS_MAX:
            raise _refuse(f"--sam-words is {len(sam_words)} characters; the limit is {SAM_WORDS_MAX}")
        if batch_words is not None and len(batch_words) > BATCH_WORDS_MAX:
            raise _refuse(f"--batch-words is {len(batch_words)} characters; the limit is {BATCH_WORDS_MAX}")
        try:
            sender = str(uuid.UUID(s.session().strip()))
        except (ValueError, AttributeError):
            raise _refuse("--via mediator needs this session's own id, and it did not resolve to a UUID "
                          "(tuple-watch marker, then NX_T1_SESSION_ID, then NX_SESSION_ID)") from None
        via_value = f"mediator:{sender}"
        words = sam_words

    title = answer_title(ask)
    _gate_existing(s.t2.get(project=ASKS_PROJECT, title=title), now, replace, title)

    batch_title: str | None = None
    if batch_words:
        batch_title, _prior = _batch_title_for(s, sender, batch_words, now)
    answer_id = s.answer_id()
    record: dict[str, Any] = {
        "answer_id": answer_id,
        "question": ask.question,
        "options": [_option_text(ask, o.n) for o in ask.options],
        "choice": choice,
        "note": recorded_note,
        "sam_words": words,
        "batch_title": batch_title,
        "via": via_value,
        "asked": ask.asked,
        "blocking": ask.blocking,
        "tty": tty,
        "time": _fmt_time(now),
        "status": "sending",
        "tuple_id": None,
        "error": None,
    }
    s.t2.put(project=ASKS_PROJECT, title=title, content=json.dumps(record, ensure_ascii=False), tags="ask-answer")
    back = s.t2.get(project=ASKS_PROJECT, title=title)
    back_rec = _json_obj(back) if back is not None else None
    if back_rec is None or back_rec.get("answer_id") != answer_id:
        winner = back_rec.get("answer_id", "?") if back_rec else "unreadable"
        raise _refuse(f"another answer was recorded first (answer {winner}); nothing was sent")

    def settle(status: str, tuple_id: str | None, error: str | None) -> bool:
        """Write the final record; one retry. False when both writes failed.

        A ``failed`` record never joins a batch, so it carries no batch title."""
        final = {**record, "status": status, "tuple_id": tuple_id, "error": error}
        if status == "failed":
            final["batch_title"] = None
        for _attempt in range(2):
            try:
                s.t2.put(project=ASKS_PROJECT, title=title, content=json.dumps(final, ensure_ascii=False), tags="ask-answer")
                return True
            except Exception:  # noqa: BLE001 -- reported by the caller, which knows what was already sent
                continue
        return False

    def join_batch() -> None:
        """After the mail: only an answer whose mail went (or may have gone) is in the batch record."""
        if not batch_title:
            return
        try:
            _join_batch(s, batch_title, batch_words or "", via_value, title, now)
        except Exception as exc:  # noqa: BLE001 -- the mail is out; the batch record is the digest's extra
            click.echo(_out(f"warning: the batch record {batch_title} was not updated: {exc}"), err=True)

    try:
        tuple_id = s.tuple_out(
            f"mailbox/{ask.session_id}",
            {"to": ask.session_id},
            {
                "from": sender,
                "address_kind": "session",
                "kind": "reply",
                "correlation_id": f"ask:{ask.id}@{ask.version}",
            },
            _mail_body(ask, choice, via_value, answer_id),
            str(uuid.uuid4()),
        )
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: the failure is recorded on the answer and printed
        error = f"{type(exc).__name__}: {exc}" if not str(exc) else str(exc)
        if _outcome_unknown(exc):
            # The mail may be posted: the record stays `sending` (the drain hook accepts it with the mail's answer id,
            # choice and via) and the ten-minute rule is the only retry path. The error says why it is still sending.
            settle("sending", None, f"{OUTCOME_UNKNOWN}{error}")
            join_batch()
            click.echo(
                _out(f"outcome unknown: {error}; the mail may have been delivered; answer record left at sending; "
                     f"do not re-run (a retry is allowed after {int(SENDING_STALE.total_seconds() // 60)} minutes)"),
                err=True,
            )
            ctx.exit(1)
        note = "" if settle("failed", None, error) else "; the answer record was not updated and is left at sending"
        click.echo(_out(f"failed: {error}{note}"), err=True)
        ctx.exit(1)
    settled = settle("sent", str(tuple_id), None)  # the answer's state first; only the batch title waits
    join_batch()
    if not settled:
        click.echo(
            _out(f"mail sent (tuple {tuple_id}); answer record left at sending; do not re-run"), err=True,
        )
        ctx.exit(1)
    shown = "none" if choice is None else str(choice)
    _say(f"sent: {_c(_ref(ask))}@{ask.version} choice {shown} via {via_value}; tuple {tuple_id}")
