#!/usr/bin/env python3
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
seen, not what the model claims. A scenario whose condition never arose (the editor did not propose the edit
the check needs) is VACUOUS and counts as a failure. The editor chooses the span of an edit afresh each run, so a
rejected edit often comes back with a different old string, which the exact-match filter rightly lets through;
run-review.sh therefore repeats a3 and b3 (up to four times) until the editor proposes the edit again, asking
`review_verdicts.py --probe NAME OUT_DIR` (exit 3 = vacuous, run again). Exit 1 on any FAIL or VACUOUS.
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
    else:
        dropped = [d for d in filt3.get("dropped", []) if d.get("cause") == "rejected"]
        proposed = [e["old"] for e in filt3.get("edits", [])]
        again = [o for o in rejected_old if o in proposed]
        if again:
            out.append(("a3", "FAIL", f"a rejected edit was shown again: {again}"))
        elif not dropped:
            out.append(("a3", "VACUOUS", f"the editor did not propose a rejected edit again; {note}"))
        else:
            out.append(("a3", "PASS" if ok else "FAIL", f"{len(dropped)} rejected edit(s) dropped before display; {note}"))
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
    gone = ""
    if listed and removed:
        before = [r["old"] for r in listed.get("rejections", [])]
        after = [r["old"] for r in removed.get("rejections", [])]
        missing = [o for o in before if o not in after]
        gone = missing[0] if len(missing) == 1 and len(after) == len(before) - 1 else ""
    out.append(("b2", "PASS" if ok and gone else "FAIL", f"removed={gone!r} {note}"))
    filt3 = last(b3, "brief", "filter")
    ok, note = clean(b3)
    if not filt3 or not gone:
        out.append(("b3", "FAIL", f"no filter result or nothing removed {note}"))
    else:
        proposed = [e["old"] for e in filt3.get("edits", [])]
        dropped = [d["old"] for d in filt3.get("dropped", []) if d.get("cause") == "rejected"]
        if gone in dropped:
            out.append(("b3", "FAIL", "the removed rejection still dropped the edit"))
        elif gone not in proposed:
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


def main(argv: list[str]) -> int:
    if not argv:
        sys.stdout.write((__doc__ or "") + "\n")
        return 2
    if argv[0] == "--probe":
        # run-review.sh asks after each rerun whether it proved anything: exit 3 when it is VACUOUS.
        name, probe_dir = argv[1], Path(argv[2])
        loaded = {p.stem: V.load_events(p) for p in sorted(probe_dir.glob("*.jsonl"))}
        rows = {n: v for n, v, _ in (check_a(loaded) if name.startswith("a") else check_b(loaded))}
        return 3 if rows.get(name) == "VACUOUS" else 0
    out_dir = Path(argv[0])
    runs = {p.stem: V.load_events(p) for p in sorted(out_dir.glob("*.jsonl"))}
    rows = check_a(runs) + check_b(runs) + check_c(runs)
    for name, verdict, note in rows:
        sys.stdout.write(f"{name:3} {verdict:8} {note}\n")
    return 0 if all(v == "PASS" for _, v, _ in rows) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
