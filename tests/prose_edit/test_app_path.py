# SPDX-License-Identifier: AGPL-3.0-or-later
"""The app path of the acceptance runners and their verdicts, rebased on the app-decided flow (nexus-scmcs.3).

Under PROSE_EDIT_APP_RUNNER=1 the author's decisions live only in the stand-in's list_suggestions statuses, so a path
run's answer turn says "reviewed" and nothing else; the verdicts pin that the decisions came from the app (a
list_suggestions call, then `review.py decisions --from-file`, then the dry run, then the real apply) and, for an
App-decided run, that the skill applied in the same turn (SKILL.md step 12). One control case (d) has the app accept
edit 2 and reject edit 1, the reverse of what the old chat answer named: applying edit 1 fails the run.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.prose_edit.test_acceptance_tools import (
    _b_runs,
    ACC,
    PROTECTED,
    _both,
    _ed,
    _verdicts,
    _app_turn,
    _check_a_runs,
    _gate,
    _gate_run,
    _review_verdicts,
    _script,
    _use,
    _res,
    _write_runs,
)
from tests.prose_edit.test_local_engine import Box, _run_runner, box  # noqa: F401  (the box fixture)
from tests.prose_edit import test_teach_acceptance as tt

STANDIN_HEADER = "app: STAND-IN"
EDITS2 = {"edits": [{"n": 1, "old": "A", "new": "a"}, {"n": 2, "old": "really quite ", "new": ""}], "dropped": []}


def _rows(rv, runs):  # type: ignore[no-untyped-def]
    return {n: (v, note) for n, v, note in rv.check_d(runs)}


def _d_runs(applied: list[int], rejected: list[int], *, flow: bool = True) -> dict[str, list[dict]]:
    real = {"applied": [{"n": n} for n in applied], "rejected": rejected, "held": [], "rejections_stored": True}
    return {
        "d1": _script("brief", "filter", "f1", EDITS2) + _script("review", "render", "r1", {"counts": {"edits": 2}}),
        "d2": (_app_turn("d2", {"accept": [{"n": 2}], "reject": [{"n": 1}]}, real) if flow else
               _script("review", "apply", "p", real, "--work /w --accept 2")),
    }


# ---------------------------------------------------------------------------
# The control case: accept 2, reject 1
# ---------------------------------------------------------------------------


def test_the_control_passes_when_edit_2_is_applied_and_edit_1_rejected() -> None:
    rows = _rows(_review_verdicts(), _d_runs([2], [1]))
    assert rows["d1"][0] == "PASS" and rows["d2"][0] == "PASS", rows
    assert "edit 2 applied, edit 1 rejected" in rows["d2"][1]


def test_the_control_fails_when_edit_1_is_applied_whatever_else_is_right() -> None:
    rows = _rows(_review_verdicts(), _d_runs([1], [2]))     # what the old chat answer ("accept edit 1") would have done
    assert rows["d2"][0] == "FAIL" and "edit 1 was applied" in rows["d2"][1]
    rows = _rows(_review_verdicts(), _d_runs([1, 2], []))
    assert rows["d2"][0] == "FAIL" and "edit 1 was applied" in rows["d2"][1]


@pytest.mark.parametrize(("applied", "rejected"), [([], [1, 2]), ([2], []), ([2, 3], [1])])
def test_the_control_fails_on_any_other_outcome(applied: list[int], rejected: list[int]) -> None:
    assert _rows(_review_verdicts(), _d_runs(applied, rejected))["d2"][0] == "FAIL"


def test_the_control_fails_when_the_turn_never_read_the_app() -> None:
    rows = _rows(_review_verdicts(), _d_runs([2], [1], flow=False))
    assert rows["d2"][0] == "FAIL" and "list_suggestions" in rows["d2"][1]


def test_the_control_needs_two_edits_to_be_measured_and_no_apply_at_all_fails() -> None:
    rv = _review_verdicts()
    runs = _d_runs([2], [1])
    runs["d1"] = _script("brief", "filter", "f1", EDITS2) + _script("review", "render", "r1", {"counts": {"edits": 1}})
    assert _rows(rv, runs)["d1"][0] == "FAIL"
    runs["d2"] = _app_turn("d2", {"accept": []})
    assert _rows(rv, runs)["d2"][0] == "FAIL"


def test_group_d_is_not_run_when_it_was_not_asked_for_and_fails_when_asked_and_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rv = _review_verdicts()
    _write_runs(tmp_path, _check_a_runs([2], {"edits": [], "dropped": []}))
    (tmp_path / "scenarios.txt").write_text("a b c\n", encoding="utf-8")   # a run from before the control existed
    rv.main([str(tmp_path)])
    rows = {ln.split()[0]: ln.split()[1] for ln in capsys.readouterr().out.splitlines() if ln.strip()}
    assert rows["d1"] == "NOT-RUN" and rows["d2"] == "NOT-RUN"
    (tmp_path / "scenarios.txt").write_text("a b c d\n", encoding="utf-8")
    rv.main([str(tmp_path)])
    rows = {ln.split()[0]: ln.split()[1] for ln in capsys.readouterr().out.splitlines() if ln.strip()}
    assert rows["d1"] == "FAIL" and rows["d2"] == "FAIL"


def test_the_control_rule_and_the_runner_name_the_reverse_of_the_old_answer() -> None:
    sh = (ACC / "run-review.sh").read_text(encoding="utf-8")
    assert 'pathanswer d2 d1 "Accept edit 2 and reject all the other edits."' in sh
    table = json.loads((ACC / "markup_standin_decisions.json").read_text(encoding="utf-8"))
    assert table["d2"] == {"accepted": [2], "default": "rejected"} and table["a2"] == {"accepted": [1], "default": "rejected"}


# ---------------------------------------------------------------------------
# a2 / a4 / b4: decided in the app, applied in the same turn when nothing is held
# ---------------------------------------------------------------------------


def _a(runs: dict[str, list[dict]]) -> dict[str, tuple[str, str]]:
    return {n: (v, note) for n, v, note in _review_verdicts().check_a(runs)}


ABSENT = {"edits": [{"n": 1, "old": "C", "new": "c"}], "dropped": []}


def test_an_app_decided_answer_turn_applies_in_the_same_turn_and_passes() -> None:
    rows = _a(_check_a_runs([2], ABSENT))
    assert rows["a2"][0] == "PASS" and "flow=ok" in rows["a2"][1] and rows["a4"][0] == "PASS"


def test_an_answer_turn_that_asked_apply_and_waited_for_a_confirmation_fails_a2() -> None:
    runs = _check_a_runs([2], ABSENT)
    runs["a2"] = _app_turn("a2", {"accept": [{"n": 1}]})            # dry run only
    runs["a2c"] = _script("review", "apply", "p2", {"applied": [{"n": 1}], "rejected": [2], "rejections_stored": True},
                          "--work /w --accept 1")
    verdict, note = _a(runs)["a2"]
    assert verdict == "FAIL" and "App-decided" in note and "same turn" in note


def test_a2_fails_when_the_decisions_did_not_come_from_the_app_or_came_in_the_wrong_order() -> None:
    real = {"applied": [{"n": 1}], "rejected": [2], "rejections_stored": True}
    runs = _check_a_runs([2], ABSENT)
    runs["a2"] = _app_turn("a2", {"accept": [{"n": 1}]}, real, list_first=False)     # read the chat: no list_suggestions
    verdict, note = _a(runs)["a2"]
    assert verdict == "FAIL" and "list_suggestions" in note
    ordered = _app_turn("a2", {"accept": [{"n": 1}]}, real)
    runs["a2"] = [*ordered[2:4], *ordered[0:2], *ordered[4:]]                     # decisions before list_suggestions
    assert _a(runs)["a2"][0] == "FAIL" and "out-of-order" in _a(runs)["a2"][1]
    runs["a2"] = _app_turn("a2", {"accept": [{"n": 1}]}, {**real, "applied": [{"n": 1}, {"n": 2}], "rejected": []})
    assert _a(runs)["a2"][0] == "FAIL"                                              # applied something the rule rejected


def test_a_held_answer_turn_ends_at_the_dry_run_and_the_apply_is_the_confirmation_turns() -> None:
    runs = _check_a_runs([2], ABSENT)
    assert _a(runs)["a4"][0] == "PASS"
    runs["a4"] = _app_turn("a4", {"accept": []}, {"applied": [], "rejected": [], "held": [1]})   # applied without asking
    assert _a(runs)["a4"][0] == "FAIL"
    runs = _check_a_runs([2], ABSENT)
    runs["a4"] = _script("review", "apply", "d4", {"dry_run": True}, "--work /w --accept none --dry-run")   # never read the app
    assert _a(runs)["a4"][0] == "FAIL"


def test_b4_reads_the_app_too() -> None:
    rv = _review_verdicts()
    removed = {"old": "really quite ", "new": ""}
    again = {"edits": [{"n": 1, "old": "The worker really quite simply retries", "new": "The worker simply retries"}], "dropped": []}
    runs = _b_runs(removed, again)
    assert {n: v for n, v, _ in rv.check_b(runs)}["b4"] == "PASS"
    runs["b4"] = _script("review", "apply", "d", {"dry_run": True, "accept": []}, "--work /w --dry-run")
    assert {n: v for n, v, _ in rv.check_b(runs)}["b4"] == "FAIL"


def _held_turn(tid: str) -> tuple[list[dict], list[dict]]:
    """A hold-everything answer turn after a first turn with a query: it reads the app and stops to ask question 1, and the
    confirmation turn dry-runs and applies (SKILL.md step 11 Branch A)."""
    held = {"applied": [], "rejected": [], "held": [1]}
    return (_app_turn(tid, {"accept": []})[:4],
            _script("review", "apply", f"{tid}y", {"dry_run": True}, "--work /w --accept none --dry-run")
            + _script("review", "apply", f"{tid}p", held, "--work /w --accept none"))


@pytest.mark.parametrize("counts", [{"edits": 1, "queries": 1}, {"edits": 1, "paragraphs": 1}])
def test_a4_after_a_query_turn_scores_the_deferred_flow(counts: dict) -> None:
    runs = _check_a_runs([2], ABSENT)
    runs["a3"] = runs["a3"] + _script("review", "render", "r3", {"counts": counts})
    runs["a4"], runs["a4c"] = _held_turn("a4")
    row = {n: (v, note) for n, v, note in _review_verdicts().check_a(runs)}["a4"]
    assert row[0] == "PASS" and "deferred" in row[1], row
    runs["a4"] = []                                                    # never read the app
    row = {n: (v, note) for n, v, note in _review_verdicts().check_a(runs)}["a4"]
    assert row[0] == "FAIL" and "list_suggestions" in row[1], row
    runs["a4"] = _held_turn("a4")[0] + _script("review", "apply", "e", {"applied": [], "rejected": [], "held": [1]}, "--work /w --accept none")
    assert {n: v for n, v, _ in _review_verdicts().check_a(runs)}["a4"] == "FAIL"   # applied before question 1


@pytest.mark.parametrize("counts", [{"edits": 1, "queries": 1}, {"edits": 1, "paragraphs": 1}])
def test_b4_after_a_query_turn_scores_the_deferred_flow(counts: dict) -> None:
    removed = {"old": "really quite ", "new": ""}
    again = {"edits": [{"n": 1, "old": "The worker really quite simply retries", "new": "The worker simply retries"}], "dropped": []}
    runs = _b_runs(removed, again)
    runs["b3"] = runs["b3"] + _script("review", "render", "r3", {"counts": counts})
    runs["b4"], runs["b4c"] = _held_turn("b4")
    rv = _review_verdicts()
    row = {n: (v, note) for n, v, note in rv.check_b(runs)}["b4"]
    assert row[0] == "PASS" and "deferred" in row[1], row
    runs["b4"] = []
    row = {n: (v, note) for n, v, note in rv.check_b(runs)}["b4"]
    assert row[0] == "FAIL" and "list_suggestions" in row[1], row


# ---------------------------------------------------------------------------
# A stand-in run says so
# ---------------------------------------------------------------------------


def _mark(out: Path, *names: str, app: str = "standin") -> None:
    for name in names:
        (out / f"{name}.app").write_text(app + "\n", encoding="utf-8")


def test_a_standin_run_prints_the_header_and_tags_each_pass_and_a_plain_run_does_neither(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rv = _review_verdicts()
    _write_runs(tmp_path, _check_a_runs([2], ABSENT))
    (tmp_path / "scenarios.txt").write_text("a\n", encoding="utf-8")
    rv.main([str(tmp_path)])
    plain = capsys.readouterr().out
    assert STANDIN_HEADER not in plain and "(stand-in)" not in plain
    _mark(tmp_path, "a1", "a2")
    rv.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith(STANDIN_HEADER)
    rows = [ln for ln in out.splitlines()[1:] if ln.startswith("a")]
    assert any(ln.startswith("a2  PASS (stand-in)") for ln in rows)
    assert not any("FAIL (stand-in)" in ln or "NOT-RUN (stand-in)" in ln for ln in out.splitlines())   # only a PASS is tagged
    _mark(tmp_path, "a1", "a2", app="none")
    rv.main([str(tmp_path)])
    assert "(stand-in)" not in capsys.readouterr().out


def test_the_teach_and_gate_verdicts_say_so_too(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "teach"
    out.mkdir()
    tt._all_runs(out)
    _mark(out, "t1")
    assert tt.T.main([str(out)]) == 3                  # an all-PASS stand-in batch is not a clean real pass
    text = capsys.readouterr().out
    assert text.splitlines()[0].startswith(STANDIN_HEADER) and "PASS (stand-in)" in text
    gate = _gate()
    g = tmp_path / "gate"
    g.mkdir()
    a = {"old": "really quite ", "new": ""}
    _write_runs(g, _gate_run("doc", 1, [a], [{"n": 1, "old": "A fresh edit", "new": "Fresh"}], []))
    assert gate.main([str(g)]) == 0
    assert STANDIN_HEADER not in capsys.readouterr().out
    _mark(g, "doc-1-1")
    assert gate.main([str(g)]) == 3
    text = capsys.readouterr().out
    assert text.startswith(STANDIN_HEADER) and "harness measurement, not a run of the preregistered gate" in text
    assert "PASS (stand-in)" in text


def _per_kind_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "tests" / "prose_edit" / "fixtures").mkdir(parents=True)
    (root / "tests" / "prose_edit" / "fixtures" / "protected.md").write_text(PROTECTED, encoding="utf-8")
    return root


def _per_kind_run(out: Path, name: str, events: list[dict]) -> None:
    out.mkdir(exist_ok=True)
    (out / f"{name}.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    (out / f"{name}.rc").write_text("rc=0\n", encoding="utf-8")


def test_the_per_kind_verdicts_print_the_header_tag_each_pass_and_exit_three_for_a_standin_batch(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    v = _verdicts()
    root = _per_kind_root(tmp_path)
    out = tmp_path / "per-kind"
    _per_kind_run(out, "protected-1", _both([_ed(1, "The scheduler basically hands each job", "x")]))
    assert v.main([str(out), "--root", str(root)]) == 0
    plain = capsys.readouterr().out
    assert STANDIN_HEADER not in plain and "(stand-in)" not in plain
    _mark(out, "protected-1")
    assert v.main([str(out), "--root", str(root)]) == 3
    text = capsys.readouterr().out
    assert text.splitlines()[0].startswith(STANDIN_HEADER)
    assert any(ln.startswith("protected-1") and "PASS (stand-in)" in ln for ln in text.splitlines())
    # a failure in a stand-in batch is still a failure: exit 1, and the FAIL row is not tagged
    _per_kind_run(out, "protected-2", _both([_ed(1, "This is basically a very simple loop", "x")]))
    _mark(out, "protected-2")
    assert v.main([str(out), "--root", str(root)]) == 1
    assert not any("FAIL (stand-in)" in ln for ln in capsys.readouterr().out.splitlines())


def test_the_canary_run_of_a_standin_batch_is_tagged_too(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """run-canaries.sh runs a path canary through run-scenario.sh, which gives it the stand-in under the switch."""
    v = _verdicts()
    out = tmp_path / "canaries"
    _per_kind_run(out, "protected-1", _both([_ed(1, "The scheduler basically hands each job", "x")]))
    (out / "protected-1.mcp.json").write_text("{}", encoding="utf-8")     # no .app marker: record-run.sh never ran
    assert v.main([str(out), "--root", str(_per_kind_root(tmp_path))]) == 3
    assert capsys.readouterr().out.startswith(STANDIN_HEADER)


# ---------------------------------------------------------------------------
# standin_run: an MCP config in the out dir is enough, a failed record-run.sh cannot hide the stand-in
# ---------------------------------------------------------------------------


def test_standin_run_reads_the_app_marker_and_any_mcp_config_the_runner_wrote(tmp_path: Path) -> None:
    v = _verdicts()
    assert v.standin_run(tmp_path) is False
    _mark(tmp_path, "a1", app="none")
    assert v.standin_run(tmp_path) is False
    (tmp_path / "a2.mcp.json").write_text("{}", encoding="utf-8")        # run-scenario.sh writes it before the session
    assert v.standin_run(tmp_path) is True                               # and record-run.sh may have died before NAME.app
    other = tmp_path / "other"
    other.mkdir()
    _mark(other, "a1")
    assert v.standin_run(other) is True


def test_the_standin_exit_code_is_three_only_for_a_run_that_would_exit_zero() -> None:
    v = _verdicts()
    assert [v.standin_exit(code, True) for code in (0, 1, 2, 3)] == [3, 1, 2, 3]
    assert [v.standin_exit(code, False) for code in (0, 1, 2, 3)] == [0, 1, 2, 3]


def test_the_review_verdicts_exit_three_for_an_all_pass_standin_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rv = _review_verdicts()
    monkeypatch.setattr(rv, "group_rows", lambda group, runs, asked, skipped: [(f"{group}1", "PASS", "ok")])
    assert rv.main([str(tmp_path)]) == 0
    _mark(tmp_path, "a1")
    assert rv.main([str(tmp_path)]) == 3
    monkeypatch.setattr(rv, "group_rows", lambda group, runs, asked, skipped: [(f"{group}1", "FAIL", "x")])
    assert rv.main([str(tmp_path)]) == 1


# ---------------------------------------------------------------------------
# teach: t2 applies in the same turn; a confirmation turn is a FAIL of `decided`
# ---------------------------------------------------------------------------


def test_teach_decided_passes_when_t2_reads_the_app_and_applies_and_the_card_is_scored_on_t2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tt._all_runs(tmp_path)
    assert tt.T.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "decided" in out and "card" in out and "FAIL" not in out
    assert not (tmp_path / "t2c.jsonl").exists()


def test_teach_decided_fails_when_t2_only_asked_and_a_confirmation_turn_applied(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tt._all_runs(tmp_path)
    flow_only = tt._t2()[:6]                                  # list_suggestions, decisions, the dry run: no apply
    tt._write(tmp_path, "t2", flow_only)
    tt._write(tmp_path, "t2c", tt._t2c())
    assert tt.T.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if ln.startswith("decided"))
    assert "FAIL" in row and "only asked" in row
    assert "card" in out and "PASS" in next(ln for ln in out.splitlines() if ln.startswith("card"))   # scored on t2c


def test_teach_decided_fails_when_t2_never_read_the_app(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tt._all_runs(tmp_path)
    tt._write(tmp_path, "t2", tt._t2c())                      # a real apply and the offer, but no list_suggestions or decisions
    assert tt.T.main([str(tmp_path)]) == 1
    assert "FAIL" in next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("decided"))


# ---------------------------------------------------------------------------
# memory gate: turn 2 reads the app; turn 3 exists only when turn 2 did not apply
# ---------------------------------------------------------------------------


def test_the_gate_counts_a_run_whose_turn_2_applied_in_place_and_errors_one_that_never_read_the_app(tmp_path: Path) -> None:
    gate = _gate()
    a = {"old": "really quite ", "new": ""}
    fresh = [{"n": 1, "old": "A fresh edit", "new": "Fresh"}]
    runs = _gate_run("doc", 1, [a], fresh, [])
    assert "doc-1-3" not in runs                                # no confirmation turn
    runs |= _gate_run("doc", 2, [a], fresh, [])
    runs["doc-2-2"] = _script("review", "apply", "p", {"applied": [], "rejected": [1], "held": [], "rejections_stored": True},
                              "--work /w --reject all")     # read the chat: no list_suggestions, no dry run
    _write_runs(tmp_path, runs)
    lines, pooled = gate.summarize(gate.collect(tmp_path))
    assert pooled["runs"] == 2 and pooled["errors"] == 1 and pooled["R"] == 1
    assert any(ln.startswith("doc-2: ERROR") and "did not take its decisions from the app" in ln for ln in lines)


def test_the_gate_still_takes_a_confirmation_turn_when_turn_2_only_asked(tmp_path: Path) -> None:
    gate = _gate()
    a = {"old": "really quite ", "new": ""}
    runs = _gate_run("doc", 1, [a], [{"n": 1, "old": "A fresh edit", "new": "Fresh"}], [])
    runs["doc-1-2"] = _app_turn("g1", {})                           # list_suggestions, decisions, dry run; nothing held back asked
    runs["doc-1-3"] = _script("review", "apply", "p", {"applied": [], "rejected": [1], "held": [], "rejections_stored": True},
                              "--work /w --reject all")
    _write_runs(tmp_path, runs)
    _, pooled = gate.summarize(gate.collect(tmp_path))
    assert pooled["measurable"] == 1 and pooled["R"] == 1 and pooled["errors"] == 0



def _gate_query_run(answer: list[dict], confirm: list[dict]) -> dict[str, list[dict]]:
    runs = _gate_run("doc", 1, [{"old": "really quite ", "new": ""}], [{"n": 1, "old": "A fresh edit", "new": "Fresh"}], [])
    runs["doc-1-1"] += _script("review", "render", "r1", {"counts": {"edits": 1, "queries": 1}})
    runs["doc-1-2"], runs["doc-1-3"] = answer, confirm
    return runs


REAL_G = {"applied": [], "rejected": [1], "held": [], "rejections_stored": True}


def test_with_a_query_the_gate_scores_a_run_that_reads_the_app_then_applies_in_the_confirmation(tmp_path: Path) -> None:
    gate = _gate()
    confirm = (_script("review", "apply", "cy", {"dry_run": True}, "--work /w --reject all --dry-run")
               + _script("review", "apply", "cp", REAL_G, "--work /w --reject all"))
    _write_runs(tmp_path, _gate_query_run(_read_turn("g1"), confirm))
    _, pooled = gate.summarize(gate.collect(tmp_path))
    assert pooled["measurable"] == 1 and pooled["R"] == 1 and pooled["errors"] == 0


def test_with_a_query_the_gate_errors_a_run_that_applied_before_question_1(tmp_path: Path) -> None:
    gate = _gate()
    early = _read_turn("g1") + _script("review", "apply", "e", REAL_G, "--work /w --reject all")
    confirm = (_script("review", "apply", "cy", {"dry_run": True}, "--work /w --reject all --dry-run")
               + _script("review", "apply", "cp", REAL_G, "--work /w --reject all"))   # a whole confirmation: only `early` can fail it
    _write_runs(tmp_path, _gate_query_run(early, confirm))
    _, pooled = gate.summarize(gate.collect(tmp_path))
    assert pooled["measurable"] == 0 and pooled["errors"] == 1

# ---------------------------------------------------------------------------
# The runners: the answer text by mode, and the confirmation turn only when the answer turn did not apply
# ---------------------------------------------------------------------------


def _prompts(box: Box) -> list[str]:
    path = box.ctrl / "prompts.txt"
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def _bash(body: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", f'. "{ACC / "runner_guard.bash"}"\n{body}'], capture_output=True, text=True,
                          env={**os.environ, **env}, timeout=30)


def test_runner_answer_is_reviewed_under_the_switch_and_the_chat_text_without_it() -> None:
    assert _bash('runner_answer "Accept edit 1."', PROSE_EDIT_APP_RUNNER="1").stdout == "reviewed\n"
    assert _bash('runner_answer "Accept edit 1."', PROSE_EDIT_APP_RUNNER="0").stdout == "Accept edit 1.\n"
    unset = _bash('unset PROSE_EDIT_APP_RUNNER; runner_answer "Hold every edit; accept none."')
    assert unset.stdout == "Hold every edit; accept none.\n"


def test_runner_turn_applied_reads_a_real_apply_and_not_a_dry_run_or_a_missing_transcript(tmp_path: Path) -> None:
    def write(name: str, command: str) -> None:
        (tmp_path / f"{name}.jsonl").write_text(json.dumps(_use("x", "Bash", {"command": command})) + "\n", encoding="utf-8")

    write("real", "python3 /r/prose-edit/skills/prose-edit/scripts/review.py apply --work /w --accept 1")
    write("dry", "python3 /r/prose-edit/skills/prose-edit/scripts/review.py apply --work /w --accept 1 --dry-run")
    write("other", "python3 /r/prose-edit/skills/prose-edit/scripts/memory.py apply")
    for name, rc in (("real", 0), ("dry", 1), ("other", 1), ("missing", 1)):
        assert _bash(f'runner_turn_applied "{tmp_path}" {name}').returncode == rc, name


def test_a_path_run_answer_turn_says_only_reviewed_under_the_switch_and_a_stdin_answer_keeps_its_text(box: Box) -> None:
    proc = _run_runner(box, "run-review.sh", SCENARIOS="a b c d", PROSE_EDIT_APP_RUNNER="1", PROSE_EDIT_LOCAL_ENGINE="0")
    assert proc.returncode == 0, proc.stderr
    prompts = _prompts(box)
    assert prompts.count("reviewed") == 4                      # a2, a4, b4, d2: nothing but the word
    assert "Accept edit 1." in prompts                          # c2, the stdin run, decides in the chat
    for chat in ("Accept edit 1 and reject all the other edits.", "Hold every edit; accept none.",
                 "Accept edit 2 and reject all the other edits."):
        assert chat not in prompts, chat
    # the stub never applies, so every answer turn that reviewed gets its confirmation
    after = [prompts[i + 1] for i, p in enumerate(prompts) if p == "reviewed"]
    assert after == ["Yes, apply."] * 4
    assert (box.out / "d2.mcp.json").is_file() and (box.out / "d2c.mcp.json").is_file()   # the confirmation resumes the app session


def test_the_chat_wording_runs_without_the_switch_for_the_one_group_that_runs(box: Box) -> None:
    proc = _run_runner(box, "run-review.sh", SCENARIOS="c", PROSE_EDIT_LOCAL_ENGINE="0")
    assert proc.returncode == 0, proc.stderr
    prompts = _prompts(box)
    assert "Accept edit 1." in prompts and "reviewed" not in prompts and prompts[-1] == "Yes, apply."


def test_an_answer_turn_that_already_applied_gets_no_confirmation_turn(box: Box) -> None:
    box.control("apply_on_reviewed")
    proc = _run_runner(box, "run-review.sh", SCENARIOS="a d", PROSE_EDIT_APP_RUNNER="1", PROSE_EDIT_LOCAL_ENGINE="0")
    assert proc.returncode == 0, proc.stderr
    prompts = _prompts(box)
    assert prompts.count("reviewed") == 3 and "Yes, apply." not in prompts       # a2, a4 and d2: the stub applies on each
    assert not list(box.out.glob("*c.jsonl"))


def test_the_control_copy_is_its_own_document_and_is_removed_on_exit(box: Box) -> None:
    proc = _run_runner(box, "run-review.sh", SCENARIOS="d", PROSE_EDIT_APP_RUNNER="1", PROSE_EDIT_LOCAL_ENGINE="0")
    assert proc.returncode == 0, proc.stderr
    first = _prompts(box)[0]
    assert "docs/zz-review-control/scenario.md" in first and "docs/zz-review/" not in first
    assert not (box.wt / "docs" / "zz-review-control").exists() and not (box.wt / "docs" / "zz-review").exists()


def test_the_gate_runs_turn_3_only_when_turn_2_did_not_apply(box: Box) -> None:
    args = ("g", "tests/prose_edit/fixtures/review-scenario.md", "1")
    proc = _run_runner(box, "run-memory-gate.sh", *args, PROSE_EDIT_APP_RUNNER="1", PROSE_EDIT_LOCAL_ENGINE="0")
    assert proc.returncode == 0, proc.stderr
    prompts = _prompts(box)
    assert prompts[1:3] == ["reviewed", "Yes, apply."] and len(prompts) == 4
    for f in box.out.glob("*.jsonl"):
        f.unlink()
    (box.ctrl / "prompts.txt").unlink()
    box.control("apply_on_reviewed")
    proc = _run_runner(box, "run-memory-gate.sh", *args, PROSE_EDIT_APP_RUNNER="1", PROSE_EDIT_LOCAL_ENGINE="0")
    assert proc.returncode == 0, proc.stderr
    prompts = _prompts(box)
    assert prompts[1] == "reviewed" and "Yes, apply." not in prompts and len(prompts) == 3


def test_the_teach_runner_resumes_t3_from_the_turn_that_applied(box: Box) -> None:
    proc = _run_runner(box, "run-teach.sh", PROSE_EDIT_APP_RUNNER="1", PROSE_EDIT_LOCAL_ENGINE="0")
    assert proc.returncode == 0, proc.stderr
    prompts = _prompts(box)
    assert prompts[prompts.index("reviewed") + 1] == "Yes, apply."          # t2 only asked: t2c follows, then t3
    assert (box.out / "t2c.jsonl").is_file()
    for f in box.out.glob("*.jsonl"):
        f.unlink()
    (box.ctrl / "prompts.txt").unlink()
    box.control("apply_on_reviewed")
    proc = _run_runner(box, "run-teach.sh", PROSE_EDIT_APP_RUNNER="1", PROSE_EDIT_LOCAL_ENGINE="0")
    assert proc.returncode == 0, proc.stderr
    assert not (box.out / "t2c.jsonl").exists() and (box.out / "t3.jsonl").is_file()


def test_the_control_fails_d1_when_edit_1_or_2_never_reached_the_app() -> None:
    rv = _review_verdicts()
    for n in (1, 2):
        runs = _d_runs([2], [1])
        runs["d1"] = _script("brief", "filter", "f1", EDITS2) + _script(
            "review", "render", "r1", {"counts": {"edits": 2}, "unplaced": [{"n": n, "cause": "not found"}]})
        row = _rows(rv, runs)["d1"]
        assert row[0] == "FAIL" and f"unplaced_of_1_2=[{n}]" in row[1], row


@pytest.mark.parametrize(("counts", "verdict"), [({"edits": 2, "queries": 1}, "NOT-MEASURABLE"),
                                                 ({"edits": 2, "paragraphs": 1}, "NOT-MEASURABLE"),
                                                 ({"edits": 2}, "FAIL")])
def test_teach_decided_is_not_measurable_when_t1_held_a_query_and_t2_read_the_app_then_t2c_applied(
        counts: dict, verdict: str) -> None:
    t1 = _script("review", "render", "r1", {"counts": counts})
    t2 = _read_turn("t2")
    t2c = _confirm_turn("c", REAL_D)
    got, note = tt.T.check_decided(t2, t2c, t1)    # t2 read the app and asked question 1; t2c dry-ran and applied
    assert got == verdict, note


@pytest.mark.parametrize("counts", [{"edits": 2, "queries": 1}, {"edits": 2, "paragraphs": 1}])
def test_teach_decided_fails_a_broken_flow_even_when_t1_held_a_query(counts: dict) -> None:
    t1 = _script("review", "render", "r1", {"counts": counts})
    got, note = tt.T.check_decided([], None, t1)              # t2 never read the app: a broken flow, not an unmeasured one
    assert got == "FAIL" and "list_suggestions" in note, note
    got, note = tt.T.check_decided(_read_turn("t2"), None, t1)  # read the app, and no confirmation turn ran: unmeasured
    assert got == "NOT-MEASURABLE", note
    got, note = tt.T.check_decided(_read_turn("t2"), [], t1)    # a confirmation turn ran and never applied
    assert got == "FAIL", note


@pytest.mark.parametrize("counts", [{"edits": 2, "queries": 1}, {"edits": 2, "paragraphs": 1}])
@pytest.mark.parametrize("with_t2c", [True, False], ids=["t2c", "no-t2c"])
def test_teach_decided_fails_an_apply_before_question_1(counts: dict, with_t2c: bool) -> None:
    t1 = _script("review", "render", "r1", {"counts": counts})
    same_turn = _app_turn("t2", {}, {"applied": [{"n": 1}], "rejected": [2], "held": [], "rejections_stored": True})
    got, note = tt.T.check_decided(same_turn, [] if with_t2c else None, t1)
    assert got == "FAIL" and "before question 1" in note, note


# ---------------------------------------------------------------------------
# A first turn with a query: question 1 comes first, so the apply moves to the confirmation turn (step 11 Branch A)
# ---------------------------------------------------------------------------

def _read_turn(tid: str) -> list[dict]:
    return _app_turn(tid, {})[:4]                            # list_suggestions and decisions --from-file, no apply


def _confirm_turn(tid: str, real: dict) -> list[dict]:
    return (_script("review", "apply", f"{tid}y", {"dry_run": True}, "--work /w --accept 2 --dry-run")
            + _script("review", "apply", f"{tid}p", real, "--work /w --accept 2"))


def _d_query_runs(answer: list[dict], confirm: list[dict]) -> dict[str, list[dict]]:
    return {"d1": _script("brief", "filter", "f1", EDITS2)
            + _script("review", "render", "r1", {"counts": {"edits": 2, "queries": 1}}),
            "d2": answer, "d2c": confirm}


REAL_D = {"applied": [{"n": 2}], "rejected": [1], "held": [], "rejections_stored": True}


def test_with_a_query_the_control_passes_when_the_answer_reads_the_app_and_the_confirmation_applies() -> None:
    rows = _rows(_review_verdicts(), _d_query_runs(_read_turn("d2"), _confirm_turn("c", REAL_D)))
    assert rows["d2"][0] == "PASS" and "deferred" in rows["d2"][1], rows


def test_with_a_query_an_answer_turn_that_never_read_the_app_fails() -> None:
    rows = _rows(_review_verdicts(), _d_query_runs([], _confirm_turn("c", REAL_D)))
    assert rows["d2"][0] == "FAIL" and "list_suggestions" in rows["d2"][1], rows


def test_with_a_query_an_apply_before_question_1_fails() -> None:
    early = _read_turn("d2") + _script("review", "apply", "e", REAL_D, "--work /w --accept 2")
    rows = _rows(_review_verdicts(), _d_query_runs(early, _confirm_turn("c", REAL_D)))
    assert rows["d2"][0] == "FAIL" and "before question 1" in rows["d2"][1], rows


def test_without_a_query_a_confirmation_turn_apply_still_fails() -> None:
    runs = _d_query_runs(_read_turn("d2"), _confirm_turn("c", REAL_D))
    runs["d1"] = _script("brief", "filter", "f1", EDITS2) + _script("review", "render", "r1", {"counts": {"edits": 2}})
    rows = _rows(_review_verdicts(), runs)
    assert rows["d2"][0] == "FAIL" and "same turn" in rows["d2"][1], rows
