#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verdicts for scenario 6 and step 13 of the prose-edit skill (RDR-221, nexus-ger02.6 critique E).

usage: teach_verdicts.py OUT_DIR
       teach_verdicts.py seed-json     the user-level entry run-teach.sh stores, as memory.py add-entry takes it

OUT_DIR holds what run-teach.sh writes:

  s6-1.jsonl                          scenario 6: a user-level entry, a document of another genre
  t1 t2 t2c t3 .jsonl                 step 13, the voice card: edit, answer, confirm, then the author's yes
  voice-card-after.json               `memory.py voice-card DOC`, run by the script after the yes
  promote-nodry.rc / .err             a real promote the script ran with no dry run (it must be refused)
  t4 t4c .jsonl                       step 13, promote: the skill's dry run and ask, then the confirm

Each verdict reads what the skill's own scripts printed, as the author would have seen it. Exit 1 on a FAIL, 3
when none failed but a run is missing or NOT-MEASURABLE (it proves nothing, so it is never a pass), 0 when every
check passed.

  s6         the entry is in the brief the editor read, with its layer label; the post-filter proposal holds an
             edit that replaces WORD with REPLACEMENT or a query that names one of them; the editor read brief.md
  card       the card was shown at the offer (t2c) and not stored then; stored after the yes (t3) and shown back;
             `voice-card` after the yes returns the card the editor returned in t1 (whitespace aside)
  promote    a real promote with no dry run exits 1 naming a dry run; the skill dry-runs before it asks (t4) and
             promotes for real only after the confirm (t4c)
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

Obj = dict[str, Any]
HERE = Path(__file__).resolve().parent
PASS, FAIL, NOT_MEASURABLE = "PASS", "FAIL", "NOT-MEASURABLE"
WORD, REPLACEMENT = "worker", "consumer"
ENTRY = ("Acceptance entry: this project calls the role that runs a job a consumer, never a worker. "
         "Propose replacing the word worker with consumer, except inside quoted text or code.")
DOC = "docs/zz-teach-scenario.md"


def _verdicts() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prose_edit_verdicts_for_teach", HERE / "verdicts.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


V = _verdicts()


def _norm(text: object) -> str:
    return " ".join(str(text).split())


def _word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE) is not None


def _commands(events: list[Obj]) -> list[tuple[str, str, bool]]:
    """(command, what it printed, errored) for every Bash call, in order."""
    results, errors = V.tool_results(events), V.tool_errors(events)
    return [(str(inp.get("command", "")), results.get(tid, ""), tid in errors or V.is_denied(results.get(tid, "")))
            for tid, name, inp, _ in V.tool_uses(events) if name == "Bash"]


def _last_filter(events: list[Obj]) -> Obj | None:
    runs = V.filter_runs(events)
    return runs[-1][1] if runs else None


def _json(text: str) -> Obj | None:
    return V._json_object(text)


def check_s6(events: list[Obj]) -> tuple[str, str]:
    if V.editor_reply(events) is None:
        return FAIL, "no editor reply"
    unread = V.brief_read_problem(events)
    if unread:
        return FAIL, unread
    results = V.tool_results(events)
    briefs = [results.get(tid, "") for tid, name, inp, _ in V.tool_uses(events, sub=True)
              if name == "Read" and str(inp.get("file_path", "")).endswith("/brief.md")]
    if not any(f"{ENTRY} (layer: user)" in text for text in briefs):
        return FAIL, "the user-level entry, labelled (layer: user), is not in the brief the editor read"
    proposal = _last_filter(events)
    if proposal is None:
        return NOT_MEASURABLE, "no `brief.py filter` output in the transcript: the post-filter proposal cannot be read"
    edits = [e for e in proposal.get("edits") or [] if isinstance(e, dict)]
    queries = [q for q in proposal.get("queries") or [] if isinstance(q, dict)]
    applied = [e for e in edits if _word(str(e.get("old", "")), WORD) and _word(str(e.get("new", "")), REPLACEMENT)]
    asked = [q for q in queries if any(_word(str(q.get(k, "")), w) for k in ("anchor", "text")
                                       for w in (WORD, REPLACEMENT))]
    if applied or asked:
        return PASS, f"the entry reached the brief; {len(applied)} edits apply it, {len(asked)} queries name it"
    return FAIL, (f"the entry reached the brief but nothing in the proposal ties to it: no edit replaces "
                  f"{WORD!r} with {REPLACEMENT!r} and no query names either ({len(edits)} edits, {len(queries)} queries)")


def _stores_card(command: str) -> bool:
    return "memory.py voice-card" in command and "--from-stdin" in command


def check_card(t1: list[Obj], t2c: list[Obj], t3: list[Obj], after: Obj | None) -> tuple[str, str]:
    proposal = _last_filter(t1)
    shown = _norm((proposal or {}).get("voice_card") or "")
    if not shown:
        return NOT_MEASURABLE, "the first run's filter output holds no voice_card: there is no card to keep"
    if after is None:
        return NOT_MEASURABLE, "voice-card-after.json is missing: nothing read the stored card back"
    head = shown[:30]
    if any(_stores_card(c) for c, _, _ in _commands(t2c)):
        return FAIL, "the card was stored before the author said yes (a `voice-card --from-stdin` in the offer turn)"
    if head not in _norm(V.final_text(t2c)):
        return FAIL, "the offer did not show the card before asking (its opening words are not in the reply)"
    saves = [(c, out, bad) for c, out, bad in _commands(t3) if _stores_card(c)]
    if not saves:
        return FAIL, "the card was not saved after the author's yes (no `voice-card --from-stdin` in the yes turn)"
    if any(bad for _, _, bad in saves) or not any(((_json(out) or {}).get("voice_card") or {}).get("text") for _, out, _ in saves):
        return FAIL, "the save after the yes was refused or printed no stored card"
    if head not in _norm(V.final_text(t3)):
        return FAIL, "the stored card was not shown back after saving (its opening words are not in the reply)"
    stored = _norm(((after.get("voice_card") or {}) if isinstance(after, dict) else {}).get("text") or "")
    if not stored:
        return FAIL, "`memory.py voice-card` returns no card after the yes"
    if stored != shown:
        return FAIL, "`memory.py voice-card` returns another text than the card the editor returned"
    return PASS, "the card was shown, kept only after the yes, shown back, and read back whole"


def check_promote(nodry_rc: str | None, nodry_err: str, t4: list[Obj], t4c: list[Obj]) -> tuple[str, str]:
    if nodry_rc is None:
        return NOT_MEASURABLE, "promote-nodry.rc is missing: the script's direct promote did not run"
    code = re.search(r"rc=(\d+)", nodry_rc)
    if code is None or code.group(1) != "1" or "dry run" not in nodry_err:
        return FAIL, f"a promote with no dry run was not refused naming a dry run ({nodry_rc.strip()})"
    first = _commands(t4)
    promotes = [(c, out, bad) for c, out, bad in first if "memory.py promote" in c]
    if any("--dry-run" not in c for c, _, _ in promotes):
        return FAIL, "the skill ran a real promote before the author confirmed it"
    if not any((_json(out) or {}).get("dry_run") is True and not bad for _, out, bad in promotes):
        return FAIL, "no dry run of the promote was run before the skill asked the author to confirm"
    real = [(out, bad) for c, out, bad in _commands(t4c) if "memory.py promote" in c and "--dry-run" not in c]
    if not real:
        return FAIL, "no real promote after the author's confirm"
    ok = [out for out, bad in real if not bad and (_json(out) or {}).get("title") and "dry_run" not in (_json(out) or {})]
    if not ok:
        return FAIL, "the real promote after the confirm failed or printed no stored entry"
    return PASS, "refused with no dry run; dry-run before the ask; real promote only after the confirm"


def _read_events(out: Path, name: str) -> list[Obj] | None:
    path = out / f"{name}.jsonl"
    return V.load_events(path) if path.is_file() else None


def main(argv: list[str]) -> int:
    if not argv:
        sys.stdout.write((__doc__ or "") + "\n")
        return 2
    if argv[0] == "seed-json":
        sys.stdout.write(json.dumps({"scalars": {}, "lists": {"diagnostics": [ENTRY]}}) + "\n")
        return 0
    out = Path(argv[0])
    runs = {name: _read_events(out, name) for name in ("s6-1", "t1", "t2", "t2c", "t3", "t4", "t4c")}
    rows: list[tuple[str, str, str]] = []

    def need(*names: str) -> str | None:
        missing = [n for n in names if runs.get(n) is None]
        return f"missing run(s): {', '.join(missing)}" if missing else None

    def row(name: str, missing: str | None, result: Callable[[], tuple[str, str]]) -> tuple[str, str, str]:
        verdict, note = (NOT_MEASURABLE, missing) if missing else result()
        return name, verdict, note

    after_path = out / "voice-card-after.json"
    try:
        after: Obj | None = json.loads(after_path.read_text(encoding="utf-8")) if after_path.is_file() else None
    except ValueError:
        after = None
    rc = (out / "promote-nodry.rc").read_text(encoding="utf-8") if (out / "promote-nodry.rc").is_file() else None
    err = (out / "promote-nodry.err").read_text(encoding="utf-8") if (out / "promote-nodry.err").is_file() else ""
    rows.append(row("s6", need("s6-1"), lambda: check_s6(runs["s6-1"] or [])))
    rows.append(row("card", need("t1", "t2c", "t3"),
                    lambda: check_card(runs["t1"] or [], runs["t2c"] or [], runs["t3"] or [], after)))
    rows.append(row("promote", need("t4", "t4c"),
                    lambda: check_promote(rc, err, runs["t4"] or [], runs["t4c"] or [])))
    for name, verdict, note in rows:
        sys.stdout.write(f"{name:8} {verdict:14} {note}\n")
    if any(v == FAIL for _, v, _ in rows):
        return 1
    return 3 if any(v == NOT_MEASURABLE for _, v, _ in rows) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
