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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus.memory_curate import _all_closed, closed_phase_numbers, closed_rdr_numbers
from nexus.memory_curate import gate_rollup as gr

SEVERITY = {"justified": 0, "partial": 1, "not-justified": 2}


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


# ── check 2: titles resolve ──────────────────────────────────────────────────────

def merge_previous(t3: Any, scope: gr.Scope, sel: gr.Selection) -> tuple[list[dict[str, Any]], list[str]]:
    """``(carried, unresolvable)``: the earlier pointer's records this run's read no longer lists.

    A carried record keeps its archive; one whose archive no longer reads back whole cannot be resolved, and the
    run stops naming it."""
    prev = gr.read_pointer(t3, gr.pointer_title(scope.rdr, scope.phase))
    if prev is None:
        return [], []
    live = {e["title"] for e in sel.listed}
    col = gr.collection_name(t3)
    carried: list[dict[str, Any]] = []
    bad: list[str] = []
    for rec in prev.listed:
        if rec["title"] in live:
            continue
        if gr.read_parts(t3, col, rec["archive"], rec["parts"]) is None:
            bad.append(rec["title"])
        else:
            carried.append({**rec, "state": "absent"})
    return carried, bad


# ── the run ──────────────────────────────────────────────────────────────────────

def run(mem: Any, *, project: str, scope: gr.Scope, dry_run: bool, accept_open: tuple[str, ...],
        root: Path, t3_factory: Any) -> Result:
    """The command after option parsing. Every refusal is a :class:`~nexus.memory_curate.gate_rollup.GateRollupError`
    raised before anything is written."""
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

    if not sel.listed and not sel.unmapped:
        res.lines.append("nothing to write: no pointer record")
        return res
    t3 = t3_factory()
    res.carried, bad = merge_previous(t3, scope, sel)
    if bad:
        raise gr.GateRollupError(
            f"the earlier pointer lists {', '.join(bad)}, which is neither in this read nor archived in T3 any more; "
            "nothing was written")
    if dry_run:
        res.lines.append("dry run: archives, the pointer write and its read-back skipped; nothing written")
        return res

    text, res.pointer_parts = gr.render_pointer(scope, project, sel, accepted_open=accept_open, carried=res.carried)
    res.archived, res.already_whole = gr.archive_records(t3, sel, scope)
    res.stale = gr.write_pointer(t3, scope, text, res.pointer_parts)
    back = gr.read_pointer(t3, res.pointer_title)
    want = {(e["project"], e["title"]) for e in [*sel.listed, *res.carried]}
    if back is None or {(e["project"], e["title"]) for e in back.listed} != want:
        raise gr.GateRollupError(f"the pointer {res.pointer_title!r} did not read back from T3 with the same list")
    res.lines += [
        f"archived {res.archived} record(s), {res.already_whole} already whole",
        f"pointer: {res.pointer_title} ({len(text.encode())} bytes), pointer parts: {res.pointer_parts}",
    ]
    if res.carried:
        res.lines.append(f"carried: {len(res.carried)} record(s) an earlier pointer listed that this read does not")
    res.lines += [f"stale: {t!r} is left from an earlier, longer pointer; nothing here deletes it" for t in res.stale]
    return res
