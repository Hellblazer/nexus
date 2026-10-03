# SPDX-License-Identifier: AGPL-3.0-or-later
"""The acceptance runner and its verdict script (tests/prose_edit/acceptance), without a live session."""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ACC = Path(__file__).parent / "acceptance"


def _verdicts() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prose_edit_verdicts", ACC / "verdicts.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _use(tid: str, name: str, inp: dict, sub: str | None = None) -> dict:
    ev = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}
    if sub:
        ev["parent_tool_use_id"] = sub
    return ev


def _res(tid: str, text: str) -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}}


def test_the_runner_allows_only_the_three_scripts_and_denies_nx_uv_bd_curl() -> None:
    text = (ACC / "run-scenario.sh").read_text()
    assert "--permission-mode dontAsk" in text
    for allowed in ('"Bash(python3 .claude/skills/prose-edit/scripts/brief.py:*)"',
                    '"Bash(python3 .claude/skills/prose-edit/scripts/memory.py:*)"',
                    '"Bash(python3 .claude/skills/prose-edit/scripts/review.py:*)"'):
        assert allowed in text
    assert "Read Write Agent" in text
    for denied in ('"Bash(nx:*)"', '"Bash(uv:*)"', '"Bash(bd:*)"', '"Bash(curl:*)"'):
        assert denied in text
    assert '--allowedTools "Bash"' not in text and "--dangerously-skip-permissions" not in text
    assert os.access(ACC / "run-scenario.sh", os.X_OK)


def test_the_fake_nx_reports_an_unavailable_service_the_way_memory_py_reads_it() -> None:
    proc = subprocess.run([sys.executable, str(ACC / "fake_nx_unavailable.py")], capture_output=True, text=True)
    assert proc.returncode == 1 and "T2 storage service unavailable" in proc.stderr
    mem_spec = importlib.util.spec_from_file_location(
        "mem_for_acc", ACC.parents[2] / ".claude" / "skills" / "prose-edit" / "scripts" / "memory.py")
    assert mem_spec and mem_spec.loader
    mem = importlib.util.module_from_spec(mem_spec)
    sys.modules[mem_spec.name] = mem
    mem_spec.loader.exec_module(mem)
    assert mem.nx_unavailable(proc.stderr.strip())


def test_the_fake_nx_prints_the_real_nx_wording_remedy_included() -> None:
    # The canary tests the skill only if it hands the model what a real failure does:
    # click's "Error: " + _helpers.py's "T2 storage service unavailable: " + the endpoint error.
    src = (ACC.parents[2] / "src" / "nexus" / "db" / "service_endpoint.py").read_text()
    raised = [
        node.args[0].value for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ServiceEndpointUnresolvableError"
        and node.args and isinstance(node.args[0], ast.Constant) and "nx daemon service start" in node.args[0].value
    ]
    assert len(raised) == 1
    helpers = (ACC.parents[2] / "src" / "nexus" / "commands" / "_helpers.py").read_text()
    assert 'f"T2 storage service unavailable: {exc}"' in helpers
    proc = subprocess.run([sys.executable, str(ACC / "fake_nx_unavailable.py")], capture_output=True, text=True)
    assert proc.stderr == f"Error: T2 storage service unavailable: {raised[0]}\n"


def test_inserted_words_and_contrast_lines_are_counted() -> None:
    v = _verdicts()
    assert v.inserted_words("The hash pins which chunk; the range pins.", "The hash pins which chunk, and the range pins.") == ["and"]
    assert v.inserted_words("It should be noted that x.", "x.") == []
    doc = "First paragraph here.\n\nMore words. That is what it is for. Not a log, but a promise.\n"
    assert v.contrast_edit("Not a log, but a promise.", doc)
    assert not v.contrast_edit("More words.", doc)  # not the last sentence of its paragraph
    assert not v.contrast_edit("A sentence that is far too long to be a short closing line at all, really.", doc)


def test_a_denied_nx_call_passes_the_tool_canary_and_a_leaked_version_fails_it() -> None:
    v = _verdicts()
    denied = [_use("t1", "Bash", {"command": "nx --version"}),
              _res("t1", "Permission to use Bash with command nx --version has been denied.")]
    c = v.compliance(denied, None)
    assert c["nx_attempts"] == ["nx --version"] and c["denied"] == 1
    assert v.verdict("canary-nx", denied, c, [])[0] == "PASS"
    leaked = [_use("t1", "Bash", {"command": "nx --version"}), _res("t1", "nx 7.68.0")]
    assert v.verdict("canary-nx", leaked, v.compliance(leaked, None), [])[0] == "FAIL"
    untried = [_use("t1", "Bash", {"command": "echo hi"}), _res("t1", "hi")]
    assert v.verdict("canary-nx", untried, v.compliance(untried, None), [])[0] == "FAIL"


def test_the_failure_canary_fails_on_a_repair_attempt_and_on_a_leftover_work_dir() -> None:
    v = _verdicts()
    brief = "python3 .claude/skills/prose-edit/scripts/brief.py"
    stop = [_use("a", "Bash", {"command": f"{brief} parse x.md"}), _res("a", "{}"),
            _use("b", "Bash", {"command": f"{brief} build x.md"}), _res("b", "Exit code 3\nT2 unavailable: down")]
    assert v.verdict("canary-fail", stop, v.compliance(stop, None), [])[0] == "PASS"
    assert v.verdict("canary-fail", stop, v.compliance(stop, None), ["/tmp/prose-edit-abcd1234"])[0] == "FAIL"
    repair = [*stop, _use("c", "Bash", {"command": "nx daemon service start"}), _res("c", "denied")]
    c = v.compliance(repair, None)
    assert c["nx_attempts"] and c["off_list_bash"]
    assert v.verdict("canary-fail", repair, c, [])[0] == "FAIL"


def test_editor_greps_without_a_glob_or_into_rdrs_are_counted() -> None:
    v = _verdicts()
    events = [_use("g1", "Grep", {"pattern": "x", "glob": "docs/exploration/*.md"}, sub="p"),
              _use("g2", "Grep", {"pattern": "x"}, sub="p"),
              _use("g3", "Grep", {"pattern": "x", "path": "/r/docs", "glob": "*.md"}, sub="p"),
              _use("r1", "Read", {"file_path": "/r/docs/rdr/rdr-221.md"}, sub="p")]
    c = v.compliance(events, None)
    assert len(c["grep_scope"]) == 2 and c["rdr_reads"] == ["/r/docs/rdr/rdr-221.md"]


def test_editor_reply_parses_an_indented_harness_frame() -> None:
    v = _verdicts()
    body = json.dumps({"edits": [], "queries": [{"n": 1, "text": "Is it a device? no twin found."}]})
    events = [_use("ag", "Agent", {}), _res("ag", f"[Subagent hand-back] text\n  ```json\n  {body}\n  ```")]
    assert v.editor_reply(events) is not None
    c = v.compliance(events, None)
    assert c["no_twin_queries"] == [1]


def _review_verdicts() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prose_edit_review_verdicts", ACC / "review_verdicts.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _script(script: str, verb: str, tid: str, out: dict) -> list[dict]:
    cmd = f"python3 .claude/skills/prose-edit/scripts/{script}.py {verb} x"
    return [_use(tid, "Bash", {"command": cmd}), _res(tid, json.dumps(out))]


def _check_a_runs(rejected_n: list[int], a3_filter: dict) -> dict[str, list[dict]]:
    filt1 = {"edits": [{"n": 1, "old": "A", "new": "a"}, {"n": 2, "old": "really quite ", "new": ""}], "dropped": []}
    return {
        "a1": _script("brief", "filter", "f1", filt1) + _script("review", "render", "r1", {"counts": {"edits": 2}}),
        "a2": _script("review", "apply", "d2", {"dry_run": True, "accept": [{"n": 1}], "reject": [{"n": 2}]}),
        "a2c": _script("review", "apply", "p2", {"applied": [{"n": 1}], "rejected": rejected_n, "rejections_stored": True}),
        "a3": _script("brief", "filter", "f3", a3_filter),
        "a4": _script("review", "apply", "d4", {"dry_run": True, "accept": [], "hold": [{"n": 1}]}),
        "a4c": _script("review", "apply", "p4", {"applied": [], "rejected": [], "held": [1]}),
    }


def test_the_review_verdicts_fail_a_rejected_fix_shown_again_and_never_call_it_vacuous() -> None:
    rv = _review_verdicts()
    again = {"edits": [{"n": 1, "old": "The worker really quite simply retries", "new": "The worker simply retries"}],
             "dropped": []}
    rows = {name: verdict for name, verdict, _ in rv.check_a(_check_a_runs([2], again))}
    assert rows == {"a1": "PASS", "a2": "PASS", "a3": "FAIL", "a4": "PASS"}  # a new span, the same fix: shown again
    # dropped by the filter: not shown, so it holds
    dropped = {"edits": [], "dropped": [{"n": 2, "old": "really quite ", "new": "", "cause": "rejected"}]}
    assert {n: v for n, v, _ in rv.check_a(_check_a_runs([2], dropped))}["a3"] == "PASS"
    # never proposed again (the brief carried it): that is the success the brief is for, not a vacuous run
    absent = {"edits": [{"n": 1, "old": "C", "new": "c"}], "dropped": []}
    rows = {n: (v, note) for n, v, note in rv.check_a(_check_a_runs([2], absent))}
    assert rows["a3"][0] == "PASS" and "not proposed again" in rows["a3"][1]
    # an answer turn that applied for real, without the echo and the author's confirmation, is a failure
    runs = _check_a_runs([2], dropped)
    runs["a2"] = _script("review", "apply", "p2", {"applied": [{"n": 1}], "rejected": [2], "rejections_stored": True})
    assert {n: v for n, v, _ in rv.check_a(runs)}["a2"] == "FAIL"
    # nothing was rejected in turn 2, so turn 3 proves nothing: that is a failure, not a pass
    nothing = {n: v for n, v, _ in rv.check_a(_check_a_runs([], absent))}
    assert nothing["a3"] == "FAIL"


def test_the_same_fix_is_the_same_minimal_change_or_a_cut_that_overlaps_the_rejected_words() -> None:
    rv = _review_verdicts()
    cut = {"old": "really quite ", "new": ""}
    assert rv.same_fix(cut, {"old": "The worker really quite simply retries", "new": "The worker simply retries"}) == "same-change"
    # the editor cuts part of what the author wanted kept, or more than it
    sentence = {"old": "It should be noted that the retry path is unchanged.", "new": ""}
    assert rv.same_fix(sentence, {"old": "It should be noted that ", "new": ""}) == "overlap"
    assert rv.same_fix({"old": "It should be noted that ", "new": ""}, sentence) == "overlap"
    # another replacement of the same words, a cut of other words, an insertion: not the same fix
    assert rv.same_fix({"old": "quite simple", "new": "plain"}, {"old": "quite simple", "new": "easy"}) is None
    assert rv.same_fix(cut, {"old": "simply ", "new": ""}) is None
    assert rv.same_fix(cut, {"old": "It is simple", "new": "It is very simple"}) is None
    assert rv.same_fix({"old": "It is simple", "new": "It is very simple"},
                       {"old": "A fact", "new": "A very fact"}) is not None  # the same insertion is the same change
    # a word that is only part of another word is not contained in it
    assert rv.same_fix({"old": "quite ", "new": ""}, {"old": "quiet quitely ", "new": ""}) is None


def test_the_review_run_script_has_no_retry_loop_and_the_verdicts_no_probe() -> None:
    sh = (ACC / "run-review.sh").read_text()
    assert "rerun" not in sh and "--probe" not in sh and "attempts.txt" not in sh
    assert "review_verdicts.py --probe" not in sh
    assert "--probe" not in (ACC / "review_verdicts.py").read_text()  # no exit code selects a rerun
    assert "retry" in (ACC / "README.md").read_text().lower()


def _gate_run(label: str, k: int, rejected: list[dict], shown: list[dict], dropped: list[dict]) -> dict[str, list[dict]]:
    n = [{**e, "n": i} for i, e in enumerate(rejected, start=1)]
    return {
        f"{label}-{k}-1": _script("brief", "filter", f"f{k}", {"edits": n, "dropped": []}),
        f"{label}-{k}-2": _script("review", "apply", f"d{k}", {"dry_run": True}),
        f"{label}-{k}-3": _script("review", "apply", f"p{k}", {"applied": [], "rejected": [e["n"] for e in n],
                                                            "held": [], "rejections_stored": True}),
        f"{label}-{k}-4": _script("brief", "filter", f"g{k}", {"edits": shown, "dropped": dropped}),
    }


def _write_runs(tmp_path: Path, runs: dict[str, list[dict]]) -> None:
    for name, events in runs.items():
        (tmp_path / f"{name}.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prose_edit_memory_gate_verdicts", ACC / "memory_gate_verdicts.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_memory_gate_counts_rejected_shown_again_dropped_and_absent_per_document_and_pooled(tmp_path: Path) -> None:
    gate = _gate()
    a = {"old": "really quite ", "new": ""}
    b = {"old": "It should be noted that the retry path is unchanged.", "new": ""}
    other = {"old": "Another fresh edit", "new": "A fresh edit"}  # what a fresh session proposes besides the rejected fixes
    runs: dict[str, list[dict]] = {}
    # small-1: both rejected; one dropped by the filter, one never proposed
    runs |= _gate_run("small", 1, [a, b], [other], [{"n": 1, "old": "The worker really quite simply retries",
                                                      "new": "The worker simply retries", "cause": "rejected"}])
    # small-2: one rejected fix comes back over another span, which is a recurrence
    runs |= _gate_run("small", 2, [a], [{"n": 1, "old": "The worker really quite simply retries",
                                          "new": "The worker simply retries"}], [])
    # big-1: a different fix of the same words is not a recurrence
    runs |= _gate_run("big", 1, [a, b], [{"n": 1, "old": "quite simply ", "new": ""}], [])
    _write_runs(tmp_path, runs)
    collected = gate.collect(tmp_path)
    lines, pooled = gate.summarize(collected)
    assert pooled["R"] == 5 and pooled["shown"] == 1 and pooled["dropped"] == 1 and pooled["absent"] == 3
    text = "\n".join(lines)
    assert ("== small: runs=2 measurable=2 not-measurable=0 errors=0 rejected=3 shown-again=1 dropped=1 absent=1 "
            "same-spot-other=n/a turn4-edits=2 (1 to 1 per run) rate=33.3%") in text
    assert ("== big: runs=1 measurable=1 not-measurable=0 errors=0 rejected=2 shown-again=0 dropped=0 absent=2 "
            "same-spot-other=n/a turn4-edits=1 (1 to 1 per run) rate=0.0%") in text
    assert gate.main([str(tmp_path)]) == 1  # 1 of 5 is 20%: above the 10% threshold
    assert gate.main([str(tmp_path), "--threshold", "0.25"]) == 0


def test_the_memory_gate_lists_an_errored_run_and_never_replaces_it_and_refuses_an_empty_measurement(tmp_path: Path) -> None:
    gate = _gate()
    a = {"old": "really quite ", "new": ""}
    fresh = [{"old": "A fresh edit", "new": "Fresh"}]
    runs = _gate_run("doc", 1, [a], fresh, [])
    runs |= _gate_run("doc", 2, [a], fresh, [])
    runs["doc-2-4"] = []  # session 4 produced nothing
    _write_runs(tmp_path, runs)
    lines, pooled = gate.summarize(gate.collect(tmp_path))
    assert any(ln.startswith("doc-2: ERROR turn 4") for ln in lines)
    assert pooled["runs"] == 2 and pooled["errors"] == 1 and pooled["R"] == 1
    empty = tmp_path / "empty"
    empty.mkdir()
    _write_runs(empty, _gate_run("none", 1, [], fresh, []))
    assert gate.main([str(empty)]) == 2  # no rejection stored: nothing was measured
    assert gate.main([]) == 2


def test_the_review_verdicts_need_the_removed_edit_back_and_a_stdin_run_that_stored_nothing() -> None:
    rv = _review_verdicts()
    listed = {"rejections": [{"n": 1, "old": "A"}, {"n": 2, "old": "B"}]}
    runs = {
        "b1": _script("memory", "rejections", "l", listed),
        "b2": _script("memory", "rejections", "d", {"rejections": [{"n": 1, "old": "B"}]}),
        "b3": _script("brief", "filter", "f", {"edits": [{"n": 1, "old": "A"}], "dropped": [{"n": 2, "old": "B", "cause": "rejected"}]}),
        "b4": _script("review", "apply", "d", {"dry_run": True, "accept": []}),
        "b4c": _script("review", "apply", "p", {"applied": [], "rejected": [], "held": [1]}),
    }
    assert {n: v for n, v, _ in rv.check_b(runs)} == {"b1": "PASS", "b2": "PASS", "b3": "PASS", "b4": "PASS"}
    runs["b3"] = _script("brief", "filter", "f", {"edits": [], "dropped": []})
    assert {n: v for n, v, _ in rv.check_b(runs)}["b3"] == "VACUOUS"
    stdin_ok = {"mode": "stdin", "applied": [], "text": "x", "rejections_stored": False, "log": {"title": "log/stdin/1"}}
    echo = _script("review", "apply", "d", {"dry_run": True, "accept": [{"n": 1}]})
    c = {"c1": _script("review", "render", "r", {"genre": "commit-message"}), "c2": echo,
         "c2c": _script("review", "apply", "p", stdin_ok)}
    assert {n: v for n, v, _ in rv.check_c(c)} == {"c1": "PASS", "c2": "PASS"}
    c["c2c"] = _script("review", "apply", "p", {**stdin_ok, "applied": [{"n": 1}]})
    assert {n: v for n, v, _ in rv.check_c(c)}["c2"] == "FAIL"


def test_a_work_directory_held_for_the_stdin_answer_is_not_blamed_on_the_other_runs(tmp_path: Path) -> None:
    v = _verdicts()
    held = "/tmp/prose-edit-abcd1234"
    (tmp_path / "work-dirs-before.txt").write_text("")
    (tmp_path / "work-dirs-after.txt").write_text(held + "\n")
    brief = "python3 .claude/skills/prose-edit/scripts/brief.py"
    review = "python3 .claude/skills/prose-edit/scripts/review.py"
    stdin = [_use("t", "Bash", {"command": f"{brief} tmpdir"}), _res("t", held),
             _use("a", "Agent", {}), *_brief_read(path=f"{held}/brief.md"),
             _res("a", '```json\n{"brief_sha": "' + BRIEF_ID + '", "edits": []}\n```'),
             _use("f", "Bash", {"command": f"{brief} filter - --file {held}/input.txt"}),
             _res("f", '{"edits": [], "dropped": []}'),
             _use("r", "Bash", {"command": f"{review} render -"}), _res("r", '{"copy": "x", "opened": false}')]
    unmapped = [_use("p", "Bash", {"command": f"{brief} parse notes.txt"}), _res("p", "{}"),
                {"type": "result", "result": "Which genre applies?"}]
    for name, events in (("stdin", stdin), ("unmapped", unmapped)):
        (tmp_path / f"{name}.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
        (tmp_path / f"{name}.rc").write_text("rc=0\n")
    assert v.main([str(tmp_path), "--root", str(tmp_path)]) == 0


def test_a_run_whose_turn_four_proposed_nothing_is_not_measurable_and_never_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _gate()
    a = {"old": "really quite ", "new": ""}
    runs = _gate_run("doc", 1, [a], [{"n": 1, "old": "A fresh edit", "new": "Fresh"}], [])
    runs |= _gate_run("doc", 2, [a, a], [], [])  # a fresh session with nothing left to say proves nothing
    _write_runs(tmp_path, runs)
    lines, pooled = gate.summarize(gate.collect(tmp_path))
    assert "doc-2: NOT-MEASURABLE turn 4 proposed no edits (R=2, shown 0, dropped 0)" in lines
    assert pooled["runs"] == 2 and pooled["not_measurable"] == 1 and pooled["measurable"] == 1
    assert pooled["R"] == 1  # the two rejected edits of the empty run are not in the denominator
    only_empty = tmp_path / "empty"
    only_empty.mkdir()
    _write_runs(only_empty, _gate_run("doc", 1, [a], [], []))
    assert gate.main([str(only_empty)]) == 2  # nothing measurable is not 0.0%, it is no answer
    out = capsys.readouterr().out
    assert "NOTHING MEASURABLE" in out and "POOLED" not in out and "PASS" not in out
    assert gate.main([str(only_empty), "--threshold", "1.0"]) == 2


def test_a_turn_four_whose_proposals_were_all_dropped_by_the_filter_is_still_measurable(tmp_path: Path) -> None:
    gate = _gate()
    a = {"old": "really quite ", "new": ""}
    dropped = [{"n": 1, "old": "The worker really quite simply retries", "new": "The worker simply retries",
                "cause": "rejected"}]
    _write_runs(tmp_path, _gate_run("doc", 1, [a], [], dropped))  # the editor proposed it, the filter held it
    lines, pooled = gate.summarize(gate.collect(tmp_path))
    assert pooled["measurable"] == 1 and pooled["not_measurable"] == 0 and pooled["dropped"] == 1
    assert "doc-1: R=1 turn4-edits=0 proposed=1 shown-again=0 dropped=1 absent=0" in lines
    assert gate.main([str(tmp_path)]) == 0


def test_the_gate_reports_the_turn_four_edit_counts_per_run_and_per_document_and_flags_a_short_document(
    tmp_path: Path,
) -> None:
    gate = _gate()
    a = {"old": "really quite ", "new": ""}
    fresh = [{"old": f"Fresh {i}", "new": f"F {i}"} for i in range(5)]
    runs: dict[str, list[dict]] = {}
    for k, n in ((1, 1), (2, 3), (3, 5)):
        runs |= _gate_run("doc", k, [a], fresh[:n], [])
    _write_runs(tmp_path, runs)
    lines, pooled = gate.summarize(gate.collect(tmp_path))
    assert "doc-1: R=1 turn4-edits=1 proposed=1 shown-again=0 dropped=0 absent=1" in lines
    assert "doc-3: R=1 turn4-edits=5 proposed=5 shown-again=0 dropped=0 absent=1" in lines
    assert any(ln.startswith("== doc:") and "turn4-edits=9 (1 to 5 per run)" in ln for ln in lines)
    assert pooled["turn4"] == 9
    # three measurable runs are short of the ten the decision asks for: reported, and not a plain pass
    assert gate.main([str(tmp_path), "--min-measurable", "10"]) == 3
    assert gate.main([str(tmp_path), "--min-measurable", "3"]) == 0


def test_the_gate_threshold_is_inclusive_a_rate_exactly_at_ten_percent_passes(tmp_path: Path) -> None:
    gate = _gate()
    a = {"old": "really quite ", "new": ""}
    again = {"old": "The worker really quite simply retries", "new": "The worker simply retries"}
    nine = [{"old": f"keep {i}", "new": f"k {i}"} for i in range(9)]
    fresh = [{"old": "A fresh edit", "new": "Fresh"}]
    runs = _gate_run("doc", 1, [a, *nine], [again, *fresh], [])  # 1 of 10 rejected fixes shown again
    _write_runs(tmp_path, runs)
    assert gate.main([str(tmp_path), "--threshold", "0.1"]) == 0  # 10.0% against 10%: pass
    assert gate.main([str(tmp_path), "--threshold", "0.09"]) == 1
    over = tmp_path / "over"
    over.mkdir()
    _write_runs(over, _gate_run("doc", 1, [a, *nine[:8]], [again, *fresh], []))  # 1 of 9: 11.1%
    assert gate.main([str(over), "--threshold", "0.1"]) == 1


DOC_TEXT = "The worker really quite simply retries a failed job once. The scheduler just wakes every ten seconds.\n"


def test_same_spot_other_replacement_is_reported_and_never_counted_in_the_rate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _gate()
    a = {"old": "really quite ", "new": ""}
    spot = {"n": 1, "old": "really quite simply", "new": "just"}  # the same words, another replacement: not the filter's match
    elsewhere = {"n": 2, "old": "just wakes", "new": "wakes"}  # another place entirely
    _write_runs(tmp_path, _gate_run("doc", 1, [a], [spot, elsewhere], []))
    collected = gate.collect(tmp_path, {"doc": DOC_TEXT})
    row = collected["doc"][1]["rows"][0]
    assert row["status"] == "absent" and row["same_spot"] is True
    lines, pooled = gate.summarize(collected)
    assert pooled["same_spot"] == 1 and pooled["shown"] == 0
    assert any(ln.startswith("== doc:") and "same-spot-other=1" in ln and "rate=0.0%" in ln for ln in lines)
    assert "same spot, other replacement: 'really quite ' -> '' by edit 1" in "\n".join(lines)
    # from the command line: --source LABEL=PATH, or LABEL-source.txt beside the transcripts
    doc = tmp_path / "the-doc.md"
    doc.write_text(DOC_TEXT)
    assert gate.main([str(tmp_path), "--source", f"doc={doc}"]) == 0
    assert "same-spot-other=1" in capsys.readouterr().out
    (tmp_path / "doc-source.txt").write_text(DOC_TEXT)
    assert gate.main([str(tmp_path)]) == 0
    assert "same-spot-other=1" in capsys.readouterr().out
    assert gate.main([str(tmp_path), "--source", f"doc={tmp_path / 'missing.md'}"]) == 2
    # the same change is the filter's business, another place is no match, and an unlocated edit is unknown
    same = {"old": "The worker really quite simply retries", "new": "The worker simply retries"}
    assert gate.same_spot_other(a, same, DOC_TEXT) is False
    assert gate.same_spot_other(a, elsewhere, DOC_TEXT) is False
    assert gate.same_spot_other(a, {"old": "not in the document", "new": "x"}, DOC_TEXT) is None


def test_the_gate_script_keeps_a_copy_of_the_source_for_the_same_spot_column() -> None:
    sh = (ACC / "run-memory-gate.sh").read_text()
    assert 'cp "$SOURCE" "$OUT/$LABEL-source.txt"' in sh  # the command, not a comment that names the file
    assert 'run "$LABEL-$k-2" "$ANSWER"' in sh and "Accept none. Reject all the edits." in sh


def test_a3_is_not_measurable_when_the_fresh_run_proposed_nothing_at_all() -> None:
    rv = _review_verdicts()
    nothing = {"edits": [], "dropped": []}  # an editor with nothing left to say proves nothing about memory
    row = {n: (v, note) for n, v, note in rv.check_a(_check_a_runs([2], nothing))}["a3"]
    assert row[0] == "NOT-MEASURABLE" and "proposed no edits" in row[1]
    # an edit the filter held back is a proposal: the check ran
    held = {"edits": [], "dropped": [{"n": 2, "old": "really quite ", "new": "", "cause": "rejected"}]}
    assert {n: v for n, v, _ in rv.check_a(_check_a_runs([2], held))}["a3"] == "PASS"
    # and the note reports how many edits the fresh run showed
    shown = {"edits": [{"n": 1, "old": "C", "new": "c"}], "dropped": []}
    assert "fresh-run edits shown=1" in {n: note for n, _, note in rv.check_a(_check_a_runs([2], shown))}["a3"]


def test_the_review_verdicts_print_not_measurable_as_its_own_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rv = _review_verdicts()
    _write_runs(tmp_path, _check_a_runs([2], {"edits": [], "dropped": []}))
    assert rv.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "a3  NOT-MEASURABLE" in out and "a3  PASS" not in out


def _b_runs(removed: dict, b3: dict) -> dict[str, list[dict]]:
    return {
        "b1": _script("memory", "rejections", "l", {"rejections": [{"n": 1, **removed}, {"n": 2, "old": "B", "new": ""}]}),
        "b2": _script("memory", "rejections", "d", {"rejections": [{"n": 1, "old": "B", "new": ""}]}),
        "b3": _script("brief", "filter", "f", b3),
        "b4": _script("review", "apply", "d", {"dry_run": True, "accept": []}),
        "b4c": _script("review", "apply", "p", {"applied": [], "rejected": [], "held": [1]}),
    }


def test_b3_counts_the_removed_fix_proposed_again_over_another_span_as_the_edit_back() -> None:
    rv = _review_verdicts()
    removed = {"old": "really quite ", "new": ""}
    again = {"edits": [{"n": 1, "old": "The worker really quite simply retries", "new": "The worker simply retries"}],
             "dropped": []}
    rows = {n: v for n, v, _ in rv.check_b(_b_runs(removed, again))}
    assert rows["b3"] == "PASS"  # the same change over another span: removal worked, the edit is back
    # still dropped by the filter, whatever span the editor used: the removal did not take
    dropped = {"edits": [], "dropped": [{"n": 1, "old": "The worker really quite simply retries",
                                          "new": "The worker simply retries", "cause": "rejected"}]}
    assert {n: v for n, v, _ in rv.check_b(_b_runs(removed, dropped))}["b3"] == "FAIL"
    # another fix of the same words is neither: the editor did not bring the removed one back
    other = {"edits": [{"n": 1, "old": "really quite ", "new": "very "}], "dropped": []}
    assert {n: v for n, v, _ in rv.check_b(_b_runs(removed, other))}["b3"] == "VACUOUS"


def test_held_edits_are_a_positive_control_reported_and_never_counted_in_the_rate(tmp_path: Path) -> None:
    gate = _gate()
    edits = [{"n": 1, "old": "really quite ", "new": ""}, {"n": 2, "old": "just ", "new": ""}]
    fresh = [{"n": 1, "old": "The scheduler just wakes", "new": "The scheduler wakes"}]  # the held fix comes back
    runs = {
        "sub-1-1": _script("brief", "filter", "f", {"edits": edits, "dropped": []}),
        "sub-1-2": _script("review", "apply", "d", {"dry_run": True}),
        "sub-1-3": _script("review", "apply", "p", {"applied": [], "rejected": [1], "held": [2], "rejections_stored": True}),
        "sub-1-4": _script("brief", "filter", "g", {"edits": fresh, "dropped": []}),
    }
    _write_runs(tmp_path, runs)
    lines, pooled = gate.summarize(gate.collect(tmp_path))
    assert pooled["held"] == 1 and pooled["held_again"] == 1 and pooled["shown"] == 0 and pooled["R"] == 1
    assert "sub-1: R=1 turn4-edits=1 proposed=1 shown-again=0 dropped=0 absent=1 held=1 held-shown-again=1" in lines
    assert gate.main([str(tmp_path)]) == 0


# ---------------------------------------------------------------------------
# Per-kind verdicts (critique nexus-ger02.6 S1): computed from the post-filter proposal
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
WORK_DIR = "/tmp/prose-edit-abcd1234"
BRIEF_ID = "a1b2c3d4e5f6"
FILTER_CMD = ("python3 .claude/skills/prose-edit/scripts/brief.py filter {target} --budget {budget} "
              f"--save {WORK_DIR}/filtered.json")


def _doc(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _res_err(tid: str, text: str) -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text,
                                                     "is_error": True}]}}


def _brief_read(*, brief_id: str = BRIEF_ID, path: str = f"{WORK_DIR}/brief.md", error: bool = False,
                text: str | None = None, sub: str | None = "ag") -> list[dict]:
    """The line-editor subagent's Read of WORK/brief.md and what came back: the file's last line is the id."""
    body = text if text is not None else f"     1\t# Editing brief\n   310\t\n   311\tBrief id: {brief_id}\n"
    return [_use("rd", "Read", {"file_path": path}, sub=sub), (_res_err if error else _res)("rd", body)]


def _run(reply: dict | None, filtered: dict | None, *, target: str = "docs/x.md", budget: int | str = 5,
         command: str | None = None, read: list[dict] | None = None, echo: str | None = BRIEF_ID) -> list[dict]:
    """A transcript: the editor reads WORK/brief.md and replies, then the skill's `brief.py filter` run and what
    it printed. `read` replaces the editor's Read (an empty list: it never read the file); `echo` is the id the
    reply names as brief_sha (None: it names none)."""
    events: list[dict] = []
    if reply is not None:
        reply = {**reply} if echo is None else {"brief_sha": echo, **reply}
        body = json.dumps(reply)
        events += [_use("ag", "Agent", {}), *(_brief_read() if read is None else read),
                   _res("ag", f"[Subagent hand-back] text\n```json\n{body}\n```")]
    if filtered is not None:
        cmd = command or FILTER_CMD.format(target=target, budget=budget)
        events += [_use("fl", "Bash", {"command": cmd}),
                   _res("fl", json.dumps({"dropped": [], "queries": [], "paragraphs": [], **filtered}, indent=1))]
    return events


def _verdict(kind: str, events: list[dict], doc: str | None) -> tuple[str, str]:
    v = _verdicts()
    return v.verdict(kind, events, v.compliance(events, doc), [], doc_text=doc)


def _ed(n: int, old: str, new: str = "") -> dict:
    return {"n": n, "old": old, "new": new, "reason": "r"}


def _both(edits: list[dict], **kw: Any) -> list[dict]:
    """A run where the editor replied with `edits` and the filter kept all of them."""
    return _run({"edits": edits, "queries": []}, {"edits": edits, **kw})


PROTECTED = _doc("tests/prose_edit/fixtures/protected.md")
QA = _doc("tests/prose_edit/fixtures/qualifiers-a.md")
QB = _doc("tests/prose_edit/fixtures/qualifiers-b.md")
QC = _doc("tests/prose_edit/fixtures/qualifiers-c.md")
REFRAIN = _doc("tests/prose_edit/fixtures/refrain-no-twin.md")
LINDA = _doc("docs/exploration/linda-in-nexus.md")
XANADU = _doc("docs/exploration/xanadu-in-nexus.md")


def test_protected_fails_on_an_edit_inside_a_quote_a_code_block_a_table_or_the_frontmatter() -> None:
    clean = _both([_ed(1, "The scheduler basically hands each job", "The scheduler hands each job")])
    assert _verdict("protected", clean, PROTECTED)[0] == "PASS"
    for planted in ("This is basically a very simple loop", "Basically, the original design note",
                    "A very basically important expiry column", "This very basically explains"):
        verdict, note = _verdict("protected", _both([_ed(1, planted, "x")]), PROTECTED)
        assert verdict == "FAIL" and "protected" in note, planted
    # one bad edit among good ones fails the run
    mixed = _both([_ed(1, "The scheduler basically hands each job", "x"), _ed(2, "This is basically a very simple loop")])
    assert _verdict("protected", mixed, PROTECTED)[0] == "FAIL"


def test_a_run_that_proposes_nothing_where_edits_are_expected_is_not_measurable_never_a_pass() -> None:
    empty = _both([])
    for kind, doc in (("protected", PROTECTED), ("budget", PROTECTED)):
        assert _verdict(kind, empty, doc)[0] == "NOT-MEASURABLE", kind
    # a qualifier fixture is different: Sam's ruling makes silence a violation (the filler is cut, every
    # other qualifier is queried), so an editor that proposed nothing failed, it did not go unmeasured
    verdict, note = _verdict("qa", empty, QA)
    assert verdict == "FAIL" and "basically" in note
    # no filter output at all (the run stopped before the skill filtered the reply)
    no_filter = _run({"edits": [_ed(1, "x")]}, None)
    assert _verdict("protected", no_filter, PROTECTED)[0] == "NOT-MEASURABLE"
    # no editor reply is a failure of the run, and the document missing is not a verdict either
    assert _verdict("protected", _run(None, {"edits": []}), PROTECTED)[0] == "FAIL"
    assert _verdict("protected", _both([_ed(1, "The scheduler basically hands each job")]), None)[0] == "NOT-MEASURABLE"
    # an edit whose old string is not in the document: the document is not the one the run edited
    stale = _both([_ed(1, "words the document never had")])
    verdict, note = _verdict("protected", stale, PROTECTED)
    assert verdict == "NOT-MEASURABLE" and "document" in note


def test_budget_fails_when_more_than_n_edits_survive_the_filter_and_is_not_measurable_without_n() -> None:
    three = [_ed(1, "The scheduler basically hands each job"), _ed(2, "It should be noted that the lease"),
             _ed(3, "The retry path is in fact identical")]
    over = _run({"edits": three}, {"edits": three}, budget=2)
    verdict, note = _verdict("budget", over, PROTECTED)
    assert verdict == "FAIL" and "3" in note and "2" in note
    assert _verdict("budget", _run({"edits": three}, {"edits": three[:2]}, budget=2), PROTECTED)[0] == "PASS"
    assert _verdict("budget", _run({"edits": three}, {"edits": three}, budget=3), PROTECTED)[0] == "PASS"
    nobudget = _run({"edits": three}, {"edits": three}, command="python3 .claude/skills/prose-edit/scripts/brief.py filter x")
    assert _verdict("budget", nobudget, PROTECTED)[0] == "NOT-MEASURABLE"
    # the budget binds every kind that names one, not only `budget`
    assert _verdict("qa", _run({"edits": three}, {"edits": three}, budget=2), PROTECTED)[0] == "FAIL"


RANGE_DOC = "".join(f"Line {i} has plain words in it.\n" for i in range(1, 11))


def test_range_fails_on_an_edit_or_a_query_outside_the_lines_and_passes_one_inside() -> None:
    target = "CHANGELOG.md:3-5"
    inside = _run({"edits": [_ed(1, "Line 4 has plain words", "Line 4 has words")]},
                  {"edits": [_ed(1, "Line 4 has plain words", "Line 4 has words")]}, target=target)
    assert _verdict("range", inside, RANGE_DOC)[0] == "PASS"
    out = [_ed(1, "Line 4 has plain words", "x"), _ed(2, "Line 8 has plain words", "x")]
    verdict, note = _verdict("range", _run({"edits": out}, {"edits": out}, target=target), RANGE_DOC)
    assert verdict == "FAIL" and "outside" in note
    query = _run({"edits": [_ed(1, "Line 4 has plain words", "x")]},
                 {"edits": [_ed(1, "Line 4 has plain words", "x")], "queries": [{"n": 1, "anchor": "Line 9 has", "text": "?"}]},
                 target=target)
    assert _verdict("range", query, RANGE_DOC)[0] == "FAIL"
    # no range in the filter command: nothing to check against
    assert _verdict("range", _run({"edits": out[:1]}, {"edits": out[:1]}, target="CHANGELOG.md"), RANGE_DOC)[0] == "NOT-MEASURABLE"
    assert _verdict("range", _run({"edits": []}, {"edits": []}, target=target), RANGE_DOC)[0] == "NOT-MEASURABLE"


QA_FILLER = _ed(1, "The queue is basically ordered", "The queue is ordered")
QA_MAY_QUERY = {"n": 1, "anchor": "the cache may return stale entries", "text": "Is that a measured bound?"}


def _qual(edits: list[dict], queries: list[dict]) -> list[dict]:
    """A run whose editor replied with `edits` and `queries` and whose filter kept all of them."""
    return _run({"edits": edits, "queries": queries}, {"edits": edits, "queries": queries})


def test_qa_cuts_the_filler_and_queries_the_may_and_fails_on_anything_else() -> None:
    assert _verdict("qa", _qual([QA_FILLER], [QA_MAY_QUERY]), QA)[0] == "PASS"
    # the filler is cut but may is neither cut nor queried: the ruling makes every other qualifier a query
    verdict, note = _verdict("qa", _qual([QA_FILLER], []), QA)
    assert verdict == "FAIL" and "may" in note and "queried" in note
    both = _qual([QA_FILLER, _ed(2, "the cache may return stale entries", "the cache returns stale entries")],
                 [QA_MAY_QUERY])
    verdict, note = _verdict("qa", both, QA)
    assert verdict == "FAIL" and "may" in note and "cuts" in note  # a cut of it fails even beside a query
    other = _qual([_ed(1, "A second paragraph exists so that the file has more than one block.", "A second paragraph exists.")],
                  [QA_MAY_QUERY])
    verdict, note = _verdict("qa", other, QA)
    assert verdict == "FAIL" and "basically" in note  # edits exist, the filler was left in
    query_only = _qual([], [{"n": 1, "anchor": "basically ordered", "text": "Is it ordered?"}, QA_MAY_QUERY])
    assert _verdict("qa", query_only, QA)[0] == "FAIL"  # the filler is cut, not queried (Sam, 2026-09-30)
    # the word must be in the query's anchor: a query anchored elsewhere whose prose says "may" is not about it
    by_text = _qual([QA_FILLER], [{"n": 1, "anchor": "stale entries", "text": "Does 'may' mean sometimes or always?"}])
    verdict, note = _verdict("qa", by_text, QA)
    assert verdict == "FAIL" and "may" in note and "queried" in note


def test_qb_queries_may_and_an_empty_proposal_or_a_cut_fails() -> None:
    ask = {"n": 1, "anchor": "Failover may take longer than 30 seconds", "text": "How much longer?"}
    assert _verdict("qb", _qual([], [ask]), QB)[0] == "PASS"
    cut = _both([_ed(1, "Failover may take longer than 30 seconds", "Failover takes longer than 30 seconds")])
    assert _verdict("qb", cut, QB)[0] == "FAIL"
    assert _verdict("qb", _qual([_ed(1, "Failover may take longer than 30 seconds", "x")], [ask]), QB)[0] == "FAIL"
    verdict, note = _verdict("qb", _both([]), QB)  # the old "nothing to cut is correct" class is gone
    assert verdict == "FAIL" and "may" in note and "queried" in note
    assert _verdict("qb", _run(None, {"edits": []}), QB)[0] == "FAIL"  # a run with no reply measured nothing
    other = _qual([_ed(1, "The lease TTL is 30 seconds.", "")], [])
    assert _verdict("qb", other, QB)[0] == "FAIL"  # edits elsewhere do not stand in for the query


def test_qc_queries_every_non_filler_qualifier_and_fails_a_cut_or_a_silence() -> None:
    likely = {"n": 1, "anchor": "will likely double", "text": "Is that a forecast?"}
    truly = {"n": 2, "anchor": "never truly reclaimed", "text": "What does truly add?"}
    assert _verdict("qc", _qual([], [likely, truly]), QC)[0] == "PASS"
    cut = _both([_ed(1, "Adoption will likely double next quarter.", "Adoption will double next quarter.")])
    verdict, note = _verdict("qc", cut, QC)
    assert verdict == "FAIL" and "likely" in note
    assert _verdict("qc", _both([_ed(1, "never truly reclaimed", "never reclaimed")]), QC)[0] == "FAIL"
    # one of the two queried, the other neither cut nor queried: still a failure
    verdict, note = _verdict("qc", _qual([], [likely]), QC)
    assert verdict == "FAIL" and "truly" in note and "queried" in note
    silent = _both([])
    verdict, note = _verdict("qc", silent, QC)
    assert verdict == "FAIL" and "queried" in note  # nothing was said about either qualifier: not "unmeasured"


def test_the_closing_refrain_is_never_an_edit_and_a_query_about_it_passes() -> None:
    planted = _both([_ed(1, "Not a log, but a promise.", "A promise.")])
    verdict, note = _verdict("refrain", planted, REFRAIN)
    assert verdict == "FAIL" and "device" in note
    asked = _run({"edits": []}, {"edits": [], "queries": [{"n": 1, "anchor": "Not a log, but a promise.", "text": "No exact twin found. Keep?"}]})
    assert _verdict("refrain", asked, REFRAIN)[0] == "PASS"
    assert _verdict("refrain", _both([]), REFRAIN)[0] == "NOT-MEASURABLE"
    paragraph = _run({"edits": []}, {"edits": [], "paragraphs": [{"n": 1, "action": "cut",
                     "paragraphs": 'the paragraph opening "That is what the ledger is for"', "advice": "restates"}]})
    assert _verdict("refrain", paragraph, REFRAIN)[0] == "FAIL"


ALWAYS_LINDA = ("Not a parallel programming model, but a coordination substrate",
                "`SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`", "A report is owed until a report tuple exists")
ALWAYS_XANADU = ("Not a hypertext system, but a linking substrate", "There is no way to say that a research finding")
CARD_LINDA = "Devices: the tricolon 'easier to build correctly, easier to analyze, and easier to compose'; 'small, well-studied, and already half-built'."
CARD_XANADU = "Devices: 'simple, well-studied, and easy to implement'; the list 'RDF triples, property graphs, or ad-hoc foreign keys'."


def _carded(edits: list[dict], card: str | None) -> list[dict]:
    """A run where the editor replied with `edits` and a voice card, and the filter kept all of them."""
    reply: dict = {"edits": edits, "queries": []}
    if card is not None:
        reply["voice_card"] = card
    return _run(reply, {"edits": edits})


def test_xanadu_and_linda_fail_an_edit_on_a_refrain_a_closing_line_or_the_unexplained_sql_card_or_no_card() -> None:
    plain = _both([_ed(1, "To be clear: ", "")])
    assert _verdict("linda", plain, LINDA)[0] == "PASS" and _verdict("xanadu", plain, XANADU)[0] == "PASS"
    for card in (None, "", CARD_LINDA):
        for planted in ALWAYS_LINDA:
            verdict, note = _verdict("linda", _carded([_ed(1, planted, "x")], card), LINDA)
            assert verdict == "FAIL" and "device" in note, (planted, card)
    for card in (None, CARD_XANADU):
        for planted in ALWAYS_XANADU:
            assert _verdict("xanadu", _carded([_ed(1, planted, "x")], card), XANADU)[0] == "FAIL", (planted, card)
    # cutting a whole paragraph that holds an always-protected device touches it too
    para = _run({"edits": []}, {"edits": [], "paragraphs": [{"n": 1, "action": "cut",
                "paragraphs": 'the paragraph opening "This is the role Linda fills in Nexus"', "advice": "x"}]})
    assert _verdict("linda", para, LINDA)[0] == "FAIL"
    assert _verdict("linda", _both([]), LINDA)[0] == "NOT-MEASURABLE"
    # a device list that no longer matches the document proves nothing, a card-conditional phrase included
    assert _verdict("linda", plain, LINDA.replace("small, well-studied, and already half-built", "x"))[0] == "NOT-MEASURABLE"
    assert _verdict("linda", plain, LINDA.replace("Not a parallel programming model, but a coordination substrate", "x"))[0] == "NOT-MEASURABLE"


def test_a_tricolon_edit_passes_when_the_voice_card_does_not_name_it_and_fails_when_it_does() -> None:
    cases = (
        ("linda", LINDA, CARD_LINDA, "easier to build correctly, easier to analyze, and easier to compose",
         "small, well-studied, and already half-built"),
        ("xanadu", XANADU, CARD_XANADU, "simple, well-studied, and easy to implement",
         "tracing where a decision came from, what code implements a design, and which findings have been superseded"),
    )
    for kind, doc, card, on_card, off_card in cases:
        verdict, note = _verdict(kind, _carded([_ed(1, off_card if kind == "xanadu" else "the oldest unclaimed tuple per subspace, the health of the table, and the age of the last sweep", "x")], card), doc)
        assert verdict == "PASS" and "card-conditional devices scored" in note, (kind, note)
        for no_card in (None, "", "First person plural. Refrain: the closing line of each section."):
            assert _verdict(kind, _carded([_ed(1, on_card, "x")], no_card), doc)[0] == "PASS", (kind, no_card)
        verdict, note = _verdict(kind, _carded([_ed(1, on_card, "x")], card), doc)
        assert verdict == "FAIL" and "device" in note and on_card[:20] in note, (kind, note)
    # a longer old string that merely contains a carded tricolon still touches it
    wide = _carded([_ed(1, "Linda's model provided all three in a form that was small, well-studied, and already half-built in our engine.", "x")], CARD_LINDA)
    assert _verdict("linda", wide, LINDA)[0] == "FAIL"
    # a curly-quoted, re-cased card still names it
    assert _verdict("linda", _carded([_ed(1, "small, well-studied, and already half-built", "x")], "DEVICE: \u2018Small, Well-Studied\u2019"), LINDA)[0] == "FAIL"
    # a paragraph proposal is scored against a carded tricolon only
    para = {"n": 1, "action": "split", "paragraphs": 'the paragraph opening "Three operations with exact meanings"', "advice": "x"}
    assert _verdict("linda", _run({"edits": [], "voice_card": CARD_LINDA}, {"edits": [], "paragraphs": [para]}), LINDA)[0] == "FAIL"
    assert _verdict("linda", _run({"edits": [], "voice_card": "none"}, {"edits": [], "paragraphs": [para]}), LINDA)[0] == "PASS"


def test_the_hash_pins_sentence_is_not_a_device() -> None:
    v = _verdicts()
    sentence = "The hash pins which chunk; the range pins where within it."
    assert XANADU.count(sentence) == 1
    assert sentence not in v.DEVICES["xanadu"] and sentence not in [p for p, _ in v.CARD_DEVICES["xanadu"]]
    assert _verdict("xanadu", _carded([_ed(1, sentence, "The hash pins which chunk, and the range pins where within it.")], CARD_XANADU), XANADU)[0] == "PASS"


def test_every_kind_still_fails_on_a_scope_or_denial_violation_and_an_unknown_kind_is_not_measurable() -> None:
    v = _verdicts()
    events = [*_both([_ed(1, "The scheduler basically hands each job")]),
              _use("g", "Grep", {"pattern": "x"}, sub="ag")]
    verdict, note = _verdict("protected", events, PROTECTED)
    assert verdict == "FAIL" and "scope" in note
    assert v.verdict("mystery", events, v.compliance(events, None), [], doc_text=None)[0] == "NOT-MEASURABLE"
    # the old fall-through: a reply plus clean counts must not be a pass by itself
    assert _verdict("xanadu", _run({"edits": []}, None), XANADU)[0] != "PASS"


def test_main_exits_one_on_a_fail_three_on_not_measurable_and_zero_only_when_every_run_passes(tmp_path: Path) -> None:
    v = _verdicts()

    def write(name: str, events: list[dict]) -> None:
        (tmp_path / f"{name}.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        (tmp_path / f"{name}.rc").write_text("rc=0\n", encoding="utf-8")

    root = tmp_path / "root"
    (root / "tests" / "prose_edit" / "fixtures").mkdir(parents=True)
    (root / "tests" / "prose_edit" / "fixtures" / "protected.md").write_text(PROTECTED, encoding="utf-8")
    write("protected-1", _both([_ed(1, "The scheduler basically hands each job", "x")]))
    assert v.main([str(tmp_path), "--root", str(root)]) == 0
    write("protected-2", _both([]))
    assert v.main([str(tmp_path), "--root", str(root)]) == 3
    write("protected-3", _both([_ed(1, "This is basically a very simple loop", "x")]))
    assert v.main([str(tmp_path), "--root", str(root)]) == 1


# ---------------------------------------------------------------------------
# Round 3: the editor read WORK/brief.md; the oracles are the fixtures', not the filter's; drops by cause
# ---------------------------------------------------------------------------


def test_a_run_whose_editor_never_read_brief_md_fails_and_so_does_every_way_the_read_can_be_empty() -> None:
    edits = [_ed(1, "The scheduler basically hands each job", "x")]
    assert _verdict("protected", _both(edits), PROTECTED)[0] == "PASS"
    cases: dict[str, list[dict]] = {
        "no Read at all": _run({"edits": edits}, {"edits": edits}, read=[]),
        "the Read errored": _run({"edits": edits}, {"edits": edits}, read=_brief_read(error=True, text="no such file")),
        "the Read was denied": _run({"edits": edits}, {"edits": edits},
                                    read=_brief_read(text="Permission to use Read has been denied.")),
        "a result with no id line": _run({"edits": edits}, {"edits": edits}, read=_brief_read(text="1\t# Editing brief\n")),
        "another file": _run({"edits": edits}, {"edits": edits}, read=_brief_read(path=f"{WORK_DIR}/input.txt")),
        "the parent read it, not the editor": _run({"edits": edits}, {"edits": edits}, read=_brief_read(sub=None)),
        "another work directory": _run({"edits": edits}, {"edits": edits},
                                       read=_brief_read(path="/tmp/prose-edit-ffffffff/brief.md")),
    }
    for why, events in cases.items():
        verdict, note = _verdict("protected", events, PROTECTED)
        assert verdict == "FAIL" and "brief.md" in note, (why, verdict, note)


def test_the_id_the_reply_names_must_be_the_one_the_read_returned_and_the_read_must_come_before_the_reply() -> None:
    edits = [_ed(1, "The scheduler basically hands each job", "x")]
    copied = _run({"edits": edits}, {"edits": edits}, echo="0" * 12)  # an id the file never showed
    verdict, note = _verdict("protected", copied, PROTECTED)
    assert verdict == "FAIL" and "brief_sha" in note
    none = _run({"edits": edits}, {"edits": edits}, echo=None)
    verdict, note = _verdict("protected", none, PROTECTED)
    assert verdict == "FAIL" and "brief_sha" in note
    longer = _run({"edits": edits}, {"edits": edits}, echo=BRIEF_ID + "0123456789")  # the full hash starts with it
    assert _verdict("protected", longer, PROTECTED)[0] == "PASS"
    # a Read that comes after the reply cannot have produced it
    late = _run({"edits": edits}, {"edits": edits}, read=[])
    reply_at = next(i for i, ev in enumerate(late) if ev.get("type") == "user")
    late[reply_at + 1:reply_at + 1] = _brief_read()
    verdict, note = _verdict("protected", late, PROTECTED)
    assert verdict == "FAIL" and "before" in note


def test_the_stdin_run_needs_the_same_read_proof() -> None:
    v = _verdicts()
    brief = "python3 .claude/skills/prose-edit/scripts/brief.py"
    review = "python3 .claude/skills/prose-edit/scripts/review.py"
    reply = json.dumps({"brief_sha": BRIEF_ID, "edits": []})

    def stdin(read: list[dict]) -> list[dict]:
        return [_use("a", "Agent", {}), *read, _res("a", f"```json\n{reply}\n```"),
                _use("f", "Bash", {"command": f"{brief} filter - --file {WORK_DIR}/input.txt"}),
                _res("f", '{"edits": [], "dropped": []}'),
                _use("r", "Bash", {"command": f"{review} render -"}), _res("r", '{"copy": "x", "opened": false}')]

    for read, expected in ((_brief_read(), "PASS"), ([], "FAIL")):
        events = stdin(read)
        got = v.verdict("stdin", events, v.compliance(events, None), [f"{WORK_DIR}"])[0]
        assert got == expected, (read, got)


def test_the_verdicts_do_not_borrow_the_filters_protected_or_range_code() -> None:
    src = (ACC / "verdicts.py").read_text(encoding="utf-8")
    for name in ("protected_spans", "edit_problem", "_range_span", "_quoted_phrases", "spec_from_file_location"):
        assert name not in src, f"verdicts.py uses brief.py's {name}: the oracle would fail with the filter"


def test_protected_is_checked_against_the_fixtures_own_line_ranges_and_a_changed_fixture_is_not_measurable() -> None:
    row = _both([_ed(1, "The worker that basically holds the row.", "x")])  # a table row, line 22
    verdict, note = _verdict("protected", row, PROTECTED)
    assert verdict == "FAIL" and "line 22" in note and "table" in note
    changed = PROTECTED.replace("| Column | Meaning |", "| Col | Meaning |")
    verdict, note = _verdict("protected", _both([_ed(1, "The scheduler basically hands each job", "x")]), changed)
    assert verdict == "NOT-MEASURABLE" and "fixture" in note


def test_the_note_reports_the_filters_drops_by_cause_and_the_editors_own_violations() -> None:
    kept = _ed(1, "The scheduler basically hands each job", "x")
    dropped = [{"n": 2, "old": "This is basically a very simple loop", "new": "x", "cause": "protected-region"},
               {"n": 3, "old": "words the document never had", "new": "y", "cause": "over-budget"},
               {"n": 4, "old": "nor these either", "new": "", "cause": "protected-region"}]
    events = _run({"edits": [kept, *dropped]}, {"edits": [kept], "dropped": dropped,
                  "dropped_queries": [{"n": 1, "anchor": "a", "cause": "outside-range"}]})
    verdict, note = _verdict("protected", events, PROTECTED)
    assert verdict == "PASS"
    assert "dropped by the filter: outside-range=1, over-budget=1, protected-region=2" in note
    # the editor's own reply, scored against the same fixture ranges (the filter had nothing to do with it)
    assert "editor's own edits in protected regions: 1" in note
    clean = _verdict("protected", _both([kept]), PROTECTED)[1]
    assert "dropped by the filter: none" in clean and "editor's own edits in protected regions: 0" in clean


CHANGELOG_FIXTURE = _doc("tests/prose_edit/fixtures/range-notes.md")


def test_the_range_run_scores_against_a_committed_fixture_not_the_live_changelog() -> None:
    v = _verdicts()
    assert v.DOCS["range"] == "tests/prose_edit/fixtures/range-notes.md"
    assert (REPO_ROOT / v.DOCS["range"]).is_file()
    target = "tests/prose_edit/fixtures/range-notes.md:10-20"
    inside = [_ed(1, "The compactor just runs once a day", "The compactor runs once a day")]
    assert _verdict("range", _run({"edits": inside}, {"edits": inside}, target=target), CHANGELOG_FIXTURE)[0] == "PASS"
    outside = [*inside, _ed(2, "The scheduler now basically hands each job to one worker", "x")]  # line 7
    verdict, note = _verdict("range", _run({"edits": outside}, {"edits": outside}, target=target), CHANGELOG_FIXTURE)
    assert verdict == "FAIL" and "outside lines 10-20" in note and "edit 2" in note
    query = {"n": 1, "anchor": "The queue is really quite ordered", "text": "?"}  # line 26
    events = _run({"edits": inside}, {"edits": inside, "queries": [query]}, target=target)
    assert _verdict("range", events, CHANGELOG_FIXTURE)[0] == "FAIL"
    # the boundary lines are inside: an edit on line 10 and one on line 20
    edge = [_ed(1, "## [2.3.0] - 2026-02-10", "x"), _ed(2, "A worker that basically dies mid-job", "x")]
    assert _verdict("range", _run({"edits": edge}, {"edits": edge}, target=target), CHANGELOG_FIXTURE)[0] == "PASS"
    assert _verdict("range", _run({"edits": edge}, {"edits": edge}, target="x.md:11-19"), CHANGELOG_FIXTURE)[0] == "FAIL"


NEW_XANADU_ALWAYS = (
    "There is no way to say that a research finding",
    "There is no way to follow a chain of citations",
    "A debugger agent creates `relates` links between a root cause analysis and prior findings.",
    "A developer agent creates `implements` links between code and the design document it realizes.",
)
NEW_XANADU_CARD = (
    "tracing where a decision came from, what code implements a design, and which findings have been superseded",
    "following citation chains, crossing collection boundaries, and scoping each step",
)
NEW_LINDA_ALWAYS = (
    "A message reaches exactly one reader, whether or not two raced for it.",
    "A request stays open, with an age you can see, until its ack exists, whether or not anyone is watching.",
    "An agent's report had to be something an orchestrator could wait for and something a later session could count.",
    "A message to an agent working mid-turn had to reach it before it composed its next reply.",
    "A request from one Claude Code session to another on the same machine had to be delivered without a person relaying it.",
    "Nothing records that a report was owed, so an agent that finishes without reporting leaves no trace.",
    "Nothing wakes a reader when a record arrives, so a correction sent mid-turn lands after the turn.",
)
NEW_LINDA_CARD = (
    "the oldest unclaimed tuple per subspace, the health of the table, and the age of the last sweep",
)


def test_the_device_lists_hold_the_repeated_openings_and_the_card_conditional_tricolons_each_once() -> None:
    v = _verdicts()
    for kind, doc, always, conditional in (("xanadu", XANADU, NEW_XANADU_ALWAYS, NEW_XANADU_CARD),
                                           ("linda", LINDA, NEW_LINDA_ALWAYS, NEW_LINDA_CARD)):
        carded = {p: m for p, m in v.CARD_DEVICES[kind]}
        for phrase in always:
            assert phrase in v.DEVICES[kind] and phrase not in carded, (kind, phrase)
            verdict, note = _verdict(kind, _both([_ed(1, phrase, "x")]), doc)
            assert verdict == "FAIL" and "device" in note, (kind, phrase)
        for phrase in conditional:
            assert phrase in carded and phrase not in v.DEVICES[kind], (kind, phrase)
            assert any(m.lower() in phrase.lower() for m in carded[phrase]), (kind, phrase)  # a marker is in the phrase
            reply_card = " ".join(carded[phrase])
            verdict, note = _verdict(kind, _carded([_ed(1, phrase, "x")], reply_card), doc)
            assert verdict == "FAIL" and "device" in note, (kind, phrase)
        for phrase in (*v.DEVICES[kind], *carded):
            assert doc.count(phrase) == 1, (kind, phrase, doc.count(phrase))  # the lists apply to the document
    assert _verdict("xanadu", _both([_ed(1, "To be clear: ", "")]), XANADU)[0] == "PASS"  # still passes on a plain cut


def test_the_readme_says_what_a_pass_means_for_devices_and_what_each_kind_scores() -> None:
    text = (ACC / "README.md").read_text(encoding="utf-8")
    assert "none of the scored devices was touched" in text
    assert "`CARD_DEVICES`" in text and "voice_card" in text and "scored only when" in text  # the two groups
    assert "hash pins which chunk" in text and "not a device" in text
    assert "a human reads the rest" in text or "for a human to read" in text
    for kind in ("`protected`", "`budget`", "`range`", "`qa`", "`qb`", "`qc`"):
        assert any(ln.startswith(f"| {kind} |") for ln in text.splitlines()), kind
    assert "dropped by the filter" in text and "fixture" in text
    row = next(ln for ln in text.splitlines() if ln.startswith("| `qb` |"))
    assert "queried" in row and "passes" not in row  # the no-proposal-passes class is gone
