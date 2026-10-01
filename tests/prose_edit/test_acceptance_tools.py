# SPDX-License-Identifier: AGPL-3.0-or-later
"""The acceptance runner and its verdict script (tests/prose_edit/acceptance), without a live session."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

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


def test_the_review_verdicts_call_a_rerun_that_never_proposed_the_rejected_edit_vacuous() -> None:
    rv = _review_verdicts()
    filt1 = {"edits": [{"n": 1, "old": "A"}, {"n": 2, "old": "B"}], "dropped": []}
    runs = {
        "a1": _script("brief", "filter", "f1", filt1) + _script("review", "render", "r1", {"counts": {"edits": 2}}),
        "a2": _script("review", "apply", "d2", {"dry_run": True, "accept": [{"n": 1}], "reject": [{"n": 2}]}),
        "a2c": _script("review", "apply", "p2", {"applied": [{"n": 1}], "rejected": [2], "rejections_stored": True}),
        "a3": _script("brief", "filter", "f3", {"edits": [{"n": 1, "old": "C"}], "dropped": []}),
        "a4": _script("review", "apply", "d4", {"dry_run": True, "accept": [], "hold": [{"n": 1}]}),
        "a4c": _script("review", "apply", "p4", {"applied": [], "rejected": [], "held": [1]}),
    }
    rows = {name: verdict for name, verdict, _ in rv.check_a(runs)}
    assert rows == {"a1": "PASS", "a2": "PASS", "a3": "VACUOUS", "a4": "PASS"}
    runs["a3"] = _script("brief", "filter", "f3", {"edits": [{"n": 1, "old": "B"}], "dropped": []})
    assert {n: v for n, v, _ in rv.check_a(runs)}["a3"] == "FAIL"  # a rejected edit shown again
    runs["a3"] = _script("brief", "filter", "f3", {"edits": [], "dropped": [{"n": 2, "old": "B", "cause": "rejected"}]})
    assert {n: v for n, v, _ in rv.check_a(runs)}["a3"] == "PASS"
    # an answer turn that applied for real, without the echo and the author's confirmation, is a failure
    runs["a2"] = _script("review", "apply", "p2", {"applied": [{"n": 1}], "rejected": [2], "rejections_stored": True})
    assert {n: v for n, v, _ in rv.check_a(runs)}["a2"] == "FAIL"


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
