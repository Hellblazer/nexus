# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx memory gate-rollup``: the loss check and the run that orders its steps (RDR-209 Phase 3 Steps 2 and 3).

:func:`run` is the whole command after option parsing. It reads the repository's project once, lists the run's
records (:func:`~nexus.memory_curate.gate_rollup.select_records`), and before anything is written it stops, and
names the cause, if any check of Step 2 fails:

1. the phase is closed (the census flag's own rule: :func:`closed_rdr_numbers`, :func:`closed_phase_numbers`);
2. every record an earlier pointer listed still resolves: it is in this read, or its archive reads back whole (the
   run then carries it into the new pointer, so a record the reap deleted never drops out of the listing); and,
   after the pointer is written, the pointer reads back from T3 with the same list;
3. each listed bead's newest record is ``justified``, or the bead is named with ``--accept-open``.
"""
from __future__ import annotations

import re
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from nexus.memory_curate import _all_closed, _parse_ts, closed_phase_numbers, closed_rdr_numbers
from nexus.memory_curate import gate_rollup as gr

SEVERITY = {"justified": 0, "partial": 1, "not-justified": 2}
TTL_DAYS = 30
#: The fields the TTL write must leave as they were; ``ttl`` itself must read 30.
KEPT_FIELDS = ("content", "tags", "agent", "session", "timestamp", "access_count", "last_accessed")


@dataclass
class Result:
    scope: gr.Scope
    selection: gr.Selection
    lines: list[str] = field(default_factory=list)
    archived: int = 0
    already_whole: int = 0
    pointer_title: str = ""
    pointer_parts: int = 0
    stale: list[str] = field(default_factory=list)
    carried: list[dict[str, Any]] = field(default_factory=list)
    ttl_written: list[str] = field(default_factory=list)
    ttl_skipped: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    drifted: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)


# ── check 1: the phase is closed ─────────────────────────────────────────────────

def open_phase_reason(scope: gr.Scope, root: Path) -> str | None:
    """Why the run's phase is not closed, or ``None`` when it is.

    Closed means the RDR's frontmatter status is ``closed`` or, for ``--phase P``, the phase's own heading carries
    the close marker (a sub-phase such as 3a is closed when phase 3 is). ``--all`` needs the RDR closed."""
    number = str(scope.rdr)
    if number in closed_rdr_numbers(root):
        return None
    if scope.phase is not None and _all_closed([scope.phase], closed_phase_numbers(root).get(number, set())):
        return None
    what = "its status is not closed" if scope.phase is None else f"phase {scope.phase} carries no close marker"
    return (f"RDR-{scope.rdr:03d} is not closed ({what}; read under {root}); an open phase is never touched. "
            "Close the phase first: '### Phase N (CLOSED, YYYY-MM-DD)'")


# ── check 3: the verdict chain ───────────────────────────────────────────────────

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ROUND = re.compile(r"-r(\d+)$")


def order_key(entry: dict[str, Any], content: str) -> tuple[str, int]:
    """``(date, round)`` of a record, ascending is older.

    The date is the header ``date:`` field; a record with no header is dated from the first ``YYYY-MM-DD`` in its
    title, else from its timestamp's day. The round is the title's trailing ``-r<N>``, 0 when there is none (so a
    record with no round sorts before ``-r1``). ``gate_record`` writes no round field, so the title is the only
    place a round lives."""
    head = gr.header_fields(content).get("date", "")
    if _DATE.fullmatch(head):
        date = head
    elif (m := _DATE.search(entry["title"])) is not None:
        date = m.group(0)
    else:
        date = (entry.get("timestamp") or "")[:10]
    r = _ROUND.search(entry["title"])
    return date, int(r.group(1)) if r else 0


def chain(entries: list[dict[str, Any]], contents: dict[str, str]) -> tuple[str, list[dict[str, Any]]]:
    """``(verdict, newest records)`` of one bead's records.

    The newest are those sharing the greatest ``(date, round)`` (:func:`order_key`). When more than one does, the
    worse verdict decides the chain (``not-justified`` over ``partial`` over ``justified``), because dates are
    day-granular and the round is optional, so two records of one day can have no order. The newest records are
    returned by title."""
    keyed = [(order_key(e, contents[e["title"]]), e) for e in entries]
    top = max(k for k, _ in keyed)
    newest = sorted((e for k, e in keyed if k == top), key=lambda e: e["title"])
    return max((e["verdict"] for e in newest), key=SEVERITY.__getitem__), newest


def verdict_chains(sel: gr.Selection) -> dict[str, tuple[str, list[dict[str, Any]]]]:
    by_bead: dict[str, list[dict[str, Any]]] = {}
    for e in sel.listed:
        by_bead.setdefault(e["bead"], []).append(e)
    return {bead: chain(entries, sel.contents) for bead, entries in sorted(by_bead.items())}


def quarantine_lookup(mem: Any, project: str) -> Callable[[], dict[str, dict[str, Any]]]:
    """A cached read of the project's quarantined rows by title, made the first time it is called.

    ``get_all`` hides a quarantined row, so this listing is the one place a row the expire sweep took can be found.
    An engine older than RDR-207 has no such route (404), which reads as "nothing is quarantined"."""
    cache: dict[str, dict[str, Any]] | None = None

    def get() -> dict[str, dict[str, Any]]:
        nonlocal cache
        if cache is None:
            try:
                cache = {r["title"]: r for r in mem.list_quarantined(project=project)}
            except httpx.HTTPStatusError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise
                cache = {}
        return cache

    return get


# ── check 2: titles resolve ──────────────────────────────────────────────────────

def merge_previous(t3: Any, prev: gr.Pointer | None, sel: gr.Selection,
                   quarantined: Callable[[], dict[str, dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[str]]:
    """``(carried, unresolvable)``: the earlier pointer's records this run's read no longer lists.

    One the expire sweep quarantined is carried marked ``state: quarantined`` (its row is restorable, so it is
    not lost, whatever its archive). One that is neither live nor quarantined (the reap deleted it) is carried marked
    ``state: absent`` while its archive reads back whole; one whose archive does not cannot be resolved, and the run
    stops naming it."""
    if prev is None:
        return [], []
    live = {e["title"] for e in sel.listed}
    col = gr.collection_name(t3)
    carried: list[dict[str, Any]] = []
    bad: list[str] = []
    for rec in prev.listed:
        if rec["title"] in live:
            continue
        if rec["title"] in quarantined():
            carried.append({**rec, "state": "quarantined"})
        elif gr.read_parts(t3, col, rec["archive"], rec["parts"]) is None:
            bad.append(rec["title"])
        else:
            carried.append({**rec, "state": "absent"})
    return carried, bad


# ── the engine's expiry rule ─────────────────────────────────────────────────────

def effective_ttl_days(access_count: int, ttl_days: int = TTL_DAYS) -> float:
    """``MemoryRepository.java:697``: ``ttl * (1 + ln(accessCount + 1))``, the natural log."""
    return ttl_days * (1.0 + math.log(access_count + 1))


def would_expire(row: dict[str, Any], *, now: datetime, ttl_days: int = TTL_DAYS) -> bool:
    """True when the engine's expire sweep would quarantine *row* if it carried *ttl_days* (``MemoryRepository.java:686-701``).

    A row with no timestamp is skipped (``:695``); the age is whole seconds over 86,400 (``:698``, the engine
    truncates to seconds before dividing); the comparison is strict (``:699``)."""
    stamp = _parse_ts(row.get("timestamp") or "")
    if stamp is None:
        return False
    age_days = int((now - stamp).total_seconds()) / 86400.0
    return age_days > effective_ttl_days(int(row.get("access_count") or 0), ttl_days)


def classify(sel: gr.Selection, prev: gr.Pointer | None, by_title: dict[str, dict[str, Any]],
             res: Result) -> list[dict[str, Any]]:
    """The records to give a TTL, and the ``ttl_set`` mark of every listed record.

    A record that has no TTL is a candidate, unless the earlier pointer says a run already gave it one
    (``ttl_set``): then a person restored it (``nx memory restore`` clears the TTL), the rerun leaves it alone,
    names it and keeps it marked ``restored``, so it is not given a TTL again and the next rerun skips it too. A
    record the earlier pointer listed without that mark, whose write stopped part way, is a candidate. A record that
    has a TTL already is skipped, and counts as ours when the earlier pointer listed it."""
    was = {r["title"]: r for r in (prev.listed if prev else [])}
    candidates: list[dict[str, Any]] = []
    for e in sel.listed:
        before = was.get(e["title"])
        if by_title[e["title"]].get("ttl") is not None:
            e["ttl_set"] = before is not None
            res.ttl_skipped.append(e["title"])
        elif before is not None and before.get("ttl_set"):
            e["ttl_set"], e["restored"] = True, True
            res.restored.append(e["title"])
        else:
            e["ttl_set"] = False
            candidates.append(e)
    return candidates


def write_verified_pointer(t3: Any, project: str, scope: gr.Scope, sel: gr.Selection, accept_open: tuple[str, ...],
                           res: Result) -> str:
    """Write the pointer and read it back from T3; it must list the same records. Returns its text."""
    text, res.pointer_parts = gr.render_pointer(scope, project, sel, accepted_open=accept_open, carried=res.carried)
    res.stale = gr.write_pointer(t3, scope, text, res.pointer_parts)
    back = gr.read_pointer(t3, res.pointer_title)
    want = {(e["project"], e["title"]) for e in [*sel.listed, *res.carried]}
    if back is None or {(e["project"], e["title"]) for e in back.listed} != want:
        raise gr.GateRollupError(f"the pointer {res.pointer_title!r} did not read back from T3 with the same list")
    return text


# ── the count-first guard ────────────────────────────────────────────────────────

def count_title(scope: gr.Scope) -> str:
    """The T2 title of a scope's dry-run count: ``gate-rollup-count-rdr-<NNN>-phase-<P>`` or ``-all``, in ``<repo>``."""
    return f"gate-rollup-count-rdr-{scope.rdr:03d}-" + ("all" if scope.phase is None else f"phase-{scope.phase}")


@dataclass
class Count:
    count: int
    titles: tuple[str, ...]


def render_count(scope: gr.Scope, project: str, candidates: list[dict[str, Any]], count: int, now: datetime,
                 head: str) -> str:
    lines = [f"gate-rollup dry-run count: {scope.label}", f"project: {project}", f"count: {count}",
             f"candidates: {len(candidates)}", f"date: {now:%Y-%m-%dT%H:%M:%SZ}", f"head: {head}", "titles:"]
    return "\n".join([*lines, *sorted(e["title"] for e in candidates)]) + "\n"


def parse_count(text: str) -> Count | None:
    m = re.search(r"^count: (\d+)$", text, re.MULTILINE)
    if m is None or "\ntitles:\n" not in text:
        return None
    return Count(int(m.group(1)), tuple(t for t in text.split("\ntitles:\n", 1)[1].splitlines() if t))


def record_count(mem: Any, project: str, scope: gr.Scope, candidates: list[dict[str, Any]], count: int,
                 now: datetime, head: str) -> str:
    """Write the dry run's count where the real run can read it: one T2 row in ``<repo>`` (never ``<repo>_rdr``, never
    SQLite), through the engine, titled by the scope, replaced by a later dry run. It carries neither ``kind:gate``
    nor ``gate``, so the rollup never lists it and the census never flags it."""
    title = count_title(scope)
    mem.put(project, title, render_count(scope, project, candidates, count, now, head), tags="gate-rollup-count")
    return title


def check_guard(rows: list[dict[str, Any]], scope: gr.Scope, candidates: list[dict[str, Any]], counted: int | None) -> None:
    """Refuse a real run that would write a TTL unless this scope's dry-run count is recorded, the caller passes
    ``--counted <that count>``, and every candidate is one the dry run counted. A run with no candidate writes no TTL
    and needs none."""
    if not candidates:
        return
    row = next((r for r in rows if r["title"] == count_title(scope)), None)
    rec = parse_count(row["content"]) if row is not None else None
    if rec is None:
        raise gr.GateRollupError(
            f"no dry-run count is recorded for {scope.label}: run it with --dry-run first and report the count it "
            "prints to Sam, then run again with --counted <that count>. Nothing was written.")
    if counted is None:
        raise gr.GateRollupError(
            f"{scope.label} has a dry-run count; pass --counted N, the number the dry run reported ('would be "
            "quarantined by the next expire sweep'). Nothing was written.")
    if counted != rec.count:
        raise gr.GateRollupError(
            f"--counted {counted} is not the recorded count for {scope.label}; run --dry-run again, or pass the "
            "number it reported. Nothing was written.")
    new = [e["title"] for e in candidates if e["title"] not in rec.titles]
    if new:
        raise gr.GateRollupError(
            f"{', '.join(new)} is not in the dry run's count: records changed since it ran. Run --dry-run again. "
            "Nothing was written.")


# ── the TTL write ────────────────────────────────────────────────────────────────

def write_ttl(mem: Any, project: str, candidates: list[dict[str, Any]], fresh: dict[str, dict[str, Any]],
              contents: dict[str, str], res: Result) -> dict[str, dict[str, Any]]:
    """Write ``ttl=30`` on each candidate through the verbatim import route; return ``{title: row sent}``.

    *fresh* is a read of the project taken just before this call, so a reader's access that landed after the listing
    keeps its count. A row that now has a TTL, or is gone, is skipped. So is a row whose content or timestamp is no
    longer what the archive and the pointer hold (*contents*, the listing's text): it was rewritten after it was
    archived, and a TTL would retire text that has no archive. It is named, keeps no TTL, and a rerun archives its new
    text. Of the values sent, the content, tags, agent, session, ``access_count``, ``last_accessed`` and timestamp are
    the read's: a null agent or session is sent as absent so it stays NULL, and an empty ``last_accessed`` as ``None``.
    The import route itself also clears the quarantine stamps, re-stamps the row's identity with the caller and
    truncates the timestamp to the second. Never ``put``: it fills a missing agent or session from the writer's
    environment."""
    sent: dict[str, dict[str, Any]] = {}
    for e in candidates:
        row = fresh.get(e["title"])
        if row is None or row.get("ttl") is not None:
            res.ttl_skipped.append(e["title"])
            continue
        if row["content"] != contents.get(e["title"]) or row.get("timestamp") != e["timestamp"]:
            res.drifted.append(e["title"])
            continue
        try:
            mem.import_entry(
                project=project, title=row["title"], content=row["content"], timestamp=row["timestamp"],
                tags=row.get("tags") or "", ttl=TTL_DAYS, agent=row.get("agent"), session=row.get("session"),
                access_count=int(row.get("access_count") or 0), last_accessed=row.get("last_accessed") or None)
        except Exception as exc:  # noqa: BLE001 — name the record; the ones already written keep their TTL and a rerun finishes
            raise gr.GateRollupError(
                f"the TTL write stopped at {row['title']!r}: {type(exc).__name__}: {exc}; "
                f"{len(sent)} earlier record(s) have the TTL, and a rerun writes the rest") from exc
        sent[row["title"]] = row
    return sent


def read_back(mem: Any, project: str, sent: dict[str, dict[str, Any]], res: Result,
              quarantined: Callable[[], dict[str, dict[str, Any]]]) -> None:
    """Compare every field of each row written with what it was sent as; a mismatch names the title and the field and
    prints the original row so a person can write it back. The command does not undo.

    ``get_all`` hides a row the expire sweep has already quarantined, and the row keeps its own timestamp, so an old
    one can be quarantined at once. Such a row is looked up in the quarantined listing (one listing for the run) before
    it is called missing, reported as quarantined with the command that restores it, and still compared field by field."""
    after = {r["title"]: r for r in mem.get_all(project)}
    for title, was in sent.items():
        row = after.get(title)
        if row is None and (q := quarantined().get(title)) is not None:
            res.quarantined.append(title)
            res.lines.append(f"quarantined (restorable: nx memory restore {q['id']}): {title}")
            row = q
        if row is None:
            res.mismatches.append(f"mismatch: {title!r} is not in the read after the write; it was {was}")
            continue
        wrong = [f for f in KEPT_FIELDS if (row.get(f) or None) != (was.get(f) or None)]
        if row.get("ttl") != TTL_DAYS:
            wrong.append("ttl")
        for f in wrong:
            res.mismatches.append(f"mismatch: {title!r} field {f}: now {row.get(f)!r}, was {was.get(f)!r}; original row {was}")


# ── the run ──────────────────────────────────────────────────────────────────────

def dry_run_lines(candidates: list[dict[str, Any]], by_title: dict[str, dict[str, Any]],
                  now: datetime) -> tuple[list[str], int]:
    """The dry run's report: each candidate with its timestamp, ``access_count`` and whether the engine's rule has
    it past its effective TTL, then the one number: how many the next expire sweep would quarantine (also
    returned)."""
    out: list[str] = []
    past = 0
    for e in candidates:
        row = by_title[e["title"]]
        is_past = would_expire(row, now=now)
        past += is_past
        count = int(row.get("access_count") or 0)
        out.append(f"  candidate  {row.get('timestamp') or 'no timestamp'}  access_count={count}  effective TTL "
                   f"{effective_ttl_days(count):.1f} d  {'past' if is_past else 'within'} its effective TTL  {e['title']}")
    out.append(f"would be quarantined by the next expire sweep: {past} of {len(candidates)} candidate record(s)")
    return out, past


def run(mem: Any, *, project: str, scope: gr.Scope, dry_run: bool, accept_open: tuple[str, ...],
        root: Path, t3_factory: Any, now: datetime | None = None, counted: int | None = None,
        head: str = "unknown") -> Result:
    """The command after option parsing. Every refusal is a :class:`~nexus.memory_curate.gate_rollup.GateRollupError`
    raised before anything is written; *now* is the clock the dry run's expiry rule reads, and *counted* is the
    number the caller was told by a dry run (the guard checks it before any TTL is written). *head* is the
    repository's commit, recorded with the count."""
    now = now or datetime.now(UTC)
    reason = open_phase_reason(scope, root)
    if reason is not None:
        raise gr.GateRollupError(reason)
    rows = list(mem.get_all(project))
    sel = gr.select_records(rows, project=project, rdr=scope.rdr, beads=scope.beads, phase=scope.phase)
    res = Result(scope, sel, gr.listing_lines(scope, project, sel))
    res.pointer_title = gr.pointer_title(scope.rdr, scope.phase)

    chains = verdict_chains(sel)
    unknown = [b for b in accept_open if b not in chains]
    if unknown:
        raise gr.GateRollupError(f"--accept-open names {', '.join(unknown)}, which has no listed record in this run")
    blocked = [b for b, (verdict, _) in chains.items() if verdict != "justified" and b not in accept_open]
    for e in sel.listed:
        if e["verdict"] != "justified":
            res.lines.append(f"  not justified: {e['bead']}  {e['verdict']}  {e['title']}")
    if blocked:
        raise gr.GateRollupError(
            "\n".join(res.lines) + "\nthe newest record of " + ", ".join(blocked) + " is not justified; read the "
            "records above, then fix it or name the bead with --accept-open <bead>. Nothing was written.")

    t3 = t3_factory()
    quarantined = quarantine_lookup(mem, project)
    prev = gr.read_pointer(t3, res.pointer_title)
    res.carried, bad = merge_previous(t3, prev, sel, quarantined)
    if bad:
        raise gr.GateRollupError(
            f"the earlier pointer lists {', '.join(bad)}, which is neither in this read nor archived in T3 any more; "
            "nothing was written")
    if not sel.listed and not sel.unmapped and not res.carried:
        res.lines.append("nothing to write: no pointer record")
        return res
    by_title = {r["title"]: r for r in rows}
    candidates = classify(sel, prev, by_title, res)
    res.lines += [f"restored, left alone: {t}" for t in res.restored]
    if dry_run:
        lines, past = dry_run_lines(candidates, by_title, now)
        res.lines += lines
        title = record_count(mem, project, scope, candidates, past, now, head)
        res.lines.append(f"count recorded: {project}/{title}")
        res.lines.append("dry run: archives, the pointer write and its read-back skipped; no TTL and no T3 note written")
        return res
    check_guard(rows, scope, candidates, counted)

    res.archived, res.already_whole = gr.archive_records(t3, sel, scope)
    text = write_verified_pointer(t3, project, scope, sel, accept_open, res)
    fresh = {r["title"]: r for r in mem.get_all(project)}        # one read, right before the writes
    sent = write_ttl(mem, project, candidates, fresh, sel.contents, res)
    res.ttl_written = list(sent)
    read_back(mem, project, sent, res, quarantined)
    if sent:                    # the pointer now says which records a run gave a TTL, so a rerun can tell a restore
        for e in candidates:
            e["ttl_set"] = e["ttl_set"] or e["title"] in sent
        try:
            text = write_verified_pointer(t3, project, scope, sel, accept_open, res)
        except gr.GateRollupError as exc:
            raise gr.GateRollupError(
                f"{len(sent)} record(s) now have the TTL but the pointer's ttl marks were not written ({exc}); "
                "rerun to write them") from exc
    res.lines += [
        f"archived {res.archived} record(s), {res.already_whole} already whole",
        f"ttl: wrote {len(sent)} record(s) with ttl={TTL_DAYS}, {len(res.ttl_skipped)} already have a TTL",
        f"pointer: {res.pointer_title} ({len(text.encode())} bytes), pointer parts: {res.pointer_parts}",
    ]
    if res.carried:
        res.lines.append(f"carried: {len(res.carried)} record(s) an earlier pointer listed that this read does not")
    res.lines += [f"stale: {t!r} is left from an earlier, longer pointer; nothing here deletes it" for t in res.stale]
    res.lines += [f"changed since it was archived, no TTL: {t} (a rerun archives its new text)" for t in res.drifted]
    res.lines += res.mismatches
    return res
