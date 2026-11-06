# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ask``: the mediator's tool and Sam's fallback for structured asks (RDR-245 Step 2, nexus-wc4jj.7).

An ask is a structured block in a session's T2 ``threads`` entry (parsed by ``nexus.console.threads``).
``list`` and ``show`` read the board; ``answer`` records Sam's answer in T2 project ``asks`` and mails it
to the asking session; ``digest`` lists what was answered. The record shape and the mail body are the
contract pinned by ``tests/fixtures/asks/``.

Every external effect goes through ``Seams``: the T2 reader/writer, the tuple writer, the TTY probe and the
own-session resolver, plus the clock, the beads reader, the ``cli`` sender id and the answer-id source. The
click context carries them (``obj=Seams(...)``); a run without them builds the live ones.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

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
    """This session's own id the way ``mailbox_send`` finds it (tuple-watch marker, then ``NX_T1_SESSION_ID``);
    empty when nothing resolves."""
    from nexus.tuple_directory import resolve_default_from  # noqa: PLC0415 -- deferred: CLI startup cost

    try:
        return resolve_default_from()
    except Exception:  # noqa: BLE001 -- "resolves nothing" is a refusal upstream, never a traceback
        return ""


def live_beads() -> BeadsResult:
    from nexus.console.routes.threads import run_bd_needs_sam  # noqa: PLC0415 -- deferred: console import is heavy

    return run_bd_needs_sam()


def default_cli_sender() -> str:
    """The ``from`` of a ``--via cli`` mail: a UUID minted once, kept in the nexus config dir."""
    from nexus import config as _nx_config  # noqa: PLC0415 -- deferred: CLI startup cost

    path = _nx_config.nexus_config_dir() / "ask-cli-sender"
    for _ in range(2):
        try:
            value = path.read_text(encoding="utf-8").strip()
            if _CANONICAL_UUID.fullmatch(value):
                return value
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as fh:  # exclusive: two first runs agree on one id
                fh.write(str(uuid.uuid4()) + "\n")
        except FileExistsError:
            continue
    raise click.ClickException(f"cannot keep the cli sender id at {path}")


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
        raise click.ClickException(f"no ask {ref!r} on the board (nx ask list names them)")
    if len(hits) > 1:
        names = ", ".join(_ref(a) for a, _ in hits)
        if "/" in ref:
            raise click.ClickException(f"{ref!r} names more than one ask in that entry: {names}")
        raise click.ClickException(f"{ref!r} is held by more than one entry; name one of: {names}")
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


def _one_line(value: str) -> str:
    return " ".join(value.split())


# --- answer records and the digest ------------------------------------------------------------------


def _answer_entries(s: Seams, now: datetime, window: timedelta) -> list[dict[str, Any]]:
    """Answer records inside ``window``, newest first, each with the words of the batch it came from."""
    rows = [r for r in s.t2.get_all(project=ASKS_PROJECT) if isinstance(r, dict)]
    batches = {
        str(r.get("title", "")): rec
        for r in rows
        if str(r.get("title", "")).startswith("batch/") and (rec := _json_obj(r)) is not None
    }
    out: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        title = str(row.get("title", ""))
        m = _ANSWER_TITLE.fullmatch(title)
        rec = _json_obj(row) if m else None
        if m is None or rec is None:
            continue
        when = parse_timestamp(str(rec.get("time", ""))) or parse_timestamp(str(row.get("timestamp", "") or ""))
        if when is None or now - when > window:
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
    return [e for _, e in out]


def _digest_lines(entries: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for e in entries:
        choice = "none" if e["choice"] is None else str(e["choice"])
        head = f"  {e['time']}  {e['via']}  {e['ask']}@{e['version']}  choice {choice}"
        tail = f"tuple {e['tuple_id']}" if e["status"] == "sent" else f"[{e['status']}{': ' + e['error'] if e['error'] else ''}]"
        lines.append(f'{head}  "{_one_line(str(e["sam_words"]))}"  {tail}')
        if e["batch_words"]:
            lines.append(f'      batch: "{_one_line(str(e["batch_words"]))}"')
    return lines


# --- list -------------------------------------------------------------------------------------------


def _group_line(c: ClassedAsk, width: int) -> str:
    a = c.ask
    meta = "".join(f" [{m}]" for m in ((f"by {a.by}" if a.by else ""), c.mark) if m)
    return _fit(f"  {_ref(a)}  {a.version}{meta}  {a.question}", width)


def _records_for(s: Seams, asks: list[tuple[Ask, datetime | None]]) -> dict[str, Any]:
    """Answer records by exact title, one read per ask that could be answered."""
    records: dict[str, Any] = {}
    for ask, _stamp in asks:
        title = answer_title(ask)
        if not title or ask.conditional or title in records:
            continue
        row = s.t2.get(project=ASKS_PROJECT, title=title)
        if row is not None:
            records[title] = row
    return records


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
    asks = _all_asks(rows)
    classes = classify(rows, now, _records_for(s, asks))
    board = build_board(rows, now)
    beads = s.beads()
    merged = merge_asks(board.needs_you, beads.beads, board.structured)
    findings = lint(rows)
    digest = _answer_entries(s, now, LIST_DIGEST_WINDOW)
    if as_json:
        click.echo(json.dumps(_list_json(classes, merged, beads, findings, digest, board.unreadable), indent=2))
        return
    width = _width()
    out: list[str] = []
    for name in ("interrupt", "batch"):
        group = getattr(classes, name)
        out.append(f"{name.capitalize()} ({len(group)})")
        out.extend(_group_line(c, width) for c in group)
    counts = ", ".join(f"{n} {len(getattr(classes, n))}" for n in ("quiet", "conditional", "answered"))
    out.append(f"Also: {counts}")
    if show_all:
        for name in ("quiet", "conditional", "answered"):
            group = getattr(classes, name)
            if group:
                out.append(f"{name.capitalize()} ({len(group)})")
                out.extend(_group_line(c, width) for c in group)
    if merged.threads:
        out.append(f"Legacy asks ({len(merged.threads)})")
        out.extend(
            _fit(f"  {i.session} / {i.thread.title}: {i.thread.needs_sam} ({i.age})", width) for i in merged.threads
        )
    if merged.beads or beads.error:
        out.append(f"Needs-sam beads ({len(merged.beads)})")
        if beads.error:
            out.append(f"  could not be read: {beads.error}")
        for b in merged.beads:
            pointer = f"  -> {', '.join(b.ask_pointers)}" if b.ask_pointers else ""
            prio = f"  P{b.bead.priority}" if b.bead.priority is not None else ""
            out.append(_fit(f"  {b.bead.id}{prio}{pointer}  {b.bead.title}", width))
    if findings:
        out.append(f"Lint ({len(findings)})")
        out.extend(_fit(f"  {f.entry}{'/' + f.ask if f.ask else ''}: {f.rule}: {f.message}", width) for f in findings)
    if classes.unreadable:
        out.append("Unreadable entries (their asks are not listed): " + ", ".join(classes.unreadable))
    out.append(f"Digest, last 48 hours ({len(digest)})")
    out.extend(_digest_lines(digest))
    click.echo("\n".join(out))


def _list_json(classes: Any, merged: Any, beads: BeadsResult, findings: Any, digest: list[dict[str, Any]],
               unreadable: tuple[str, ...]) -> dict[str, Any]:
    data: dict[str, Any] = {
        name: [_ask_json(c.ask, c.group, c.mark) for c in getattr(classes, name)] for name in _GROUPS
    }
    data["legacy"] = [
        {"session": i.session, "thread": i.thread.title, "needs_sam": i.thread.needs_sam, "age": i.age,
         "refs": list(i.thread.refs)}
        for i in merged.threads
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
    return data


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
    answer = _json_obj(row) if row is not None else None
    if as_json:
        click.echo(json.dumps({**_ask_json(ask), "entry_age": age, "answer": answer}, indent=2))
        return
    out = [
        _ref(ask),
        f"version: {ask.version}",
        f"asker session: {ask.session_id or '(none: the entry has no session: line)'}",
        f"entry: {ask.entry} (updated {age})",
        f"thread: {ask.thread}",
    ]
    if ask.refs:
        out.append(f"refs: {', '.join(ask.refs)}")
    out += [
        f"asked: {ask.asked}",
        f"question: {ask.question}",
        f"context: {ask.context}",
        "options:",
    ]
    for o in ask.options:
        mark = " (recommended)" if ask.rec == o.n else ""
        out.append(f"  {o.n}. {o.text}{' [irreversible]' if o.irreversible else ''}{mark}")
    out.append(f"rec: {ask.rec_raw or '-'}")
    out.append(f"why: {ask.why or '-'}")
    out.append(f"reversible: {'yes' if ask.reversible else 'no'}")
    out.append(f"blocking: {'yes' if ask.blocking else 'no'}")
    out.append(f"after: {ask.after or '-'}{' (conditional: not answerable until the asker deletes it)' if ask.conditional else ''}")
    out.append(f"by: {ask.by or '-'}")
    if ask.missing:
        out.append(f"missing: {', '.join(ask.missing)}")
    if answer is not None:
        out.append(
            f"answer: {answer.get('status', '?')} at {answer.get('time', '?')} via {answer.get('via', '?')}"
            f" (choice {answer.get('choice') if answer.get('choice') is not None else 'none'})"
        )
    click.echo("\n".join(out))


# --- digest -----------------------------------------------------------------------------------------


@ask_group.command(name="digest")
@click.pass_context
def digest_cmd(ctx: click.Context) -> None:
    """Every answer of the last seven days, newest first: when, via, Sam's words, the batch reply, the tuple id."""
    s = _seams(ctx)
    entries = _answer_entries(s, s.now(), DIGEST_WINDOW)
    if not entries:
        click.echo("no answers in the last 7 days")
        return
    click.echo("\n".join(_digest_lines(entries)))


# --- answer -----------------------------------------------------------------------------------------


def _refuse(message: str) -> click.ClickException:
    return click.ClickException(message)


def _current_text(ask: Ask) -> str:
    lines = [f"  question: {ask.question}", f"  context: {ask.context}"]
    lines += [f"  {o.n}. {_option_text(ask, o.n)}" for o in ask.options]
    return "\n".join(lines)


def _gate_existing(row: dict[str, Any] | None, now: datetime, replace: bool, title: str) -> None:
    """First record wins: refuse unless the record at this title is absent, failed, a dead send, or ``--replace``."""
    if row is None:
        return
    rec = _json_obj(row)
    status = rec.get("status") if rec else None
    if status == "sent":
        if not replace:
            raise _refuse(f"already answered at {rec.get('time', '?')} via {rec.get('via', '?')}; --replace supersedes it")
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
        if rec.get("batch_words") == words and when is not None and abs(now - when) <= BATCH_WINDOW:
            if best is None or when > best[0]:
                best = (when, title)
    if best is not None:
        return best[1], taken[best[1]]
    moment = now
    while f"{prefix}{moment.strftime('%Y%m%dT%H%M%SZ')}" in taken:
        moment += timedelta(seconds=1)
    return f"{prefix}{moment.strftime('%Y%m%dT%H%M%SZ')}", None


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
    s = _seams(ctx)
    now = s.now()
    ask, _stamp = _resolve(_all_asks(rows := _read_rows(s)), ref)
    if expect.strip() != ask.version:
        raise _refuse(
            f"--expect {expect.strip()} is not the current version {ask.version}; the ask now reads:\n{_current_text(ask)}"
        )
    if ask.conditional:
        raise _refuse(f"{_ref(ask)} has after: {ask.after!r}: it is not answerable until the asker deletes that line")
    if not ask.session_id:
        raise _refuse(f"the entry {ask.entry!r} has no session: line, so the answer cannot be routed")
    if not _CANONICAL_UUID.fullmatch(ask.session_id):
        raise _refuse(f"session: {ask.session_id!r} is not a lower-case canonical UUID, so the answer cannot be routed")
    carrying = sorted({str(r.get("title", "")) for r in rows if _session_of(r) == ask.session_id})
    if len(carrying) > 1:
        raise _refuse(f"session {ask.session_id} is carried by more than one entry ({', '.join(carrying)}); no answer is routed")

    choice: int | None = None
    if _NUMBER.fullmatch(reply.strip()):
        choice = int(reply.strip())
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
                          "(tuple-watch marker, then NX_T1_SESSION_ID)") from None
        via_value = f"mediator:{sender}"
        words = sam_words

    title = answer_title(ask)
    _gate_existing(s.t2.get(project=ASKS_PROJECT, title=title), now, replace, title)

    batch_title: str | None = None
    batch_prior: dict[str, Any] | None = None
    if batch_words:
        batch_title, batch_prior = _batch_title_for(s, sender, batch_words, now)
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

    def settle(status: str, tuple_id: str | None, error: str | None) -> None:
        final = {**record, "status": status, "tuple_id": tuple_id, "error": error}
        s.t2.put(project=ASKS_PROJECT, title=title, content=json.dumps(final, ensure_ascii=False), tags="ask-answer")

    try:
        if batch_title:
            titles = list(batch_prior["answer_titles"]) if batch_prior else []
            if title not in titles:
                titles.append(title)
            batch = {
                "batch_words": batch_words,
                "via": via_value,
                "time": batch_prior["time"] if batch_prior else _fmt_time(now),
                "answer_titles": titles,
            }
            s.t2.put(project=ASKS_PROJECT, title=batch_title, content=json.dumps(batch, ensure_ascii=False), tags="ask-batch")
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
        settle("failed", None, error)
        click.echo(f"failed: {error}", err=True)
        ctx.exit(1)
    settle("sent", str(tuple_id), None)
    shown = "none" if choice is None else str(choice)
    click.echo(f"sent: {_ref(ask)}@{ask.version} choice {shown} via {via_value}; tuple {tuple_id}")
