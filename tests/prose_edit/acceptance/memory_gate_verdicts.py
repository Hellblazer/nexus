#!/usr/bin/env python3
"""The rejection-memory gate's counts (RDR-221, nexus-ger02.16).

usage: memory_gate_verdicts.py OUT_DIR [--threshold 0.10] [--min-measurable N] [--source LABEL=PATH ...]

OUT_DIR holds what run-memory-gate.sh wrote: LABEL-<k>-<turn>.jsonl for turns 1 to 4 of each run. The
threshold, the definitions of rejected and shown again, and the rule that no run is repeated are in
README.md ("The rejection-memory gate") and were fixed before the first run.

For each run: R is the number of edits the skill stored as rejections in turn 3 (they are the edits shown in
turn 1); each is then classified against what the fresh session of turn 4 produced:

  shown-again  an edit in the edits of the last `brief.py filter` is that fix again (review_verdicts.recurrence)
  dropped      not shown: the filter dropped an edit with the same minimal change (cause "rejected")
  absent       not shown: the editor did not propose it

The recurrence rate is shown-again divided by R, summed over the measurable runs of a document and over all
runs. Three kinds of run are kept apart, and none is replaced:

  measurable      turn 4 proposed at least one edit (shown by the filter, or dropped by it). Only these count.
  NOT-MEASURABLE  turn 4 proposed no edit at all. A fresh session with nothing left to say shows no rejected fix
                  because it shows no fix, so the run says nothing about memory: it is listed with its R and
                  adds nothing to the rate. It is never a pass.
  ERROR           the run did not get as far as a stored rejection and a turn 4 filter result.

Each run line reports the edits turn 4 showed (turn4-edits) and proposed (shown plus dropped by the filter);
each document line reports their total and the range per run. When the document text is known (--source
LABEL=PATH, or LABEL-source.txt in OUT_DIR, which run-memory-gate.sh writes) the "same spot, other replacement"
column is reported too: a shown edit whose old text overlaps a rejected edit's old text in the document, with a
different change. It is NOT counted in the rate (Sam's decision: overlap alone is not a match, and the filter does
not drop it); it shows what the brief and the filter leave to the author. Because the whole old text is compared,
another edit in the same sentence counts: the column is an upper bound. When the author held some edits instead
of rejecting them, each run also reports how many of the held edits turn 4 showed again (held-shown-again): the
positive control, since a held edit is a legitimate proposal; also never counted in the rate.

Exit 0 when the pooled rate is at most the threshold, 1 when it is above it, 2 on a usage error or when nothing
was measurable (no measurable run, or no stored rejection), 3 when the rate passes but a document has fewer
measurable runs than --min-measurable (default 0: not checked; the decision asks for 10).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

Obj = dict[str, Any]
HERE = Path(__file__).resolve().parent
THRESHOLD = 0.10
_NAME = re.compile(r"(?P<label>.+)-(?P<k>[0-9]+)-(?P<turn>[1-4])")


def _load(name: str, file: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


RV = _load("prose_edit_review_verdicts_for_gate", "review_verdicts.py")
V = RV.V


def _spans(doc: str, old: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    if not old:
        return out
    i = doc.find(old)
    while i >= 0:
        out.append((i, i + len(old)))
        i = doc.find(old, i + 1)
    return out


def same_spot_other(rejected: Obj, other: Obj, doc: str) -> bool | None:
    """True when `other` is at the same place in `doc` as `rejected` (their old texts overlap) and is NOT that
    fix again (`RV.same_fix` finds nothing); False when it is elsewhere or is the fix again; None when either old
    text is not in `doc`, so the place is unknown."""
    mine, theirs = _spans(doc, str(rejected["old"])), _spans(doc, str(other["old"]))
    if not mine or not theirs:
        return None
    if RV.same_fix(rejected, other):
        return False
    return any(a < d and c < b for a, b in mine for c, d in theirs)


def run_row(events: dict[int, list[Obj]], doc: str | None = None) -> Obj:
    """One run's classified rejections, or {"error": ...} when the run did not reach a measurable state, or
    {"not_measurable": ...} when it did but turn 4 proposed no edit at all (the fresh session had nothing to say)."""
    filt1 = RV.last(events.get(1, []), "brief", "filter")
    if not filt1:
        return {"error": "turn 1 produced no filter result"}
    applied = RV.real_apply([*events.get(2, []), *events.get(3, [])])
    if not applied:
        return {"error": "no real apply in turns 2-3"}
    if applied.get("rejections_stored") is not True:
        # R = 0 is a real outcome when the editor proposed nothing; anything else means the answer did not land
        if filt1.get("edits"):
            return {"error": f"apply stored no rejections (rejected={applied.get('rejected')}, held={applied.get('held')})"}
    filt4 = RV.last(events.get(4, []), "brief", "filter")
    if not filt4:
        return {"error": "turn 4 produced no filter result"}
    by_n = {e["n"]: e for e in filt1.get("edits", [])}
    rejected = [by_n[n] for n in applied.get("rejected", []) if n in by_n]
    shown, dropped = filt4.get("edits", []), filt4.get("dropped", [])
    base: Obj = {"R": len(rejected), "shown_edits": len(shown), "dropped_edits": len(dropped),
                 "proposed": len(shown) + len(dropped)}
    if not base["proposed"]:
        return {**base, "not_measurable": "turn 4 proposed no edits"}
    rows = RV.recurrence(rejected, shown, dropped)
    # the positive control: an edit the author held, not rejected, is a legitimate proposal, so a fresh session
    # that suppresses it as well is over-suppressing. Reported, never counted in the rate.
    held = [by_n[n] for n in applied.get("held", []) if n in by_n]
    base["held"] = len(held)
    base["held_again"] = sum(1 for r in RV.recurrence(held, shown, dropped) if r["status"] == "shown-again")
    for row, rej in zip(rows, rejected, strict=True):
        hits = [(e, same_spot_other(rej, e, doc)) for e in shown] if doc is not None else []
        row["spot_by"] = [e for e, h in hits if h]
        # True: some shown edit is at the same place with another change; None: the place is unknown (no
        # document text, or an old text that is not in it); False: none is
        row["same_spot"] = (None if doc is None else True if row["spot_by"]
                            else None if any(h is None for _, h in hits) else False)
    return {**base, "rows": rows}


def collect(out_dir: Path, sources: dict[str, str] | None = None) -> dict[str, dict[int, Obj]]:
    """label -> run number -> run_row, from every LABEL-<k>-<turn>.jsonl in out_dir. `sources` gives the document
    text per label (LABEL-source.txt in out_dir is read when the label has no entry)."""
    turns: dict[tuple[str, int], dict[int, list[Obj]]] = {}
    for path in sorted(out_dir.glob("*.jsonl")):
        m = _NAME.fullmatch(path.stem)
        if m:
            turns.setdefault((m.group("label"), int(m.group("k"))), {})[int(m.group("turn"))] = V.load_events(path)
    texts = dict(sources or {})
    out: dict[str, dict[int, Obj]] = {}
    for (label, k), events in sorted(turns.items()):
        if label not in texts and (out_dir / f"{label}-source.txt").is_file():
            texts[label] = (out_dir / f"{label}-source.txt").read_text(encoding="utf-8")
        out.setdefault(label, {})[k] = run_row(events, texts.get(label))
    return out


_KEYS = ("runs", "measurable", "not_measurable", "errors", "R", "shown", "dropped", "absent", "same_spot", "turn4",
         "held", "held_again")


def summarize(runs: dict[str, dict[int, Obj]]) -> tuple[list[str], Obj]:
    lines: list[str] = []
    pooled: Obj = {key: 0 for key in _KEYS} | {"short": [], "spot_known": True, "counts": {}}
    for label, by_run in runs.items():
        tot: Obj = {key: 0 for key in _KEYS}
        edit_counts: list[int] = []
        spot_known = True
        for k, row in sorted(by_run.items()):
            tot["runs"] += 1
            if "error" in row:
                tot["errors"] += 1
                lines.append(f"{label}-{k}: ERROR {row['error']}")
                continue
            tot["turn4"] += row["shown_edits"]
            edit_counts.append(row["shown_edits"])
            if "not_measurable" in row:
                tot["not_measurable"] += 1
                lines.append(f"{label}-{k}: NOT-MEASURABLE {row['not_measurable']} (R={row['R']}, "
                             f"shown {row['shown_edits']}, dropped {row['dropped_edits']})")
                continue
            tot["measurable"] += 1
            rows = row["rows"]
            counts = {st: sum(1 for r in rows if r["status"] == st) for st in ("shown-again", "dropped", "absent")}
            tot["R"] += row["R"]
            tot["held"] += row["held"]
            tot["held_again"] += row["held_again"]
            tot["shown"] += counts["shown-again"]
            tot["dropped"] += counts["dropped"]
            tot["absent"] += counts["absent"]
            known = all(r.get("same_spot") is not None for r in rows)
            spot_known = spot_known and known
            spots = [r for r in rows if r.get("same_spot")]
            tot["same_spot"] += len(spots)
            again = [f"{r['how']}: {r['old']!r}->{r['new']!r} by {r['by']}" for r in rows if r["status"] == "shown-again"]
            lines.append(f"{label}-{k}: R={row['R']} turn4-edits={row['shown_edits']} proposed={row['proposed']} "
                         f"shown-again={counts['shown-again']} dropped={counts['dropped']} absent={counts['absent']}"
                         + (f" same-spot-other={len(spots)}" if known and rows else "")
                         + (f" held={row['held']} held-shown-again={row['held_again']}" if row["held"] else "")
                         + (f"  [{'; '.join(again)}]" if again else ""))
            for r in spots:
                for e in r["spot_by"]:
                    lines.append(f"  same spot, other replacement: {r['old']!r} -> {r['new']!r} by edit {e.get('n')} "
                                 f"({e['old']!r} -> {e.get('new', '')!r})")
        rate = f"{tot['shown'] / tot['R']:.1%}" if tot["R"] else "n/a"
        spot_text = str(tot["same_spot"]) if spot_known and tot["measurable"] else "n/a"
        span = f" ({min(edit_counts)} to {max(edit_counts)} per run)" if edit_counts else ""
        lines.append(f"== {label}: runs={tot['runs']} measurable={tot['measurable']} not-measurable={tot['not_measurable']} "
                     f"errors={tot['errors']} rejected={tot['R']} shown-again={tot['shown']} dropped={tot['dropped']} "
                     f"absent={tot['absent']} same-spot-other={spot_text} turn4-edits={tot['turn4']}{span}"
                     + (f" held={tot['held']} held-shown-again={tot['held_again']}" if tot["held"] else "")
                     + f" rate={rate}")
        pooled["counts"][label] = tot["measurable"]
        for key in _KEYS:
            pooled[key] += tot[key]
        pooled["spot_known"] = pooled["spot_known"] and spot_known
    return lines, pooled


def _sources(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, tok in enumerate(argv):
        if tok == "--source":
            label, sep, path = argv[i + 1].partition("=")
            if not sep or not Path(path).is_file():
                raise SystemExit(f"--source LABEL=PATH: {argv[i + 1]!r} is not a label and an existing file")
            out[label] = Path(path).read_text(encoding="utf-8")
    return out


def main(argv: list[str]) -> int:
    if not argv:
        sys.stdout.write((__doc__ or "") + "\n")
        return 2
    threshold = float(argv[argv.index("--threshold") + 1]) if "--threshold" in argv else THRESHOLD
    floor = int(argv[argv.index("--min-measurable") + 1]) if "--min-measurable" in argv else 0
    try:
        sources = _sources(argv)
    except SystemExit as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    runs = collect(Path(argv[0]), sources)
    lines, pooled = summarize(runs)
    sys.stdout.write("\n".join(lines) + "\n")
    if not pooled["measurable"]:
        sys.stdout.write("NOTHING MEASURABLE: every run was an error or had a turn 4 that proposed no edit\n")
        return 2
    if not pooled["R"]:
        sys.stdout.write("NO REJECTIONS: no run stored a rejection, so nothing was measured\n")
        return 2
    rate = pooled["shown"] / pooled["R"]
    verdict = "PASS" if rate <= threshold else "MISS"
    sys.stdout.write(f"POOLED: runs={pooled['runs']} measurable={pooled['measurable']} "
                     f"not-measurable={pooled['not_measurable']} errors={pooled['errors']} rejected={pooled['R']} "
                     f"shown-again={pooled['shown']} dropped={pooled['dropped']} absent={pooled['absent']} "
                     f"same-spot-other={pooled['same_spot'] if pooled['spot_known'] else 'n/a'} "
                     f"turn4-edits={pooled['turn4']}"
                     + (f" held={pooled['held']} held-shown-again={pooled['held_again']}" if pooled["held"] else "")
                     + f" rate={rate:.1%} threshold={threshold:.0%} {verdict}\n")
    short = {label: n for label, n in pooled["counts"].items() if n < floor}
    for label, n in sorted(short.items()):
        sys.stdout.write(f"SHORT: {label} has {n} measurable runs, fewer than the {floor} asked for\n")
    if verdict == "MISS":
        return 1
    return 3 if short else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
