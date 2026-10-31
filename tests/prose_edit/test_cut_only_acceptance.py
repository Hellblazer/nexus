# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cut-only acceptance kinds (nexus-ger02.10): run-cut.sh and the verdicts for cutonly, cutonly-1d, cutonly-repeat
and cutonly-web, over planted transcripts. No model runs in this file."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.prose_edit.test_acceptance_tools import (
    ACC,
    BRIEF_ID,
    WORK_DIR,
    _brief_read,
    _use,
    _verdicts,
)
from tests.prose_edit.test_acceptance_tools import _res as res

SCRIPTS = "/Users/x/nexus/prose-edit/skills/prose-edit/scripts"
BUILD = (f"python3 {SCRIPTS}/brief.py build docs/zz-cut/scenario.md --budget 20 --genre how-to --mode cut-only "
         "--voice-card /o/voice-card.txt --word-budget 40 --work")
FILTER = (f"python3 {SCRIPTS}/brief.py filter docs/zz-cut/scenario.md --budget 20 --mode cut-only --word-budget 40 "
          f"--save {WORK_DIR}/filtered.json")
FIXTURE = (Path(__file__).parent / "fixtures" / "review-scenario.md").read_text(encoding="utf-8")
Q1 = "The queue is basically ordered, and entries leave in the order they arrived."
Q2 = "The worker really quite simply retries a failed job once before it gives up."
C1 = {"n": 1, "old": Q1, "new": "The queue is ordered, and entries leave in the order they arrived.", "reason": "filler"}
C2 = {"n": 2, "old": Q2, "new": "The worker retries a failed job once before it gives up.", "reason": "filler"}
WORDS = {"document": 56, "budget": 40, "removed": 4, "after": 52}


def _cut_run(edits: list[dict], *, reply_edits: list[dict] | None = None, build: str | None = BUILD,
             filter_cmd: str | None = FILTER, final: str = f"WORK={WORK_DIR}\n1 cut proposed.", mode: str | None = "cut-only",
             queries: list[dict] | None = None, paragraphs: list[dict] | None = None,
             extra: list[dict] | None = None, dispatch: bool = True, dropped: list[dict] | None = None) -> list[dict]:
    """A cut run: build, the editor's read and reply, the filter, the final message."""
    out: list[dict] = []
    if build:
        out += [_use("bd", "Bash", {"command": build}), res("bd", f"WORK={WORK_DIR}\nDISPATCH=Read it.\n\n# Editing brief")]
    if dispatch:
        reply = {"brief_sha": BRIEF_ID, "voice_card": "vc", "edits": reply_edits if reply_edits is not None else edits,
                 "queries": [], "paragraphs": []}
        out += [_use("ag", "Agent", {}), *_brief_read(), res("ag", f"[hand-back] x\n```json\n{json.dumps(reply)}\n```")]
    if filter_cmd:
        printed = {"edits": edits, "dropped": dropped or [], "queries": queries or [], "paragraphs": paragraphs or [],
                   "dropped_queries": [], "dropped_paragraphs": [], "words": WORDS, "word_budget": 40,
                   **({"mode": mode} if mode else {})}
        out += [_use("fl", "Bash", {"command": filter_cmd}), res("fl", json.dumps(printed, indent=1))]
    out += extra or []
    out.append({"type": "result", "result": final})
    return out


def _score(kind: str, events: list[dict], doc: str | None = FIXTURE, prior: list[dict] | None = None) -> tuple[str, str]:
    v = _verdicts()
    return v.verdict(kind, events, v.compliance(events, doc), [], doc_text=doc, prior=prior)


# ---------------------------------------------------------------------------
# The oracles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("old", "new", "cut"), [
    ("The queue is basically ordered.", "The queue is ordered.", True),
    ("A sentence.", "", True),
    ("Tom &amp; Jerry really run.", "Tom &amp; Jerry run.", True),
    ("The queue is ordered.", "The queue is always ordered.", False),
    ("The queue drains.", "The queue empties.", False),
    ("one two three", "three two", False),
    ("The queue drains.", "the queue drains", False),
])
def test_the_verdicts_own_subsequence_rule(old: str, new: str, cut: bool) -> None:
    assert _verdicts().only_removes_words(old, new) is cut


def test_a_cut_is_identified_by_the_words_it_removes_however_much_text_is_quoted() -> None:
    v = _verdicts()
    short = v.removed_words("The worker really quite simply retries it.", "The worker retries it.")
    long = v.removed_words(Q2, "The worker retries a failed job once before it gives up.")
    assert short == long == ("really", "quite", "simply")
    assert v.removed_words("a b", "a b") == ()


PAGE = ("<html><head><title>Title words really</title></head><body><nav>Nav words really</nav>"
        "<h2 id=\"x\">Heading words really</h2><p>Plain words really here.</p><pre>code words really</pre>"
        "<table><tr><td><table><tr><td>inner</td></tr></table> after inner really</td></tr></table>"
        "<!-- comment words really --><p>Later plain words really.</p><button>Button words really</button></body></html>")


def test_the_element_oracle_finds_every_excluded_element_and_comment_from_the_pages_own_text() -> None:
    v = _verdicts()
    spans = v.excluded_spans(PAGE)
    inside = ["Title words really", "Nav words really", "Heading words really", "code words really",
              "after inner really", "comment words really", "Button words really"]
    outside = ["Plain words really here.", "Later plain words really."]
    for needle in inside:
        i = PAGE.index(needle)
        assert any(a <= i and i + len(needle) <= b for a, b in spans), needle
    for needle in outside:
        i = PAGE.index(needle)
        assert not any(a < i + len(needle) and i < b for a, b in spans), needle


# ---------------------------------------------------------------------------
# cutonly and cutonly-web
# ---------------------------------------------------------------------------


def test_a_cut_run_that_proposes_removals_only_passes() -> None:
    verdict, note = _score("cutonly", _cut_run([C1, C2]))
    assert verdict == "PASS", note
    assert "2 cuts" in note and '"after": 52' in note


@pytest.mark.parametrize(("what", "events", "fragment"), [
    ("an added word", _cut_run([{**C1, "new": "The queue is always ordered, and entries leave in the order they arrived."}]),
     "adds, replaces or reorders"),
    ("a replaced word", _cut_run([{**C1, "new": "The queue is sorted, and entries leave in the order they arrived."}]),
     "adds, replaces or reorders"),
    ("a surviving query", _cut_run([C1], queries=[{"n": 1, "anchor": "The queue", "text": "Why?"}]), "queries survived"),
    ("a merge proposal", _cut_run([C1], paragraphs=[{"n": 1, "action": "merge", "paragraphs": "x", "advice": "y"}]),
     "not a cut"),
    ("a MarkupEditor call", _cut_run([C1], extra=[_use("m", "mcp__markupeditor__open_document", {"path": "x"})]), "no app"),
    ("a question", _cut_run([C1], extra=[_use("q", "AskUserQuestion", {})]), "asks no question"),
    ("a copy of the page", _cut_run([C1], extra=[_use("w", "Write", {"file_path": "/r/docs/zz-cut/copy.md"})]),
     "not inside a prose-edit work directory"),
    ("a build without the mode", _cut_run([C1], build=BUILD.replace("--mode cut-only ", "")), "no `brief.py build`"),
    ("a filter without the mode", _cut_run([C1], filter_cmd=FILTER.replace("--mode cut-only ", "")), "no `brief.py filter`"),
    ("a filter output without the mode", _cut_run([C1], mode=None), "mode cut-only"),
    ("no WORK printed", _cut_run([C1], final="Done."), "WORK directory"),
    ("too many edits", _cut_run([C1, C2], filter_cmd=FILTER.replace("--budget 20", "--budget 1")), "against a budget of 1"),
    ("an unread brief", _cut_run([C1], dispatch=False, extra=[_use("ag", "Agent", {}), res("ag", "```json\n{}\n```")]),
     "never Read"),
])
def test_a_cut_run_fails_on_each_way_it_can_break_the_rules(what: str, events: list[dict], fragment: str) -> None:
    verdict, note = _score("cutonly", events)
    assert verdict == "FAIL" and fragment in note, (what, note)


def test_a_cut_run_with_no_surviving_edit_or_no_document_proves_nothing() -> None:
    assert _score("cutonly", _cut_run([]))[0] == "NOT-MEASURABLE"
    assert _score("cutonly", _cut_run([C1], filter_cmd=None))[0] == "FAIL"  # the skill never filtered the reply
    assert _score("cutonly", _cut_run([C1]), doc=None)[0] == "NOT-MEASURABLE"
    stale = _score("cutonly", _cut_run([{**C1, "old": "words the document never had", "new": ""}]))
    assert stale[0] == "NOT-MEASURABLE" and "not in the document" in stale[1]
    assert _score("cutonly", _cut_run([C1], dispatch=False))[0] == "FAIL"  # the editor was never dispatched


PHRASES = ["Title words really", "Nav words really", "Heading words really", "code words really", "after inner really",
           "comment words really", "Button words really"]
PLAIN = {"n": 1, "old": "Plain words really here.", "new": "Plain words here.", "reason": "r"}


def _planted(phrase: str) -> dict:
    return {"n": 2, "old": phrase, "new": phrase.replace(" really", ""), "reason": "r"}


@pytest.mark.parametrize("phrase", PHRASES)
def test_a_web_run_fails_on_a_cut_inside_an_excluded_element(phrase: str) -> None:
    verdict, note = _score("cutonly-web", _cut_run([PLAIN, _planted(phrase)]), doc=PAGE)
    assert verdict == "FAIL" and "excluded element" in note, phrase


@pytest.mark.parametrize("phrase", PHRASES)
def test_a_web_run_passes_when_the_editor_tried_an_excluded_element_and_the_filter_dropped_it(phrase: str) -> None:
    planted = _planted(phrase)
    drop = {"n": 2, "old": planted["old"], "new": planted["new"], "cause": "protected-region"}
    verdict, note = _score("cutonly-web", _cut_run([PLAIN], reply_edits=[PLAIN, planted], dropped=[drop]), doc=PAGE)
    assert verdict == "PASS" and "protected-region drops: 1" in note, (phrase, note)


def test_a_web_run_whose_editor_never_tried_an_excluded_element_is_vacuous_with_the_drop_count() -> None:
    verdict, note = _score("cutonly-web", _cut_run([PLAIN]), doc=PAGE)
    assert verdict == "VACUOUS" and "protected-region drops: 0" in note and "not exercised" in note, note
    # a run that fails for another reason is still a FAIL, never softened to VACUOUS
    assert _score("cutonly-web", _cut_run([{**PLAIN, "new": "Plain words are here."}]), doc=PAGE)[0] == "FAIL"
    # cutonly-1 (a markdown copy) has no excluded-element claim to exercise
    assert _score("cutonly", _cut_run([C1, C2]))[0] == "PASS"


def test_main_exits_3_for_a_vacuous_web_run(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "web").mkdir(parents=True)
    (root / "web" / "getting-started.html").write_text(PAGE, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    _write_run(out, "cutonly-web", _cut_run([PLAIN]))
    proc = subprocess.run([sys.executable, str(ACC / "verdicts.py"), str(out), "--root", str(root)],
                          capture_output=True, text=True, timeout=60)
    assert "VACUOUS" in proc.stdout and proc.returncode == 3, proc.stdout


# ---------------------------------------------------------------------------
# cutonly-1d and cutonly-repeat
# ---------------------------------------------------------------------------

APPLY = f"python3 {SCRIPTS}/review.py apply --work {WORK_DIR} --accept 2 --reject 1"
DRY = {"dry_run": True, "accept": [{"n": 2, "old": Q2, "new": C2["new"]}], "reject": [{"n": 1, "old": Q1, "new": C1["new"]}],
       "hold": [], "unplaced": [], "would_skip": [], "stores_rejections": True}
DONE = {"applied": [{"n": 2, "old": Q2, "new": C2["new"]}], "rejected": [1], "held": [], "rejections_stored": True}


def _decide(dry: dict | None = DRY, done: dict | None = DONE, *, real_cmd: str = APPLY, extra: list[dict] | None = None,
            order: tuple[str, str] = ("dry", "real")) -> list[dict]:
    calls = {"dry": ("d", f"{APPLY} --dry-run", dry), "real": ("r", real_cmd, done)}
    out: list[dict] = []
    for which in order:
        tid, cmd, printed = calls[which]
        if printed is not None:
            out += [_use(tid, "Bash", {"command": cmd}), res(tid, json.dumps(printed))]
    return [*out, *(extra or [])]


def test_the_decide_turn_passes_on_a_dry_run_then_the_same_apply_with_the_rejection_stored() -> None:
    verdict, note = _score("cutonly-1d", _decide())
    assert verdict == "PASS" and "rejected [1] stored" in note, note


@pytest.mark.parametrize(("what", "events", "fragment"), [
    ("no dry run", _decide(dry=None), "0 dry runs"),
    ("no real apply", _decide(done=None), "0 real applies"),
    ("real before dry", _decide(order=("real", "dry")), "before the dry run"),
    ("other arguments", _decide(real_cmd=APPLY.replace("--accept 2", "--accept 1")), "not the dry run's"),
    ("rejection not stored", _decide(done={**DONE, "rejections_stored": False}), "not stored"),
    ("a question", _decide(extra=[_use("q", "AskUserQuestion", {})]), "asks no question"),
])
def test_the_decide_turn_fails_on_each_way_it_can_break_the_rules(what: str, events: list[dict], fragment: str) -> None:
    verdict, note = _score("cutonly-1d", events)
    assert verdict == "FAIL" and fragment in note, (what, note)


def test_the_rejected_cut_dropped_as_rejected_passes_and_a_cut_that_comes_back_fails() -> None:
    prior = _decide()  # cutonly-1d rejected the cut that removes "basically"
    again = {"n": 3, "old": "The queue is basically ordered,", "new": "The queue is ordered,", "reason": "filler"}
    dropped = _cut_run([C2], reply_edits=[C2, again],
                       dropped=[{"n": 3, "old": again["old"], "new": again["new"], "cause": "rejected"}])
    verdict, note = _score("cutonly-repeat", dropped, prior=prior)
    assert verdict == "PASS" and "dropped it as rejected" in note, note
    back = _cut_run([C2, again], reply_edits=[C2, again])
    verdict, note = _score("cutonly-repeat", back, prior=prior)
    assert verdict == "FAIL" and "came back" in note, note


def test_a_second_run_that_never_proposes_the_rejected_cut_again_is_vacuous_not_a_pass() -> None:
    verdict, note = _score("cutonly-repeat", _cut_run([C2]), prior=_decide(DRY))
    assert verdict == "VACUOUS" and "not exercised" in note
    assert _verdicts().VACUOUS == "VACUOUS"


def test_a_second_run_with_nothing_to_compare_is_not_measurable() -> None:
    assert _score("cutonly-repeat", _cut_run([C1]), prior=None)[0] == "NOT-MEASURABLE"
    assert _score("cutonly-repeat", _cut_run([C1]), prior=_decide(dry={**DRY, "reject": []}))[0] == "NOT-MEASURABLE"
    left_other = _cut_run([C2], reply_edits=[C2, {**C1, "n": 5}],
                          dropped=[{"n": 5, "old": Q1, "new": C1["new"], "cause": "not-found"}])
    assert _score("cutonly-repeat", left_other, prior=_decide())[0] == "NOT-MEASURABLE"


# ---------------------------------------------------------------------------
# main, the decide arguments and run-cut.sh
# ---------------------------------------------------------------------------


def _write_run(out: Path, name: str, events: list[dict]) -> None:
    (out / f"{name}.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    (out / f"{name}.rc").write_text("rc=0\n", encoding="utf-8")


def test_main_scores_the_repeat_run_against_cutonly_1d_and_a_vacuous_run_exits_3(tmp_path: Path) -> None:
    _write_run(tmp_path, "cutonly-1d", _decide())
    _write_run(tmp_path, "cutonly-2", _cut_run([C2]))
    proc = subprocess.run([sys.executable, str(ACC / "verdicts.py"), str(tmp_path)], capture_output=True, text=True, timeout=60)
    rows = {ln.split()[0]: ln for ln in proc.stdout.splitlines() if ln.startswith("cutonly")}
    assert " PASS " in rows["cutonly-1d"] and "VACUOUS" in rows["cutonly-2"], proc.stdout
    assert proc.returncode == 3, proc.stdout
    (tmp_path / "cutonly-1d.jsonl").unlink()
    (tmp_path / "cutonly-1d.rc").unlink()
    again = subprocess.run([sys.executable, str(ACC / "verdicts.py"), str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert "NOT-MEASURABLE" in next(ln for ln in again.stdout.splitlines() if ln.startswith("cutonly-2"))


def test_main_exits_1_when_a_cut_run_fails(tmp_path: Path) -> None:
    _write_run(tmp_path, "cutonly-1", _cut_run([{**C1, "new": "The queue is sorted."}]))
    proc = subprocess.run([sys.executable, str(ACC / "verdicts.py"), str(tmp_path), "--root", str(ACC.parents[2])],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1 and "cutonly-1" in proc.stdout and "FAIL" in proc.stdout


def test_the_decide_arguments_come_from_the_cut_runs_own_transcript(tmp_path: Path) -> None:
    v = _verdicts()
    events = _cut_run([{**C2, "n": 4}, {**C1, "n": 2}, {**C1, "n": 7}])
    assert v.decide_args(events) == (WORK_DIR, 2, "4,7")
    assert v.decide_args(_cut_run([C1])) == (WORK_DIR, 1, "none")
    assert v.decide_args(_cut_run([])) is None
    assert v.decide_args(_cut_run([C1], build=None)) is None  # no WORK= line was printed
    _write_run(tmp_path, "cutonly-1", events)
    proc = subprocess.run([sys.executable, str(ACC / "verdicts.py"), "--decide-args", str(tmp_path / "cutonly-1.jsonl")],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and proc.stdout == f"{WORK_DIR}\t2\t4,7\n"
    bad = subprocess.run([sys.executable, str(ACC / "verdicts.py"), "--decide-args", str(tmp_path / "absent.jsonl")],
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 1 and "no WORK=" in bad.stderr


def test_run_cut_sh_runs_the_four_scenarios_through_run_scenario_with_nothing_of_its_own() -> None:
    text = (ACC / "run-cut.sh").read_text(encoding="utf-8")
    assert os.access(ACC / "run-cut.sh", os.X_OK)
    assert subprocess.run(["bash", "-n", str(ACC / "run-cut.sh")], capture_output=True).returncode == 0
    for name in ("cutonly-1", "cutonly-1d", "cutonly-2", "cutonly-web"):
        assert name in text, name
    assert "run-scenario.sh" in text and "runner_lock run-cut.sh" in text and "--decide-args" in text
    assert "RESUME_FROM=cutonly-1" in text and "--genre how-to" in text and "--budget 20" in text
    assert "--allowedTools" not in text and "--permission-mode" not in text
    assert "zzprose0210_" in text and "PROSE_EDIT_PROJECT_PREFIX" in text
    assert "web/getting-started.html" in text and (ACC.parents[2] / "web" / "getting-started.html").is_file()
    assert "PROSE_EDIT_APP_RUNNER" not in text  # no app, so no harness switch
    assert "importlib" not in text and "prose_word_count" not in text  # no import of brief.py's internals
    assert "brief.py count" in text
    prompts = [ln for ln in text.splitlines() if "prose-cut" in ln and ln.lstrip().startswith(("run ", "RESUME_FROM"))]
    assert len(prompts) == 4, prompts
    for line in prompts:  # every path variable in a prompt sits inside single quotes
        bare = re.findall(r"\$(?:CARD|DOC|PAGE|WORK)\b", line)
        quoted = re.findall(r"'\$(?:CARD|DOC|PAGE|WORK)'", line)
        assert bare and len(bare) == len(quoted), line
    assert 'runner_not_run "$OUT" cutonly-web' in text  # a failed count stops the web scenario, never an empty flag


def test_a_scenario_runner_not_run_skipped_is_scored_not_run_and_the_exit_is_3(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """run-cut.sh skips cutonly-web through runner_not_run when the page count fails: verdicts.py reads not-run.txt,
    prints the row with its reason and exits 3 (never 0 for a batch with a scenario that did not run)."""
    out = tmp_path / "out"
    guard = f'WT=/nonexistent; . "{ACC / "runner_guard.bash"}"; runner_not_run "{out}" cutonly-web "count failed"'
    proc = subprocess.run(["bash", "-c", guard], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "NOT-RUN cutonly-web: count failed" in proc.stderr, proc.stderr
    assert (out / "not-run.txt").read_text(encoding="utf-8") == "cutonly-web: count failed\n"
    v = _verdicts()
    assert v.not_run_rows(out) == [("cutonly-web", "count failed")]
    assert v.main([str(out)]) == 3
    assert "cutonly-web" in capsys.readouterr().out
    (out / "cutonly-web.rc").write_text("rc=0\n", encoding="utf-8")  # a name that did run is not a skipped one
    assert v.not_run_rows(out) == []


def test_run_cut_sh_refuses_an_unknown_scenario_group(tmp_path: Path) -> None:
    env = {**os.environ, "OUT_DIR": str(tmp_path / "out"), "SCENARIOS": "zzz", "TMPDIR": str(tmp_path)}
    proc = subprocess.run(["bash", str(ACC / "run-cut.sh")], capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 2 and "unknown scenario" in proc.stderr
    assert not (tmp_path / "out").exists()  # nothing was started


def test_the_acceptance_readme_names_the_cut_only_scenarios() -> None:
    text = (ACC / "README.md").read_text(encoding="utf-8")
    for needle in ("run-cut.sh", "cutonly-1", "cutonly-1d", "cutonly-2", "cutonly-web", "VACUOUS"):
        assert needle in text, needle


def _page_budget(cwd: Path, page: str) -> subprocess.CompletedProcess[str]:
    """run-cut.sh's own page_budget function, run on its own in `cwd` for the page PAGE."""
    text = (ACC / "run-cut.sh").read_text(encoding="utf-8")
    start = text.index("page_budget() {")
    body = text[start:text.index("\n}\n", start) + 3]
    return subprocess.run(["bash", "-c", 'PAGE="$1"\n' + body + "\npage_budget", "x", page],
                          cwd=cwd, capture_output=True, text=True, timeout=120)


def test_page_budget_is_nine_tenths_of_the_count_verbs_words() -> None:
    root = ACC.parents[2]
    page = "tests/prose_edit/fixtures/cut-only-page.html"
    proc = _page_budget(root, page)
    assert proc.returncode == 0 and proc.stdout.strip() == str(int(45 * 0.9)), (proc.stdout, proc.stderr)


def test_page_budget_fails_without_printing_a_number_when_the_count_verb_fails(tmp_path: Path) -> None:
    root = ACC.parents[2]
    gone = _page_budget(root, "web/absent-page.html")  # brief.py count refuses a page that is not there
    assert gone.returncode != 0 and gone.stdout.strip() == "", (gone.stdout, gone.stderr)
    no_script = _page_budget(tmp_path, "page.html")  # no brief.py under this directory at all
    assert no_script.returncode != 0 and no_script.stdout.strip() == ""
