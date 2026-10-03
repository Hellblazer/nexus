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
             _use("a", "Agent", {}), _res("a", "ok"),
             _use("f", "Bash", {"command": f"{brief} filter -"}), _res("f", '{"edits": [], "dropped": []}'),
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
