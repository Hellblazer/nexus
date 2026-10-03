# SPDX-License-Identifier: AGPL-3.0-or-later
"""The acceptance runner and verdicts for scenario 6 (a user-level entry reaches another genre's edit) and for
step 13 of the skill (the voice card kept after the author's yes, the promote dry-run gate), without a live
session. Every transcript here is planted: no model runs in this file (nexus-ger02.6, critique E)."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from tests.prose_edit.test_acceptance_tools import (
    ACC,
    BRIEF_ID,
    WORK_DIR,
    _brief_read,
    _res,
    _res_err,
    _use,
)

BRIEF = "python3 .claude/skills/prose-edit/scripts/brief.py"
MEMORY = "python3 .claude/skills/prose-edit/scripts/memory.py"
DOC = "docs/zz-teach-scenario.md"
CARD = "First person, plain register. Refrain: the closing sentence of each paragraph."


def _teach() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prose_edit_teach_verdicts", ACC / "teach_verdicts.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


T = _teach()


def _final(text: str) -> dict:
    return {"type": "result", "result": text}


def _bash(tid: str, command: str, out: object, *, error: bool = False) -> list[dict]:
    text = out if isinstance(out, str) else json.dumps(out)
    return [_use(tid, "Bash", {"command": command}), (_res_err if error else _res)(tid, text)]


# ---------------------------------------------------------------------------
# Scenario 6: a user-level entry, a document of another genre
# ---------------------------------------------------------------------------


def _s6(*, entry_in_brief: bool = True, edits: list[dict] | None = None, queries: list[dict] | None = None,
        read: list[dict] | None = None, echo: str | None = BRIEF_ID) -> list[dict]:
    body = f"     1\t# Editing brief\n    40\t- {T.ENTRY} (layer: user)\n   311\tBrief id: {BRIEF_ID}\n"
    if not entry_in_brief:
        body = f"     1\t# Editing brief\n   311\tBrief id: {BRIEF_ID}\n"
    reply = {"edits": edits or [], "queries": queries or []}
    if echo is not None:
        reply["brief_sha"] = echo
    target = "tests/prose_edit/fixtures/user-entry.md"
    return [
        _use("ag", "Agent", {}), *(_brief_read(text=body) if read is None else read),
        _res("ag", "```json\n" + json.dumps(reply) + "\n```"),
        *_bash("fl", f"{BRIEF} filter {target} --budget 5 --save {WORK_DIR}/filtered.json",
               {"edits": edits or [], "queries": queries or [], "dropped": [], "paragraphs": []}),
    ]


WORKER = {"n": 1, "old": "A worker that dies mid-job releases the lease", "new": "A consumer that dies mid-job releases the lease",
          "reason": "the project's term"}


def test_scenario_6_passes_when_the_entry_reached_the_brief_and_an_edit_applies_it() -> None:
    verdict, note = T.check_s6(_s6(edits=[WORKER]))
    assert verdict == "PASS", note


def test_scenario_6_passes_on_a_query_that_ties_to_the_entry() -> None:
    ask = {"n": 1, "anchor": "The worker takes one job", "text": "The entry says consumer: rename it here?"}
    assert T.check_s6(_s6(queries=[ask]))[0] == "PASS"
    by_replacement = {"n": 1, "anchor": "holds a lease", "text": "Should this say consumer?"}
    assert T.check_s6(_s6(queries=[by_replacement]))[0] == "PASS"


def test_scenario_6_fails_when_the_entry_never_reached_the_brief_the_editor_read() -> None:
    verdict, note = T.check_s6(_s6(entry_in_brief=False, edits=[WORKER]))
    assert verdict == "FAIL" and "brief" in note and "user" in note  # the layer label is part of the proof


def test_scenario_6_fails_when_the_editor_proposed_nothing_tied_to_the_entry() -> None:
    other = {"n": 1, "old": "The scheduler waits ten seconds between attempts.", "new": "", "reason": "r"}
    for edits, queries in (([], []), ([other], []), ([], [{"n": 1, "anchor": "the lease", "text": "How long?"}])):
        verdict, note = T.check_s6(_s6(edits=edits, queries=queries))
        assert verdict == "FAIL" and "worker" in note, (edits, queries, note)
    # an edit that touches the word but is not the entry's replacement is not tied to it
    cut = {"n": 1, "old": "A worker that dies mid-job releases the lease", "new": "A lease is released", "reason": "r"}
    assert T.check_s6(_s6(edits=[cut]))[0] == "FAIL"


def test_scenario_6_needs_the_read_proof_like_every_editing_run() -> None:
    assert T.check_s6(_s6(edits=[WORKER], read=[]))[0] == "FAIL"
    assert T.check_s6(_s6(edits=[WORKER], echo="0" * 12))[0] == "FAIL"
    assert T.check_s6([])[0] == "FAIL"  # no transcript, no editor reply


# ---------------------------------------------------------------------------
# Step 13: the voice card
# ---------------------------------------------------------------------------


def _t1(card: str | None = CARD, edits: int = 3) -> list[dict]:
    shown = [{"n": i, "old": f"w{i}", "new": "", "reason": "r"} for i in range(1, edits + 1)]
    return _bash("f", f"{BRIEF} filter {DOC} --budget 5 --save {WORK_DIR}/filtered.json",
                 {"edits": shown, "queries": [], "dropped": [], "paragraphs": [], "voice_card": card})


REVIEW = "python3 .claude/skills/prose-edit/scripts/review.py"


def _t2c(*, shows: bool = True, stores: bool = False, rejects: bool = True) -> list[dict]:
    """The confirm turn: the skill's real apply (it stored the rejections unless `rejects` is False), then the offer."""
    out = {"mode": "edit", "path": DOC, "applied": [], "rejected": [2, 3] if rejects else [], "held": [],
           "rejections_stored": rejects}
    ev = _bash("a", f"{REVIEW} apply --work {WORK_DIR} --accept 1 --reject rest", out)
    ev += _bash("v", f"{MEMORY} voice-card {DOC}", {"path": DOC, "voice_card": None})
    if stores:
        ev += _bash("w", f"{MEMORY} voice-card {DOC} --from-stdin < /tmp/prose-edit-ab12cd34/card.json",
                    {"path": DOC, "voice_card": {"text": CARD, "at": "2026-10-03T12:00:00Z"}})
    return ev + [_final(f"Applied. Keep this voice card for the document?\n\n{CARD if shows else '(not shown)'}")]


def _t3(*, stores: bool = True, shows: bool = True, error: bool = False) -> list[dict]:
    ev: list[dict] = []
    if stores:
        ev += _bash("w", f"{MEMORY} voice-card {DOC} --from-stdin < /tmp/prose-edit-ab12cd34/card.json",
                    "Error: voice-card: refused" if error else
                    {"path": DOC, "voice_card": {"text": CARD, "at": "2026-10-03T12:00:00Z"}}, error=error)
    return ev + [_final(f"Stored. The card now reads: {CARD if shows else 'saved'}")]


AFTER = {"path": DOC, "range": None, "voice_card": {"text": CARD, "at": "2026-10-03T12:00:00Z"}}


def _t5(*, in_brief: str | None = CARD, reply_card: str | None = CARD, warnings: tuple[str, ...] = (),
        read: list[dict] | None = None) -> list[dict]:
    """A second edit run after the card was stored: the brief the editor Read holds the card between the
    voice_card tags (a Read result is line-numbered), the reply returns it, the filter says nothing about it."""
    card = "" if in_brief is None else "".join(
        f"    {20 + i}\t{ln}\n" for i, ln in enumerate(["<voice_card>", *in_brief.split("\n"), "</voice_card>"]))
    body = f"     1\t# Editing brief\n{card}   311\tBrief id: {BRIEF_ID}\n"
    reply = {"brief_sha": BRIEF_ID, "voice_card": reply_card, "edits": [], "queries": []}
    return [_use("ag", "Agent", {}), *(_brief_read(text=body) if read is None else read),
            _res("ag", "```json\n" + json.dumps(reply) + "\n```"),
            *_bash("fl", f"{BRIEF} filter {DOC} --budget 5 --save {WORK_DIR}/filtered.json",
                   {"edits": [], "queries": [], "dropped": [], "paragraphs": [], "warnings": list(warnings)})]


def test_the_card_check_passes_when_it_is_shown_stored_only_after_the_yes_and_read_back_whole() -> None:
    verdict, note = T.check_card(_t1(), _t2c(), _t3(), AFTER)
    assert verdict == "PASS", note
    # whitespace and line breaks in the retyped card do not matter
    spaced = {**AFTER, "voice_card": {"text": CARD.replace(" ", "  ") + "\n", "at": "x"}}
    assert T.check_card(_t1(), _t2c(), _t3(), spaced)[0] == "PASS"


def test_the_card_check_fails_on_each_way_step_13_can_go_wrong() -> None:
    cases = {
        "stored before the author said yes": T.check_card(_t1(), _t2c(stores=True), _t3(), AFTER),
        "the offer did not show the card": T.check_card(_t1(), _t2c(shows=False), _t3(), AFTER),
        "never saved after the yes": T.check_card(_t1(), _t2c(), _t3(stores=False), AFTER),
        "the save was refused": T.check_card(_t1(), _t2c(), _t3(error=True), AFTER),
        "the stored card was not shown back": T.check_card(_t1(), _t2c(), _t3(shows=False), AFTER),
        "memory.py voice-card returns nothing": T.check_card(_t1(), _t2c(), _t3(), {**AFTER, "voice_card": None}),
        "memory.py voice-card returns another text": T.check_card(
            _t1(), _t2c(), _t3(), {**AFTER, "voice_card": {"text": "A different card.", "at": "x"}}),
    }
    for why, (verdict, note) in cases.items():
        assert verdict == "FAIL", (why, note)


def test_the_card_check_is_not_measurable_without_an_editor_card_or_the_after_file() -> None:
    assert T.check_card(_t1(card=None), _t2c(), _t3(), AFTER)[0] == "NOT-MEASURABLE"
    assert T.check_card(_t1(), _t2c(), _t3(), None)[0] == "NOT-MEASURABLE"
    assert T.check_card([], _t2c(), _t3(), AFTER)[0] == "NOT-MEASURABLE"  # no filter output in the first run


# ---------------------------------------------------------------------------
# Step 13: promote needs a dry run first
# ---------------------------------------------------------------------------

REFUSED = "memory.py: promote: no matching dry run on record for this entry at this level. Run the dry run first."
DRY = {"level": "repo", "project": "p", "title": "not-a-defect", "entry": {"old": "just", "new": ""}, "dry_run": True}
REAL = {"level": "repo", "project": "p", "title": "not-a-defect", "entry": {"old": "just", "new": ""}}
CMD = f"{MEMORY} promote {DOC} 1 --level repo"


def _t4(*, dry: bool = True, early_real: bool = False) -> list[dict]:
    ev: list[dict] = []
    if dry:
        ev += _bash("d", f"{CMD} --dry-run", DRY)
    if early_real:
        ev += _bash("e", CMD, REAL)
    return ev + [_final(f"Here is the entry: {DRY['entry']['old']} -> (cut). Confirm?")]


def _t4c(*, real: bool = True, error: bool = False, dry_again: bool = False) -> list[dict]:
    ev: list[dict] = []
    if dry_again:
        ev += _bash("d2", f"{CMD} --dry-run", DRY)
    if real:
        ev += _bash("r", CMD, "Error: promote refused" if error else REAL, error=error)
    return ev + [_final("Stored.")]


def test_the_promote_check_passes_on_refusal_then_dry_run_then_a_real_promote_after_the_confirm() -> None:
    verdict, note = T.check_promote("rc=1\n", REFUSED, _t4(), _t4c())
    assert verdict == "PASS", note


def test_the_promote_check_fails_on_each_way_the_gate_can_go_wrong() -> None:
    cases = {
        "a promote with no dry run went through": T.check_promote("rc=0\n", "", _t4(), _t4c()),
        "refused for another reason": T.check_promote("rc=1\n", "memory.py: no stored rejection number 1", _t4(), _t4c()),
        "no dry run was run before the ask": T.check_promote("rc=1\n", REFUSED, _t4(dry=False), _t4c()),
        "the real promote ran before the confirm": T.check_promote("rc=1\n", REFUSED, _t4(early_real=True), _t4c(real=False)),
        "no real promote after the confirm": T.check_promote("rc=1\n", REFUSED, _t4(), _t4c(real=False)),
        "the real promote after the confirm failed": T.check_promote("rc=1\n", REFUSED, _t4(), _t4c(error=True)),
    }
    for why, (verdict, note) in cases.items():
        assert verdict == "FAIL", (why, note)
    # a fresh dry run after the confirm, then the real promote, is the skill doing it again: fine
    assert T.check_promote("rc=1\n", REFUSED, _t4(), _t4c(dry_again=True))[0] == "PASS"


def test_the_promote_check_is_not_measurable_without_the_runners_direct_call() -> None:
    assert T.check_promote(None, "", _t4(), _t4c())[0] == "NOT-MEASURABLE"
    assert T.check_promote("rc=1\n", REFUSED, [], _t4c())[0] in ("FAIL", "NOT-MEASURABLE")


# ---------------------------------------------------------------------------
# main(): files in, verdicts out
# ---------------------------------------------------------------------------


def _write(out: Path, name: str, events: list[dict]) -> None:
    (out / f"{name}.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def _all_runs(out: Path, *, refused: bool = True) -> None:
    _write(out, "s6-1", _s6(edits=[WORKER]))
    for name, events in (("t1", _t1()), ("t2", []), ("t2c", _t2c()), ("t3", _t3()), ("t4", _t4()), ("t4c", _t4c()),
                         ("t5", _t5())):
        _write(out, name, events)
    (out / "voice-card-after.json").write_text(json.dumps(AFTER), encoding="utf-8")
    (out / "promote-nodry.rc").write_text("rc=1\n" if refused else "rc=0\n", encoding="utf-8")
    (out / "promote-nodry.err").write_text(REFUSED if refused else "", encoding="utf-8")


def test_main_exits_zero_on_a_clean_batch_one_on_a_failure_and_three_on_a_missing_run(
    tmp_path: Path, capsys: object
) -> None:
    _all_runs(tmp_path)
    assert T.main([str(tmp_path)]) == 0
    (tmp_path / "promote-nodry.rc").write_text("rc=0\n", encoding="utf-8")
    assert T.main([str(tmp_path)]) == 1
    (tmp_path / "promote-nodry.rc").write_text("rc=1\n", encoding="utf-8")
    (tmp_path / "t4c.jsonl").unlink()
    assert T.main([str(tmp_path)]) == 3  # a run that never happened measured nothing: not a pass
    assert T.main([]) == 2


def test_the_seed_the_runner_writes_is_the_entry_the_verdict_looks_for() -> None:
    proc = subprocess.run([sys.executable, str(ACC / "teach_verdicts.py"), "seed-json"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"scalars": {}, "lists": {"diagnostics": [T.ENTRY]}}
    fixture = (ACC.parent / "fixtures" / "user-entry.md").read_text(encoding="utf-8")
    assert T.WORD in fixture and fixture.count(T.WORD) >= 2  # the document has the word to act on


def test_the_teach_runner_is_one_pass_with_a_test_prefix_a_direct_refused_promote_and_no_retry() -> None:
    sh = (ACC / "run-teach.sh").read_text(encoding="utf-8")
    assert os.access(ACC / "run-teach.sh", os.X_OK) and "SPDX-License-Identifier" in sh.splitlines()[1]
    assert 'PROSE_EDIT_PROJECT_PREFIX="${PROSE_EDIT_PROJECT_PREFIX:-zzprose0206_}"' in sh  # never the live projects
    assert "rerun" not in sh and "retry loop" in sh  # the comment says why there is none
    assert "teach_verdicts.py seed-json" in sh and "add-entry --level user --from-stdin" in sh
    for name in ("s6-1", "t1", "t2", "t2c", "t3", "t4", "t4c"):
        assert re.search(rf'\b{name}\b', sh), name
    assert "voice-card-after.json" in sh and "promote-nodry.rc" in sh and "promote-nodry.err" in sh
    direct = sh.index("promote-nodry.rc")  # the refused promote runs after t3 (the yes) and before t4's dry run
    assert sh.index('run t3') < direct < sh.index('run t4 ')
    promote_line = next(ln for ln in sh.splitlines() if "promote" in ln and "promote-nodry" in ln and "memory.py" in ln)
    assert "--dry-run" not in promote_line
    assert "trap" in sh and DOC in sh  # the scenario copy is removed on exit
    assert "delete the zzprose0206_" in sh.lower() or "zzprose0206_*" in sh


def test_the_readme_documents_the_teach_runner_and_what_it_cannot_prove() -> None:
    text = (ACC / "README.md").read_text(encoding="utf-8")
    assert "run-teach.sh" in text and "teach_verdicts.py" in text
    assert "scenario 6" in text.lower() and "step 13" in text.lower()
