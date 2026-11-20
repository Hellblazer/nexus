# SPDX-License-Identifier: AGPL-3.0-or-later
"""The compaction-outcome gate's judge, statistics and hook-evidence rules (tests/e2e/lib/compact_outcome_judge.py and
compact_outcome_stats.py), on recorded-shape events. Pure: no claude, no process, no repo. The harness's wiring (plugin, stub nx,
isolation, shell wrapper, turn script) is in test_compact_outcome_harness.py.

Pinned here: the scored window (an automatic boundary inside the final user turn, one compaction), only the items still open at the
boundary, credit by effect (state at the end, a read of the watcher's output), the three scenario kinds (work, bounded, waiting),
the hook evidence each arm needs, the Wilson interval, the one-sided Fisher exact test and the census calibration line.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "tests" / "e2e" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
import compact_outcome_fix_overlay as overlay  # noqa: E402
import compact_outcome_judge as jd  # noqa: E402
import compact_outcome_scenario as sc  # noqa: E402
import compact_outcome_stats as st  # noqa: E402

OPEN, BOUNDED, MEANWHILE, WAITING = (sc.SCENARIOS[n] for n in ("open", "bounded", "meanwhile", "waiting"))
NOTHING = {"todo2": False, "todo3": False, "alpha": False, "beta": False, "indep": False}


class Run:
    """A recorded run, built the way the stream shows it: harness markers, assistant tool_use, user tool_result, boundaries."""

    def __init__(self) -> None:
        self.ev: list[dict] = []
        self.n = 0

    def turn(self, name: str = "t") -> "Run":
        self.ev.append({"type": "harness", "subtype": "user_turn", "turn": name})
        return self

    def boundary(self, trigger: str = "auto") -> "Run":
        self.ev.append({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": trigger, "pre_tokens": 80000}})
        return self

    def call(self, name: str, out: str = "", error: bool = False, **tool_input) -> "Run":
        self.n += 1
        tid = f"tu{self.n}"
        self.ev.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": tool_input}]}})
        self.ev.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": error}]}})
        return self

    def say(self, text: str) -> "Run":
        self.ev.append({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
        return self

    def result(self, **kw) -> "Run":
        self.ev.append({"type": "result", "is_error": False, "stop_reason": "end_turn", **kw})
        return self

    def started_watcher(self, task: str = "bx1", path: str = "/tmp/t/tasks/bx1.output") -> "Run":
        return self.call("Bash", f"Command running in background with ID: {task}. Output is being written to: {path}", command="./watcher.sh",
                         run_in_background=True)


def judged(run: Run, scenario=OPEN, state: dict | None = None, **kw) -> dict:
    return jd.judge(run.ev, scenario=scenario, state_done={**NOTHING, **(state or {})}, **kw)


def standard(scenario=OPEN, pre=None, window=None, text: str = "") -> Run:
    """One compaction inside the final user turn, the realistic shape: turns ... then `final` with the boundary in it."""
    r = Run().turn("t1").started_watcher().result().turn("t2")
    for c in pre or []:
        r.call(*c[0], **c[1])
    r.result().turn("final").boundary()
    for c in window or []:
        r.call(*c[0], **c[1])
    if text:
        r.say(text)
    return r.result()


def C(name: str, **tin):
    return ((name,), tin)


class TestJudgeWindow:
    def test_the_observed_two_compaction_shape_scores_the_last_boundary_in_the_final_turn(self) -> None:
        """Pin of what every run of the first two matrices showed: R R C R C R. The scored boundary is the last one, after the
        third result, inside the fourth turn."""
        r = Run().call("Bash", command="start").result().call("Edit", file_path="app.py", new_string="def mul").result()
        r.boundary().result()                                   # the first compaction, in turn 3 (the paste turn)
        r.boundary().call("Edit", file_path="/r/app.py", new_string="def div(a, b)").result()    # the second, in turn 4
        v = judged(r, state={"todo2": True}, max_prior_compactions=1)
        assert (v["verdict"], v["acted"], v["compactions"], v["prior_compactions"]) == ("FAIL", ["todo2"], 2, 1)
        # but a summary of a summary is not the shape of a real compaction, so by default it is no verdict
        assert judged(r, state={"todo2": True})["verdict"] == "NOISE"

    def test_a_boundary_before_the_final_turn_is_noise_not_a_window(self) -> None:
        r = Run().turn("t1").boundary().result().turn("final").call("Edit", file_path="app.py", new_string="def div").result()
        v = judged(r, state={"todo2": True})
        assert v["verdict"] == "NOISE" and "before the final" in v["why"]
        # with no markers the same shape is read from the results: R C R R
        bare = Run().result().boundary().result().call("Edit", file_path="app.py", new_string="def div").result()
        assert judged(bare, state={"todo2": True})["verdict"] == "NOISE"

    def test_no_automatic_compaction_a_manual_one_or_one_turn_is_noise(self) -> None:
        assert judged(Run().turn().call("Bash", command="git log alpha").result())["verdict"] == "NOISE"
        assert judged(Run().turn().boundary("manual").call("Bash", command="x").result())["verdict"] == "NOISE"
        assert "fewer than two" in judged(Run().boundary().call("Bash", command="x").result())["why"]

    def test_a_refusal_or_no_result_is_noise(self) -> None:
        refused = Run().turn().boundary().ev + [{"type": "result", "stop_reason": "refusal", "is_error": True}]
        assert jd.judge(refused, scenario=OPEN, state_done=NOTHING)["verdict"] == "NOISE"
        assert judged(Run().turn().boundary().call("Bash", command="x"))["verdict"] == "NOISE"

    def test_the_no_compaction_baseline_has_no_boundary_and_its_window_is_the_final_turn(self) -> None:
        r = Run().turn("t1").started_watcher().result().turn("final")
        r.call("Edit", file_path="app.py", new_string="def div(a, b)").result()
        v = judged(r, state={"todo2": True}, expect_compaction=False)
        assert (v["verdict"], v["acted"]) == ("FAIL", ["todo2"])        # one of five items: what a session that never compacts does
        assert v["unavailable"] == [] and v["prior_compactions"] == 0
        compacted = Run().turn("t1").result().turn("final").boundary().call("Bash", command="x").result()
        assert "baseline" in judged(compacted, expect_compaction=False)["why"]


class TestOnlyOpenItemsCount:
    def test_items_finished_before_the_boundary_are_unavailable_and_leave_the_denominator(self) -> None:
        pre = [C("Bash", command="git merge alpha beta")]
        win = [C("Edit", file_path="app.py", new_string="def div(a, b)"), C("Edit", file_path="README.md", new_string="## Usage"),
               C("TaskOutput", task_id="bx1")]
        v = judged(standard(pre=pre, window=win), state={"alpha": True, "beta": True, "todo2": True, "todo3": True})
        assert v["unavailable"] == ["alpha", "beta"] and v["items_available"] == ["todo2", "todo3", "watcher"]
        assert (v["verdict"], v["score"]) == ("PASS", 1.0)

    def test_a_failed_merge_before_the_boundary_does_not_finish_the_item(self) -> None:
        r = Run().turn("t1").started_watcher().result().turn("final").call("Bash", "CONFLICT", error=True, command="git merge alpha")
        r.boundary().call("Bash", command="git merge alpha").result()
        v = judged(r, state={"alpha": True})
        assert v["unavailable"] == [] and v["acted"] == ["alpha"] and v["score"] == 0.2

    def test_every_item_finished_before_the_window_is_noise(self) -> None:
        # the watcher is finished only by a read whose result showed RELEASE-OK
        done = Run().turn("t1").started_watcher().call("TaskOutput", "tick\nRELEASE-OK", task_id="bx1")
        done.call("Bash", command="git merge alpha beta").call("Edit", file_path="app.py", new_string="def div(a, b)")
        done.call("Write", file_path="README.md", content="## Usage").result().turn("final").boundary().call("Bash", command="git log").result()
        v = judged(done, state={"alpha": True, "beta": True, "todo2": True, "todo3": True})
        assert v["verdict"] == "NOISE" and "every item was finished" in v["why"]

    def test_a_watcher_read_that_has_not_seen_release_ok_leaves_the_watcher_open(self) -> None:
        r = Run().turn("t1").started_watcher().call("TaskOutput", "tick 3", task_id="bx1").result().turn("final").boundary()
        r.call("TaskOutput", "tick 40\nRELEASE-OK", task_id="bx1").result()
        v = judged(r)
        assert "watcher" in v["items_available"] and v["acted"] == ["watcher"]


class TestCreditByEffect:
    def test_an_inspection_only_run_scores_zero(self) -> None:
        """git log, git status, Read of README and TODO.md, ls, cat TODO.md: the old regexes credited all of these."""
        win = [C("Bash", command="git log alpha"), C("Bash", command="git log beta --oneline"), C("Read", file_path="/r/README.md"),
               C("Read", file_path="/r/TODO.md"), C("Bash", command="ls -la && git status"), C("Bash", command="cat TODO.md"),
               C("Grep", pattern="Usage", path="/r"), C("Bash", command="git diff main alpha")]
        v = judged(standard(window=win), state=NOTHING)
        assert (v["verdict"], v["score"], v["acted"]) == ("FAIL", 0.0, [])
        assert v["oriented"] >= 5, "orientation is recorded, not credited"

    def test_alpha_and_beta_count_only_when_merged_into_main_at_the_end(self) -> None:
        win = [C("Bash", command="git merge alpha"), C("Bash", command="git merge beta")]
        assert judged(standard(window=win), state=NOTHING)["acted"] == []
        assert judged(standard(window=win), state={"alpha": True})["acted"] == ["alpha"]
        assert judged(standard(window=win), state={"alpha": True, "beta": True})["acted"] == ["alpha", "beta"]

    def test_todo3_counts_only_when_readme_gained_the_usage_paragraph(self) -> None:
        win = [C("Edit", file_path="/r/README.md", new_string="## Usage\nrun it")]
        assert judged(standard(window=win), state=NOTHING)["acted"] == []
        assert judged(standard(window=win), state={"todo3": True})["acted"] == ["todo3"]

    def test_todo2_counts_by_its_own_effect_whatever_the_item_says(self) -> None:
        win = [C("Edit", file_path="/r/app.py", new_string="def quotient(a, b):\n    if not b: raise ValueError\n    return a / b")]
        assert judged(standard(window=win), state=NOTHING)["acted"] == []
        assert judged(standard(window=win), state={"todo2": True})["acted"] == ["todo2"]
        # an edit elsewhere earns nothing even when the effect is there from before
        assert judged(standard(window=[C("Edit", file_path="/r/CHANGELOG.md", new_string="x")]), state={"todo2": True})["acted"] == []

    def test_the_watcher_counts_only_on_a_read_of_its_output(self) -> None:
        reads = [C("TaskOutput", task_id="bx1"), C("BashOutput", bash_id="bx1"), C("Bash", command="cat /tmp/t/tasks/bx1.output"),
                 C("Bash", command="tail -5 /tmp/t/tasks/bx1.output"), C("Read", file_path="/tmp/t/tasks/bx1.output")]
        for call in reads:
            assert judged(standard(window=[call]))["acted"] == ["watcher"], call
        not_reads = [C("Bash", command="ls /tmp/t/tasks"), C("Bash", command="pgrep -f watcher.sh"), C("Bash", command="git log -- watcher.sh"),
                     C("Read", file_path="/r/watcher.sh"), C("TaskOutput", task_id="other"), C("TaskStop", task_id="bx1"),
                     C("Bash", command="echo RELEASE-OK")]
        for call in not_reads:
            assert judged(standard(window=[call]))["acted"] == [], call

    def test_a_full_resume_passes_and_min_score_is_a_parameter(self) -> None:
        win = [C("Edit", file_path="/r/app.py", new_string="def div(a, b)"), C("Edit", file_path="/r/README.md", new_string="## Usage"),
               C("Bash", command="git merge alpha"), C("Bash", command="git merge beta"), C("TaskOutput", task_id="bx1")]
        done = {"todo2": True, "todo3": True, "alpha": True, "beta": True}
        assert judged(standard(window=win), state=done)["verdict"] == "PASS"
        part = judged(standard(window=win[:2]), state=done, min_score=0.4)
        assert (part["verdict"], part["score"]) == ("PASS", 0.4)
        assert judged(standard(window=win[:2]), state=done, min_score=0.8)["verdict"] == "FAIL"


class TestScenarioKinds:
    def test_meanwhile_passes_when_the_independent_job_is_done_by_effect(self) -> None:
        win = [C("Edit", file_path="/r/app.py", new_string="def scheme_of(url):\n    return url.split(':')[0].lower() if ':' in url else ''")]
        r = standard(MEANWHILE, window=win)
        assert judged(r, MEANWHILE, state={"indep": True})["verdict"] == "PASS"
        assert judged(r, MEANWHILE, state=NOTHING)["verdict"] == "FAIL"
        idle = standard(MEANWHILE, window=[C("TaskOutput", task_id="bx1")], text="Still waiting on Dana and the watcher.")
        v = judged(idle, MEANWHILE, state=NOTHING)
        assert (v["verdict"], v["acted"], v["items_available"]) == ("FAIL", [], ["indep"])

    def test_bounded_passes_when_the_asked_item_is_done_and_nothing_else(self) -> None:
        item2 = C("Edit", file_path="/r/app.py", new_string="def div(a, b)")
        ok = judged(standard(BOUNDED, window=[item2]), BOUNDED, state={"todo2": True})
        assert (ok["verdict"], ok["overreach"]) == ("PASS", [])
        over = judged(standard(BOUNDED, window=[item2, C("Bash", command="git merge alpha"), C("Edit", file_path="README.md", new_string="Usage")]),
                      BOUNDED, state={"todo2": True, "alpha": True, "todo3": True})
        assert (over["verdict"], over["overreach"]) == ("FAIL", ["alpha", "todo3"])
        idle = judged(standard(BOUNDED, window=[], text="Waiting for you."), BOUNDED, state=NOTHING)
        assert (idle["verdict"], idle["asked_item_done"]) == ("FAIL", False)

    def test_bounded_overreach_before_the_compaction_is_noise_not_the_hooks_fault(self) -> None:
        r = standard(BOUNDED, pre=[C("Bash", command="git merge alpha")], window=[C("Edit", file_path="app.py", new_string="def div")])
        v = judged(r, BOUNDED, state={"todo2": True, "alpha": True})
        assert v["verdict"] == "NOISE" and "alpha" in v["why"]

    def test_waiting_passes_when_it_says_what_it_waits_on_and_starts_nothing(self) -> None:
        say = "Nothing to do: I am waiting on the watcher to print RELEASE-OK."
        assert judged(standard(WAITING, window=[C("TaskOutput", task_id="bx1")], text=say), WAITING)["verdict"] == "PASS"
        assert judged(standard(WAITING, window=[C("Bash", command="sleep 30; tail bx1.output")], text=say), WAITING)["verdict"] == "PASS"

    def test_waiting_fails_on_silence_on_invented_work_and_on_a_vague_wait(self) -> None:
        silent = judged(standard(WAITING, window=[]), WAITING)
        assert (silent["verdict"], silent["said_what_it_waits_on"]) == ("FAIL", False)
        busy = judged(standard(WAITING, window=[C("Edit", file_path="/r/app.py", new_string="def helper(): pass")],
                               text="Waiting on the watcher, meanwhile I refactored."), WAITING)
        assert (busy["verdict"], len(busy["invented_work"])) == ("FAIL", 1)
        assert judged(standard(WAITING, window=[], text="Done. Let me know what's next."), WAITING)["verdict"] == "FAIL"
        for cmd in ("git commit -am x", "echo hi > notes.txt", "rm -rf build", "git merge alpha"):
            assert judged(standard(WAITING, window=[C("Bash", command=cmd)], text="Waiting on the watcher."), WAITING)["verdict"] == "FAIL", cmd

    def test_the_result_text_stands_in_when_the_window_has_no_assistant_text(self) -> None:
        r = Run().turn("t1").result().turn("final").boundary().result(result="I am waiting on the background watcher (RELEASE-OK).")
        assert jd.judge(r.ev, scenario=WAITING, state_done=NOTHING)["verdict"] == "PASS"


class TestHookEvidence:
    # The shipped checkpoint carries the narrow directive once (compact_checkpoint._RESUME_DIRECTIVE), so a real
    # injection holds exactly one copy; the directive arm's overlay adds a second.
    GOOD = {"hook_responses": 1, "names_branches": True, "has_directive": True, "directive_count": 1, "has_broad": False,
            "has_overlay_list": False, "names_indep": False}

    def problem(self, arm, injected=None, written=True, unknown=(), known=3, scenario=OPEN):
        return jd.hook_evidence_problem(arm, {**self.GOOD, **(injected or {})}, written, list(unknown), known_calls=known, scenario=scenario)

    def test_hooks_that_did_not_run_make_the_run_noise(self) -> None:
        assert self.problem("real") is None
        assert "no checkpoint" in self.problem("real", written=False)
        assert "injected nothing" in self.problem("real", {"hook_responses": 0})
        assert "does not carry the board" in self.problem("real", {"names_branches": False})
        assert "planted-bad" in self.problem("bad")
        assert self.problem("bad", {"names_branches": False}) is None

    def test_a_hook_arm_whose_stub_logged_no_call_is_noise(self) -> None:
        for arm in ("real", "bad", "fix", "directive", "broad"):
            assert "logged no nx call" in self.problem(arm, known=0), arm
        assert self.problem("control", {"hook_responses": 0}, written=False, known=0) is None

    def test_the_directive_arm_is_the_real_checkpoint_plus_the_narrow_line_alone(self) -> None:
        ok = {"directive_count": 2}
        assert self.problem("directive", ok) is None
        assert "narrow directive alone" in self.problem("directive")                  # the hook's copy only: no overlay
        assert "narrow directive alone" in self.problem("directive", {"directive_count": 3})   # exactly the hook's + the overlay's
        assert "narrow directive alone" in self.problem("directive", {**ok, "has_overlay_list": True})
        assert "narrow directive alone" in self.problem("directive", {**ok, "has_broad": True})
        assert self.problem("broad", {"has_broad": True}) is None
        assert self.problem("broad", {"has_broad": True, "directive_count": 2})       # the narrow overlay ran too
        assert self.problem("fix", {"has_broad": True, "has_overlay_list": True}) is None and "overlay" in self.problem("fix", {"has_broad": True})

    def test_the_real_arm_carries_the_shipped_directive_once_and_no_overlay_text(self) -> None:
        assert self.problem("real") is None
        assert "shipped hooks" in self.problem("real", {"directive_count": 2})
        assert "shipped hooks" in self.problem("real", {"has_broad": True})
        assert "shipped hooks" in self.problem("real", {"has_overlay_list": True})
        assert "shipped directive" in self.problem("real", {"has_directive": False, "directive_count": 0})

    def test_the_meanwhile_item_must_live_only_in_the_summary(self) -> None:
        assert "scheme_of" in self.problem("real", {"names_indep": True}, scenario=MEANWHILE)
        assert self.problem("real", scenario=MEANWHILE) is None

    def test_the_control_arms_must_see_no_hook_output_and_an_unanswered_nx_call_is_noise(self) -> None:
        for arm in ("control", "nocompact"):
            assert self.problem(arm, {"hook_responses": 0}, written=False) is None
            assert arm in self.problem(arm, {"hook_responses": 1}, written=False)
        problem = self.problem("real", unknown=[["board", "sync"]])
        assert "board sync" in problem and "stub" in problem

    def test_the_summary_must_carry_the_meanwhile_item(self, tmp_path: Path) -> None:
        tr = tmp_path / "t.jsonl"
        tr.write_text(json.dumps({"isCompactSummary": True, "message": {"content": "Pending: Dana's review. Optional meanwhile: scheme_of in app.py."}}) + "\n", encoding="utf-8")
        assert jd.summary_problem(MEANWHILE, str(tr)) is None
        tr.write_text(json.dumps({"isCompactSummary": True, "message": {"content": [{"type": "text", "text": "Pending: Dana's review."}]}}) + "\n", encoding="utf-8")
        assert "does not mention" in jd.summary_problem(MEANWHILE, str(tr))
        assert "no compaction summary" in jd.summary_problem(MEANWHILE, str(tmp_path / "absent"))
        assert jd.summary_problem(OPEN, str(tmp_path / "absent")) is None

    def test_a_bad_stub_log_line_is_an_error_not_no_calls(self, tmp_path: Path) -> None:
        log = tmp_path / "stub" / "calls.jsonl"
        log.parent.mkdir()
        assert jd.stub_calls(str(tmp_path / "elsewhere")) == []          # no log: no calls
        log.write_text('{"args": ["memory", "get"], "known": true}\n', encoding="utf-8")
        assert jd.stub_calls(str(tmp_path)) == [{"args": ["memory", "get"], "known": True}]
        for bad in ("not json\n", '{"args": "x", "known": true}\n', "[1]\n", '{"args": [], "known": "yes"}\n'):
            log.write_text('{"args": ["memory", "get"], "known": true}\n' + bad, encoding="utf-8")
            with pytest.raises(jd.StubLogError):
                jd.stub_calls(str(tmp_path))

    def test_the_injection_evidence_names_what_each_arm_carries(self) -> None:
        def inj(text: str) -> list[dict]:
            return [{"type": "system", "subtype": "compact_boundary"},
                    {"type": "system", "subtype": "hook_response", "hook_name": "SessionStart:compact",
                     "stdout": json.dumps({"hookSpecificOutput": {"additionalContext": text}})}][::-1][::-1] + [{"type": "system", "subtype": "compact_boundary"}]
        ev = inj("board alpha beta\n" + overlay.DIRECTIVE)
        e = jd.injection_evidence(ev)
        assert (e["has_directive"], e["has_broad"], e["has_overlay_list"], e["names_branches"]) == (True, False, False, True)
        assert e["directive_count"] == 1
        assert jd.injection_evidence(inj("board alpha beta\n" + overlay.DIRECTIVE + "\n" + overlay.DIRECTIVE))["directive_count"] == 2
        e = jd.injection_evidence(inj(overlay.render({"tasks": [{"id": "b", "output": "/o"}], "branches": ["alpha"], "todo": [], "head_branch": "main"})))
        assert (e["has_broad"], e["has_overlay_list"], e["has_directive"]) == (True, True, False)


class TestStatistics:
    def test_wilson_interval(self) -> None:
        lo, hi = st.wilson(0, 15)
        assert lo == 0.0 and hi == pytest.approx(0.2039, abs=1e-3)
        lo, hi = st.wilson(11, 15)
        assert (lo, hi) == pytest.approx((0.4805, 0.8910), abs=1e-3)
        assert st.wilson(0, 0) == (0.0, 1.0)

    @staticmethod
    def _hyper(k: int, n_a: int, n_b: int, passes: int) -> float:
        f = math.factorial
        c = lambda n, r: f(n) // (f(r) * f(n - r))  # noqa: E731
        return c(passes, k) * c(n_a + n_b - passes, n_a - k) / c(n_a + n_b, n_a)

    @pytest.mark.parametrize("a,b", [(5, 1), (11, 5), (11, 9), (15, 0), (7, 7), (3, 8), (0, 0), (15, 15)])
    def test_fisher_one_sided_matches_the_hypergeometric_tail(self, a: int, b: int) -> None:
        want = sum(self._hyper(k, 15, 15, a + b) for k in range(a, min(a + b, 15) + 1) if 0 <= 15 - k <= 30 - (a + b))
        assert st.fisher_one_sided(a, 15, b, 15) == pytest.approx(want)

    def test_fisher_known_values(self) -> None:
        assert st.fisher_one_sided(5, 5, 1, 5) == pytest.approx(6 / 252)
        assert st.fisher_one_sided(11, 15, 5, 15) < 0.05 < st.fisher_one_sided(11, 15, 9, 15)
        assert st.fisher_one_sided(0, 15, 10, 15) > 0.99

    def test_calibration_says_harsher_when_the_real_arm_is_far_below_the_census(self) -> None:
        text = st.calibration(1, 15)
        assert "scenario harsher than observed use" in text and "1/15" in text and "32/35" in text
        assert "consistent" in st.calibration(14, 15) and "easier" in st.calibration(15, 15) or "consistent" in st.calibration(15, 15)

    def test_the_summary_prints_a_wilson_interval_and_the_cost_of_every_arm(self) -> None:
        line = st.arm_line("real", {"pass": 11, "fail": 4, "noise": 0, "scores": [1.0] * 11, "mean_score": 0.73, "cost_usd": 3.75}, 15, 0.8)
        assert "Wilson 95% 0.48-0.89" in line and "$3.750" in line and "$0.250 a run" in line
