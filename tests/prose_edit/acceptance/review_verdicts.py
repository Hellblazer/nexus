#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verdicts for the review-loop scenarios (RDR-221 Steps 1.1 and 1.5, nexus-ger02.4).

usage: review_verdicts.py OUT_DIR

OUT_DIR holds what run-review.sh writes: NAME.jsonl for each turn of the three scenarios.

  a1 a2 a3 a4   reject, then run again: turn 1 renders the copy, turn 2 is the author's answer (accept edit 1,
                reject the rest), turn 3 is a fresh run on the same document, turn 4 is the answer "hold everything"
  b1 b2 b3 b4   list the stored rejections, remove number 1, run again, hold everything
  c1 c2         a stdin run with --genre commit-message: the copy, then the author's answer
  a2c a4c b4c c2c   the author's confirmation of the dry-run echo the skill showed for the answer turn

An answer turn must end at `apply --dry-run` (no real apply in it); the real apply is the confirmation turn's.

Each verdict reads the tool results the skill's own scripts printed, so it checks what the author would have
seen, not what the model claims.

a3 scores each edit the author rejected in turn 2 against what the fresh run of turn 3 showed. A rejected fix
shown again is a FAIL, whatever span the editor picked for it; a fresh run that proposed no edit at all is
NOT-MEASURABLE (an editor with nothing left to say shows no rejected fix either), never a PASS: `recurrence` counts an edit with the same
minimal change, or a cut that overlaps the rejected words, as that fix again (the definition is in README.md,
"The rejection-memory gate"). A fix the filter dropped, or the editor never proposed, is not shown: both PASS,
and the note says how many of each, plus how many edits the fresh run showed. A3 is never VACUOUS, and no
scenario is run again until it passes: a retry that keeps the run in which the editor happened to behave selects
the pass. b3 is the one check that can still be VACUOUS (the editor did not propose the removed fix again, so
removal was not exercised); it matches the fix as a3 does, not the exact old string, and it is reported as it
is.

run-review.sh records the groups it was asked to run (SCENARIOS, default a b c) in OUT_DIR/scenarios.txt. A group
that is not listed there and left no transcript is NOT-RUN, never FAIL; a listed group with no transcript, or a
directory with no scenarios.txt, keeps failing, and a group that ran is scored whether or not it was listed. Exit 0
when every row is PASS, 3 when every row is PASS or NOT-RUN (part of the matrix was not asked for), 1 otherwise.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

Obj = dict[str, Any]
HERE = Path(__file__).resolve().parent


def _verdicts() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prose_edit_verdicts_lib", HERE / "verdicts.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


V = _verdicts()


def _memory() -> ModuleType:
    path = HERE.parents[2] / ".claude" / "skills" / "prose-edit" / "scripts" / "memory.py"
    spec = importlib.util.spec_from_file_location("prose_edit_memory_for_verdicts", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


M = _memory()


def same_fix(rejected: Obj, other: Obj) -> str | None:
    """How `other` is the fix `rejected` again: "same-change" (the same minimal change, which is what the
    filter matches), "overlap" (the same replacement text over changed words that contain, or are contained
    in, the rejected ones: a cut of words the author wanted kept is that fix again), or None."""
    gone_r, put_r = M.change_key(str(rejected["old"]), str(rejected.get("new", "")))
    gone_o, put_o = M.change_key(str(other["old"]), str(other.get("new", "")))
    if (gone_r, put_r) == (gone_o, put_o):
        return "same-change"
    if put_r == put_o and gone_r.strip() and gone_o.strip():
        words_r, words_o = f" {' '.join(gone_r.split())} ", f" {' '.join(gone_o.split())} "
        if words_r in words_o or words_o in words_r:
            return "overlap"
    return None


def recurrence(rejected: list[Obj], shown: list[Obj], dropped: list[Obj]) -> list[Obj]:
    """One row per rejected edit: "shown-again" when an edit in `shown` is that fix again (with how, and which
    edit numbers), "dropped" when the filter dropped it (cause rejected), else "absent" (never proposed)."""
    rows: list[Obj] = []
    for rej in rejected:
        hits = [(same_fix(rej, e), e.get("n")) for e in shown]
        hits = [(how, n) for how, n in hits if how]
        row: Obj = {"old": rej["old"], "new": rej.get("new", "")}
        if hits:
            row.update(status="shown-again", how=hits[0][0], by=[n for _, n in hits])
        elif any(d.get("cause") == "rejected" and same_fix(rej, d) == "same-change" for d in dropped):
            row.update(status="dropped", how="same-change", by=[])
        else:
            row.update(status="absent", how="", by=[])
        rows.append(row)
    return rows


def first_json(text: str) -> Obj | None:
    start = text.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def script_results(events: list[Obj], script: str, verb: str) -> list[Obj]:
    """The JSON each `python3 .../<script>.py <verb> ...` Bash call printed, in call order."""
    results = V.tool_results(events)
    out: list[Obj] = []
    for tid, name, inp, _ in V.tool_uses(events, sub=False):
        command = str(inp.get("command", ""))
        if name == "Bash" and f"scripts/{script}.py {verb}" in command and tid in results:
            parsed = first_json(results[tid])
            if parsed is not None:
                out.append(parsed)
    return out


def last(events: list[Obj], script: str, verb: str) -> Obj | None:
    found = script_results(events, script, verb)
    return found[-1] if found else None


def real_apply(events: list[Obj]) -> Obj | None:
    """The last `review.py apply` that changed things (not the --dry-run echo)."""
    found = [r for r in script_results(events, "review", "apply") if not r.get("dry_run")]
    return found[-1] if found else None


def dry_apply(events: list[Obj]) -> Obj | None:
    found = [r for r in script_results(events, "review", "apply") if r.get("dry_run")]
    return found[-1] if found else None


def answer_turn(events: list[Obj]) -> tuple[bool, str]:
    """The author's answer ends at the echo: a dry run was shown and nothing was applied yet."""
    echoed, applied = dry_apply(events), real_apply(events)
    return bool(echoed) and not applied, f"echo={bool(echoed)} applied-early={bool(applied)}"


def clean(events: list[Obj]) -> tuple[bool, str]:
    c = V.compliance(events, None)
    bad = bool(c["denied"] or c["off_list_bash"] or c["nx_attempts"])
    return not bad, f"denied={c['denied']} off-list={len(c['off_list_bash'])} nx={len(c['nx_attempts'])}"


def check_a(runs: dict[str, list[Obj]]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    a1, a2, a3, a4 = (runs.get(k, []) for k in ("a1", "a2", "a3", "a4"))
    filt1, render1, apply2 = last(a1, "brief", "filter"), last(a1, "review", "render"), real_apply(runs.get("a2c", []))
    ok, note = clean(a1)
    out.append(("a1", "PASS" if ok and filt1 and render1 and render1.get("counts", {}).get("edits", 0) >= 2 else "FAIL",
                f"filtered={bool(filt1)} rendered={bool(render1)} {note}"))
    rejected_old: list[str] = []
    if filt1 and apply2:
        by_n = {e["n"]: e["old"] for e in filt1.get("edits", [])}
        rejected_old = [by_n[n] for n in apply2.get("rejected", []) if n in by_n]
    ok, note = clean(a2)
    held_back, echo_note = answer_turn(a2)
    ok_c, note_c = clean(runs.get("a2c", []))
    good = bool(apply2 and 1 in [e["n"] for e in apply2.get("applied", [])] and rejected_old
                and apply2.get("rejections_stored") is True)
    out.append(("a2", "PASS" if ok and ok_c and held_back and good else "FAIL",
                f"{echo_note} applied={[e['n'] for e in (apply2 or {}).get('applied', [])]} "
                f"rejected={(apply2 or {}).get('rejected')} {note} {note_c}"))
    filt3 = last(a3, "brief", "filter")
    ok, note = clean(a3)
    if not filt3:
        out.append(("a3", "FAIL", f"no filter result {note}"))
    elif not rejected_old:
        out.append(("a3", "FAIL", f"nothing was rejected in turn 2, so turn 3 proves nothing; {note}"))
    else:
        by_n1 = {e["n"]: e for e in (filt1 or {}).get("edits", [])}
        rejected = [by_n1[n] for n in (apply2 or {}).get("rejected", []) if n in by_n1]
        rows = recurrence(rejected, filt3.get("edits", []), filt3.get("dropped", []))
        again = [r for r in rows if r["status"] == "shown-again"]
        counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("shown-again", "dropped", "absent")}
        shown_n, dropped_n = len(filt3.get("edits", [])), len(filt3.get("dropped", []))
        detail = (f"rejected={len(rows)} shown-again={counts['shown-again']} dropped-by-filter={counts['dropped']} "
                  f"not proposed again={counts['absent']}; fresh-run edits shown={shown_n} dropped={dropped_n}; {note}")
        if shown_n + dropped_n == 0:
            # a fresh session with nothing to say shows no rejected fix because it shows no fix: the run
            # measured an editor with nothing left to propose, not memory
            out.append(("a3", "NOT-MEASURABLE", f"the fresh run proposed no edits, so it proves nothing about memory; {detail}"))
        elif again:
            out.append(("a3", "FAIL", "a rejected fix was shown again: "
                        + "; ".join(f"{r['how']} {r['old']!r}->{r['new']!r} by edit {r['by']}" for r in again) + f"; {detail}"))
        else:
            out.append(("a3", "PASS" if ok else "FAIL", detail))
    apply4 = real_apply(runs.get("a4c", []))
    ok, note = clean(a4)
    held_back, echo_note = answer_turn(a4)
    out.append(("a4", "PASS" if ok and held_back and apply4 and not apply4.get("rejected") and not apply4.get("applied")
                else "FAIL", f"{echo_note} held={(apply4 or {}).get('held')} rejected={(apply4 or {}).get('rejected')} {note}"))
    return out


def check_b(runs: dict[str, list[Obj]]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    b1, b2, b3, b4 = (runs.get(k, []) for k in ("b1", "b2", "b3", "b4"))
    listed = last(b1, "memory", "rejections")
    ok, note = clean(b1)
    out.append(("b1", "PASS" if ok and listed and listed.get("rejections") else "FAIL",
                f"listed={len((listed or {}).get('rejections', []))} {note}"))
    removed = last(b2, "memory", "rejections")
    ok, note = clean(b2)
    gone: Obj | None = None
    if listed and removed:
        after = [(r["old"], r.get("new", "")) for r in removed.get("rejections", [])]
        before = [r for r in listed.get("rejections", [])]
        missing = [r for r in before if (r["old"], r.get("new", "")) not in after]
        gone = missing[0] if len(missing) == 1 and len(after) == len(before) - 1 else None
    out.append(("b2", "PASS" if ok and gone else "FAIL", f"removed={(gone or {}).get('old')!r} {note}"))
    filt3 = last(b3, "brief", "filter")
    ok, note = clean(b3)
    if not filt3 or not gone:
        out.append(("b3", "FAIL", f"no filter result or nothing removed {note}"))
    else:
        # the fix, not the string: the editor picks its span afresh, so the removed edit is "back" when an
        # edit with the same minimal change (or the same cut over the same words) is shown, and "still held"
        # when the filter dropped such an edit
        gone_fix = {"old": gone["old"], "new": gone.get("new", "")}
        proposed = [e for e in filt3.get("edits", []) if same_fix(gone_fix, e)]
        dropped = [d for d in filt3.get("dropped", []) if d.get("cause") == "rejected" and same_fix(gone_fix, d) == "same-change"]
        if dropped:
            out.append(("b3", "FAIL", "the removed rejection still dropped the edit"))
        elif not proposed:
            out.append(("b3", "VACUOUS", f"the editor did not propose the removed edit again; {note}"))
        else:
            out.append(("b3", "PASS" if ok else "FAIL", f"the removed edit is proposed again; {note}"))
    apply4 = real_apply(runs.get("b4c", []))
    ok, note = clean(b4)
    held_back, echo_note = answer_turn(b4)
    out.append(("b4", "PASS" if ok and held_back and apply4 and not apply4.get("rejected") else "FAIL",
                f"{echo_note} held={(apply4 or {}).get('held')} {note}"))
    return out


def check_c(runs: dict[str, list[Obj]]) -> list[tuple[str, str, str]]:
    c1, c2 = runs.get("c1", []), runs.get("c2", [])
    render = last(c1, "review", "render")
    ok1, note1 = clean(c1)
    apply = real_apply(runs.get("c2c", []))
    ok2, note2 = clean(c2)
    held_back, echo_note = answer_turn(c2)
    return [
        ("c1", "PASS" if ok1 and render and render.get("genre") == "commit-message" else "FAIL",
         f"rendered={bool(render)} {note1}"),
        ("c2", "PASS" if ok2 and held_back and apply and apply.get("mode") == "stdin" and apply.get("applied") == []
         and isinstance(apply.get("text"), str) and apply.get("rejections_stored") is False
         and (apply.get("log") or {}).get("title", "").startswith("log/stdin/") else "FAIL",
         f"{echo_note} mode={(apply or {}).get('mode')} applied={(apply or {}).get('applied')} "
         f"stored={(apply or {}).get('rejections_stored')} log={((apply or {}).get('log') or {}).get('title')} {note2}"),
    ]


# What each scenario group writes: the rows it scores and the transcripts it leaves.
GROUPS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "a": (("a1", "a2", "a3", "a4"), ("a1", "a2", "a2c", "a3", "a4", "a4c")),
    "b": (("b1", "b2", "b3", "b4"), ("b1", "b2", "b3", "b4", "b4c")),
    "c": (("c1", "c2"), ("c1", "c2", "c2c")),
}
CHECKS = {"a": check_a, "b": check_b, "c": check_c}


def requested(out_dir: Path) -> set[str] | None:
    """The scenario groups run-review.sh was asked to run (scenarios.txt), or None when the run left no record."""
    path = out_dir / "scenarios.txt"
    if not path.is_file():
        return None
    return set(path.read_text(encoding="utf-8").split())


def group_rows(group: str, runs: dict[str, list[Obj]], asked: set[str] | None) -> list[tuple[str, str, str]]:
    """The rows of one group. A group the runner was not asked for, with no transcript, is NOT-RUN: it never
    started, so it did not fail. With no record of what was asked, an absent group is not known to be unrequested
    and keeps failing; a group that was asked for and left nothing fails; a group that ran is always scored."""
    rows, transcripts = GROUPS[group]
    if asked is not None and group not in asked and not any(t in runs for t in transcripts):
        return [(r, "NOT-RUN", f"scenario group {group} was not requested (scenarios.txt lists "
                 f"{' '.join(sorted(asked)) or 'none'}) and left no transcript") for r in rows]
    return CHECKS[group](runs)


def main(argv: list[str]) -> int:
    if not argv:
        sys.stdout.write((__doc__ or "") + "\n")
        return 2
    out_dir = Path(argv[0])
    runs = {p.stem: V.load_events(p) for p in sorted(out_dir.glob("*.jsonl"))}
    asked = requested(out_dir)
    rows = [row for group in GROUPS for row in group_rows(group, runs, asked)]
    for name, verdict, note in rows:
        sys.stdout.write(f"{name:3} {verdict:8} {note}\n")
    verdicts = {v for _, v, _ in rows}
    if verdicts == {"PASS"}:
        return 0
    return 3 if verdicts <= {"PASS", "NOT-RUN"} else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
