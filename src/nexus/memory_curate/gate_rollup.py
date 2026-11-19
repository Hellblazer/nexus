# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx memory gate-rollup``: list a phase's gate records and write the T3 pointer (RDR-209 Phase 3 Step 1).

One ``get_all`` of ``<repo>`` (never ``<repo>_rdr``) is split by whole tag (:func:`split_tags`). The run writes one
T3 note in the knowledge subject ``nexus-rdr-research``, through the writer ``nx store put`` uses (``put_note``):
the pointer, category ``gate_pointer``, titled ``gate-records-rdr-<NNN>-phase-<P>`` or ``-all``, which lists every
record and, under ``unmapped``, the records that map to no bead or have no readable verdict. A rerun replaces it.
A pointer over ``QUOTAS.MAX_DOCUMENT_BYTES`` is written in titled parts.

The module writes nothing to T2: no TTL, no tag, no content change (Step 3 owns the TTL).
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from nexus.db.limits import QUOTAS
from nexus.memory_curate import _RDR_TAG, _phases_of
from nexus.tables.audit_tags import split_tags

_log = structlog.get_logger(__name__)

ARCHIVE_SUBJECT = "nexus-rdr-research"
POINTER_CATEGORY = "gate_pointer"
WRITER = "nx-memory-gate-rollup"
#: The verdict vocabulary the review agents mandate (``gate_record.lua`` OUTCOMES).
VERDICTS = frozenset({"justified", "partial", "not-justified"})

_HEADER_LINE = re.compile(r"^(kind|bead|rdr|sha|outcome|date):[ \t]*(.*?)\s*$")
#: An outcome line of an older record: ``- **outcome**: justified``, ``**outcome:** `partial```, ``outcome: justified``.
#: The last one in the body is the verdict, as in ``gate_record.lua``.
_OUTCOME_LINE = re.compile(r"^[ \t]*[-*]?[ \t]*(?:\*\*outcome:?\*\*:?|outcome:)[ \t]*[`*]*([A-Za-z-]+)", re.MULTILINE | re.IGNORECASE)
_PHASE_ARG = re.compile(r"^0*(\d+)([a-z]?)$")


class GateRollupError(Exception):
    """A run that stops and names its cause (nothing the command does is silently skipped)."""


class ScopeError(ValueError):
    """A usage error in the command's options."""


# ── the scope ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Scope:
    rdr: int
    beads: tuple[str, ...] | None   # None: every phase of the RDR (--all)
    phase: str | None               # None with --all

    @property
    def label(self) -> str:
        return f"RDR-{self.rdr:03d} " + ("all phases" if self.phase is None else f"phase {self.phase}")


def parse_scope(*, rdr: int, phase: str | None, beads: str | None, all_phases: bool) -> Scope:
    """The command's options as a :class:`Scope`; :class:`ScopeError` for any combination that names no run."""
    if all_phases:
        if phase is not None or beads is not None:
            raise ScopeError("--all names every phase of the RDR: give it alone, without --phase or --beads")
        return Scope(rdr, None, None)
    if phase is None or beads is None:
        raise ScopeError("give --all, or --phase P with --beads b1,b2,... (the 'Item N = <bead-id>' lines of the "
                         "phase-review-gate close step)")
    m = _PHASE_ARG.match(phase.strip().lower())
    if m is None:
        raise ScopeError(f"--phase must be a phase number, with an optional sub-phase letter (3, 3a); got {phase!r}")
    ids = tuple(dict.fromkeys(b.strip() for b in beads.split(",") if b.strip()))
    if not ids:
        raise ScopeError("--beads names no bead")
    if any(re.search(r"\s", b) for b in ids):
        raise ScopeError(f"a bead id has no whitespace: {ids!r}")
    return Scope(rdr, ids, f"{int(m.group(1))}{m.group(2)}")


def pointer_title(rdr: int, phase: str | None) -> str:
    return f"gate-records-rdr-{rdr:03d}-" + ("all" if phase is None else f"phase-{phase}")


def part_titles(title: str, count: int) -> list[str]:
    """The titles of a note written in *count* parts: the first keeps *title*, the rest are ``<title> (part N)``."""
    return [title] + [f"{title} (part {n})" for n in range(2, count + 1)]


# ── the selection ────────────────────────────────────────────────────────────────

def title_in_phase(title: str, phase: str) -> bool:
    """True when a ``-p<N>`` / ``-phase-<N>`` piece of *title* names *phase*; a sub-phase (3a) counts toward its phase (3)."""
    return any(p == phase or p.rstrip("abcdefghijklmnopqrstuvwxyz") == phase for p in _phases_of(title))


def header_fields(content: str) -> dict[str, str]:
    """The field lines ``gate_record`` writes at the top of a record (kind, bead, rdr, sha, outcome, date).

    Reading stops at the first blank or non-field line, so a line of the body never stands in for a header field;
    the first occurrence of a key wins. An older record has no header and yields ``{}``."""
    out: dict[str, str] = {}
    for line in content.splitlines():
        m = _HEADER_LINE.match(line)
        if m is None:
            break
        out.setdefault(m.group(1), m.group(2))
    return out


def verdict_of(content: str, header: dict[str, str]) -> str | None:
    """The record's verdict, or ``None`` when it is missing or outside the vocabulary.

    A headered record is read from its header ``outcome`` line only; an older record from the last outcome line of
    its body."""
    if "outcome" in header:
        value = header["outcome"].strip().lower()
    else:
        found = _OUTCOME_LINE.findall(content)
        value = found[-1].lower() if found else ""
    return value if value in VERDICTS else None


@dataclass
class Selection:
    listed: list[dict[str, Any]] = field(default_factory=list)
    unmapped: list[dict[str, Any]] = field(default_factory=list)


def _entry(project: str, row: dict[str, Any], header: dict[str, str], bead: str | None,
           verdict: str | None) -> dict[str, Any]:
    return {
        "project": project, "title": row["title"], "kind": header.get("kind"), "bead": bead,
        "sha": header.get("sha"), "verdict": verdict,
    }


def select_records(rows: Iterable[dict[str, Any]], *, project: str, rdr: int, beads: Sequence[str] | None,
                   phase: str | None) -> Selection:
    """The gate records of one run, from the rows of one project (a pure function of *rows*).

    A gate record has the whole tag ``kind:gate``. It is listed when it belongs to a run bead (``bead:<id>``; with
    ``--all``, when it carries ``rdr:<NNN>`` instead), or, with ``--phase``, when it has no tags at all and its title
    names a run bead (:func:`title_names_bead`). Listed but unreadable (no verdict, or a title naming two run beads)
    and ``kind:gate`` records of the RDR with no ``bead:`` tag (under ``--phase``: whose title names the phase) are
    ``unmapped``. Everything else is left alone, a body that mentions ``kind:gate`` included."""
    sel = Selection()
    run_beads = tuple(beads) if beads is not None else None
    for row in rows:
        tags = split_tags(row.get("tags"))
        title = row["title"]
        content = row.get("content") or ""
        header = header_fields(content)
        bead: str | None = None
        if "kind:gate" in tags:
            in_rdr = any((m := _RDR_TAG.match(t)) and int(m.group(1)) == rdr for t in tags)
            tagged = [t[5:] for t in tags if t.startswith("bead:") and len(t) > 5]
            if tagged:
                bead = tagged[0]
                if not (bead in run_beads if run_beads is not None else in_rdr):
                    continue
            else:
                if not in_rdr or (phase is not None and not title_in_phase(title, phase)):
                    continue
                sel.unmapped.append({**_entry(project, row, header, None, verdict_of(content, header)), "reason": "no bead tag"})
                continue
        else:
            continue
        verdict = verdict_of(content, header)
        if verdict is None:
            sel.unmapped.append({**_entry(project, row, header, bead, None), "reason": "verdict unreadable"})
            continue
        sel.listed.append(_entry(project, row, header, bead, verdict))
    key = lambda e: e["title"]  # noqa: E731 — one-line sort key, used twice
    sel.listed.sort(key=key)
    sel.unmapped.sort(key=key)
    return sel


# ── splitting ────────────────────────────────────────────────────────────────────

def split_text_bytes(text: str, cap: int) -> list[str]:
    """Cut *text* into pieces of at most *cap* UTF-8 bytes, after a newline where one fits, else inside a line.

    ``"".join(result) == text`` always, and no piece is empty (an empty *text* gives one empty piece)."""
    if len(text.encode()) <= cap:
        return [text]
    pieces: list[str] = []
    cur: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal cur, size
        if cur:
            pieces.append("".join(cur))
        cur, size = [], 0

    for line in text.splitlines(keepends=True):
        n = len(line.encode())
        if n > cap:
            flush()
            piece, used = "", 0
            for ch in line:
                w = len(ch.encode())
                if used + w > cap:
                    pieces.append(piece)
                    piece, used = "", 0
                piece += ch
                used += w
            cur, size = [piece], used
            continue
        if size + n > cap:
            flush()
        cur.append(line)
        size += n
    flush()
    return pieces


# ── the pointer ──────────────────────────────────────────────────────────────────

@dataclass
class Pointer:
    listed: list[dict[str, Any]]
    unmapped: list[dict[str, Any]]
    parts: int = 1
    beads: tuple[str, ...] | None = None


def _pointer_text(scope: Scope, project: str, sel: Selection, parts: int) -> str:
    lines = [f"gate-rollup pointer: {scope.label}", f"project: {project}"]
    if scope.beads is not None:
        lines.append(f"beads: {','.join(scope.beads)}")
    lines += [
        f"listed: {len(sel.listed)}", f"unmapped: {len(sel.unmapped)}", f"parts: {parts}",
        "## listed", *(json.dumps(e, ensure_ascii=False) for e in sel.listed),
        "## unmapped", *(json.dumps(e, ensure_ascii=False) for e in sel.unmapped),
    ]
    return "\n".join(lines) + "\n"


def render_pointer(scope: Scope, project: str, sel: Selection) -> tuple[str, int]:
    """``(text, parts)``: the pointer's text and how many notes of at most the store_put cap it makes.

    The text states its own part count, so the count and the text are settled together."""
    parts = 1
    for _ in range(6):
        text = _pointer_text(scope, project, sel, parts)
        needed = len(split_text_bytes(text, QUOTAS.MAX_DOCUMENT_BYTES))
        if needed == parts:
            return text, parts
        parts = needed
    raise GateRollupError("the pointer's part count did not settle")


def parse_pointer(text: str) -> Pointer:
    """The inverse of :func:`render_pointer`: each record line is one JSON object, read under its section header."""
    sections: dict[str, list[dict[str, Any]]] = {"listed": [], "unmapped": []}
    current: str | None = None
    parts, beads = 1, None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
        elif current in sections and line.startswith("{"):
            sections[current].append(json.loads(line))
        elif current is None and line.startswith("parts: "):
            parts = int(line[7:])
        elif current is None and line.startswith("beads: "):
            beads = tuple(line[7:].split(","))
    return Pointer(sections["listed"], sections["unmapped"], parts, beads)


# ── the T3 side ──────────────────────────────────────────────────────────────────

def collection_name(t3: Any, *, for_write: bool = False) -> str:
    from nexus.corpus import t3_collection_name  # noqa: PLC0415 — deferred: heavy import, only the T3 side needs it

    return t3_collection_name(ARCHIVE_SUBJECT, t3=t3, for_write=for_write)


def read_note(t3: Any, collection: str, title: str) -> str | None:
    """The whole text of the note titled *title* in *collection*, or ``None`` when it is missing or unreadable.

    The read ``store_get`` makes: one chunk is read as it is; several chunks of one note are joined by the catalog
    manifest. It stamps nothing."""
    from nexus.catalog.store_hook import split_note_text  # noqa: PLC0415 — deferred: catalog import graph
    from nexus.sensitivity import read_sensitivity  # noqa: PLC0415 — deferred, as above

    try:
        ids = t3.find_ids_by_title(collection, title)
    except Exception as exc:  # noqa: BLE001 — a collection no write has made yet reads as "no note"
        _log.debug("gate_rollup_read_note_failed", collection=collection, title=title, error=str(exc))
        return None
    if not ids:
        return None
    sensitivity = read_sensitivity()
    if len(ids) > 1:
        split = split_note_text(t3, collection, ids, sensitivity=sensitivity)
        return None if split is None else split[1]
    entry = t3.get_by_id(collection, ids[0], sensitivity=sensitivity)
    return None if entry is None else entry.get("content", "")


def read_parts(t3: Any, collection: str, title: str, parts: int) -> str | None:
    """The text of a note written by :func:`write_parts`, joined; ``None`` when any part is missing."""
    got = [read_note(t3, collection, t) for t in part_titles(title, parts)]
    return None if any(p is None for p in got) else "".join(p for p in got if p is not None)


def read_pointer(t3: Any, title: str) -> Pointer | None:
    """The pointer record titled *title*, joined from its parts, or ``None`` when absent or any part is missing."""
    col = collection_name(t3)
    first = read_note(t3, col, title)
    if first is None:
        return None
    declared = parse_pointer(first).parts
    whole = read_parts(t3, col, title, declared)
    return None if whole is None else parse_pointer(whole)


def store_note(t3: Any, *, title: str, content: str, tags: str, category: str) -> str:
    """Write one note through ``nx store put``'s writer and return its document id.

    The sequence ``nx store put`` runs: ``put_note`` (register, fence, write), the post-store chains, then the
    completion stamp. Any outcome that is not STORED stops the run with the one wording every note writer shares."""
    from nexus.catalog.note_write import failure_message, fire_note_chains, put_note, stamp_note  # noqa: PLC0415 — deferred: catalog import graph
    from nexus.errors import NoteUriEngineTooOldError, PutOversizedError  # noqa: PLC0415 — deferred, as above

    try:
        outcome = put_note(content=content, collection=collection_name(t3, for_write=True), title=title, tags=tags,
                           category=category, source_agent=WRITER)
    except (PutOversizedError, NoteUriEngineTooOldError) as exc:
        raise GateRollupError(f"{title!r} was not stored: {exc}") from exc
    message = failure_message(outcome, subject=repr(title), check="'nx store list'")
    if message is not None:
        raise GateRollupError(message)
    fire_note_chains(outcome, content)
    outcome = stamp_note(outcome)
    message = failure_message(outcome, subject=repr(title), check="'nx store list'")
    if message is not None:
        raise GateRollupError(message)
    return outcome.doc_id


def write_parts(t3: Any, *, title: str, text: str, tags: str, category: str) -> int:
    """Write *text* as one note, or as ``<title>``, ``<title> (part 2)``... when it is over the store_put cap.

    Returns the part count. The notes are read back and compared with *text* before this returns; a note that does
    not read back whole raises :class:`GateRollupError` naming it."""
    pieces = split_text_bytes(text, QUOTAS.MAX_DOCUMENT_BYTES)
    for name, piece in zip(part_titles(title, len(pieces)), pieces, strict=True):
        store_note(t3, title=name, content=piece, tags=tags, category=category)
    if read_parts(t3, collection_name(t3), title, len(pieces)) != text:
        raise GateRollupError(f"{title!r} did not read back from T3 as it was written; nothing after it was written")
    return len(pieces)


# ── the run ──────────────────────────────────────────────────────────────────────

@dataclass
class Result:
    scope: Scope
    selection: Selection
    lines: list[str] = field(default_factory=list)
    pointer_title: str = ""
    pointer_parts: int = 0
    stale: list[str] = field(default_factory=list)


def _listing_lines(scope: Scope, project: str, sel: Selection) -> list[str]:
    out = [f"{scope.label} in {project}: {len(sel.listed)} listed, {len(sel.unmapped)} unmapped"]
    out += [f"  listed    {e['verdict']:<13} {e['bead']}  {e['title']}" for e in sel.listed]
    out += [f"  unmapped  {e['title']} ({e['reason']})" for e in sel.unmapped]
    return out


def run(rows: Iterable[dict[str, Any]], *, project: str, scope: Scope, dry_run: bool,
        t3_factory: Callable[[], Any]) -> Result:
    """List the run's records from *rows*, then (unless *dry_run*) write the pointer. Nothing is written to T2."""
    sel = select_records(rows, project=project, rdr=scope.rdr, beads=scope.beads, phase=scope.phase)
    res = Result(scope, sel, _listing_lines(scope, project, sel))
    res.pointer_title = pointer_title(scope.rdr, scope.phase)
    if not sel.listed and not sel.unmapped:
        res.lines.append("nothing to write: no pointer record")
        return res
    text, res.pointer_parts = render_pointer(scope, project, sel)
    if dry_run:
        res.lines += [
            f"dry run: would write pointer {res.pointer_title!r} ({len(text.encode())} bytes, "
            f"pointer parts: {res.pointer_parts}); nothing written"]
        return res
    t3 = t3_factory()
    col = collection_name(t3)
    write_parts(t3, title=res.pointer_title, text=text, tags=f"gate-pointer,rdr:{scope.rdr:03d}",
                category=POINTER_CATEGORY)
    n = res.pointer_parts + 1
    while read_note(t3, col, f"{res.pointer_title} (part {n})") is not None:
        res.stale.append(f"{res.pointer_title} (part {n})")
        n += 1
    res.lines += [
        f"pointer: {res.pointer_title} ({len(text.encode())} bytes), pointer parts: {res.pointer_parts}",
    ]
    res.lines += [f"stale: {t!r} is left from an earlier, longer pointer; nothing here deletes it" for t in res.stale]
    return res
