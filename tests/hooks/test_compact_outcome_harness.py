# SPDX-License-Identifier: AGPL-3.0-or-later
"""The compaction-outcome gate's own machinery (tests/e2e/lib/compact_outcome.py), without `claude`.

The live part (a real session through a real automatic compaction) is tests/e2e/compact-outcome-gate.sh.
Here: the judge on recorded-shape events, the scratch plugin's hook wiring, the stub `nx` driving the
REAL compact_checkpoint.py (so the 'real' and 'bad' arms differ only in what the board returns), and the
fix arm's overlay. Each pin is chosen so that reverting the code it names fails it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._platform import IS_WINDOWS

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="the gate is POSIX-only (shebang stub nx, live claude)")

LIB = Path(__file__).resolve().parents[2] / "tests" / "e2e" / "lib"
CHECKPOINT = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "compact_checkpoint.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, LIB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


co = _load("compact_outcome")
overlay = _load("compact_outcome_fix_overlay")


def _boundary(trigger: str = "auto") -> dict:
    return {"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": trigger, "pre_tokens": 80000}}


def _call(name: str, **tool_input) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": tool_input}]}}


def _result(**kw) -> dict:
    return {"type": "result", "is_error": False, "stop_reason": "end_turn", **kw}


class TestJudge:
    def test_no_automatic_compaction_is_noise_not_a_pass_or_a_fail(self) -> None:
        assert co.judge([_call("Bash", command="git log alpha"), _result()])["verdict"] == "NOISE"
        assert co.judge([_boundary("manual"), _call("Bash", command="git log alpha"), _result()])["verdict"] == "NOISE"

    def test_one_item_and_end_fails(self) -> None:
        v = co.judge([_boundary(), _call("Edit", file_path="/r/app.py", new_string="def div(a, b):"), _result()])
        assert (v["verdict"], v["acted"], v["score"]) == ("FAIL", ["todo2"], 0.2)

    def test_all_items_pass_and_calls_before_the_boundary_do_not_count(self) -> None:
        before = [_call("Bash", command="git merge alpha beta README Usage")]
        after = [_boundary(), _call("Edit", new_string="def div(a, b)"), _call("Edit", new_string="## Usage"),
                 _call("Bash", command="git merge alpha"), _call("Bash", command="git merge beta"),
                 _call("TaskOutput", task_id="b1"), _result()]
        v = co.judge(before + after)
        assert (v["verdict"], v["score"]) == ("PASS", 1.0)
        assert co.judge(before + [_boundary(), _result()])["acted"] == []

    def test_the_last_automatic_boundary_opens_the_window_and_a_refusal_is_noise(self) -> None:
        events = [_boundary(), _call("Bash", command="git merge alpha"), _result(), _boundary(), _call("Bash", command="div"), _result()]
        assert co.judge(events)["acted"] == ["todo2"]
        assert co.judge([_boundary(), {"type": "result", "stop_reason": "refusal", "is_error": True}])["verdict"] == "NOISE"

    def test_min_score_is_a_parameter(self) -> None:
        events = [_boundary(), _call("Edit", new_string="def div(a, b)"), _call("Bash", command="git log alpha"), _result()]
        assert co.judge(events, 0.4)["verdict"] == "PASS" and co.judge(events, 0.8)["verdict"] == "FAIL"


class TestPlugin:
    def test_scratch_plugin_wires_only_the_compaction_hooks(self, tmp_path: Path) -> None:
        wired = co.build_plugin(str(tmp_path / "p"))
        assert wired == ["PostCompact:manual|auto", "PreCompact:manual|auto", "SessionStart:compact"]
        text = (tmp_path / "p" / "hooks" / "hooks.json").read_text()
        for stray in ("nx_launch", "upgrade-auto", "self-gc", "version_lockstep", "mailbox_drain", "auto-approve"):
            assert stray not in text

    def test_fix_arm_adds_the_overlay_beside_the_real_hooks(self, tmp_path: Path) -> None:
        wired = co.build_plugin(str(tmp_path / "p"), overlay=True)
        assert wired.count("PreCompact:manual|auto") == 2 and wired.count("SessionStart:compact") == 2
        assert (tmp_path / "p" / "hooks" / "scripts" / co.OVERLAY_SCRIPT).is_file()
        assert (tmp_path / "p" / "hooks" / "scripts" / "compact_checkpoint.py").read_bytes() == CHECKPOINT.read_bytes()


def _hook(mode: str, tmp_path: Path, stub_mode: str) -> subprocess.CompletedProcess:
    """Run the REAL compact_checkpoint.py with the harness's stub nx, as the scratch plugin does."""
    repo = tmp_path / "repo"
    if not repo.exists():
        co.seed_repo(str(repo))
        iso = tmp_path / "iso"
        gen = iso / "tools" / "gen-1" / "bin"
        gen.mkdir(parents=True)
        (gen / "nx").write_text(co.STUB_NX)
        (gen / "nx").chmod(0o755)
        (iso / "tools" / "current").symlink_to(iso / "tools" / "gen-1")
        (iso / "board.md").write_text(co.board_text("sid-1"))
    iso = tmp_path / "iso"
    env = {**os.environ, "NEXUS_CONFIG_DIR": str(iso / "cfg"), "NX_TOOLS_DIR": str(iso / "tools"),
           "NX_STUB_MODE": stub_mode, "NX_STUB_BOARD": str(iso / "board.md"), "CLAUDE_CONFIG_DIR": str(iso / "claude")}
    payload = {"session_id": "sid-1", "cwd": str(repo), "trigger": "auto", "hook_event_name": "PreCompact"}
    return subprocess.run([sys.executable, str(CHECKPOINT), mode], input=json.dumps(payload), env=env,
                          capture_output=True, text=True, timeout=60)


class TestStubDrivesTheRealHook:
    def test_full_stub_puts_the_board_in_the_checkpoint_and_the_injection(self, tmp_path: Path) -> None:
        assert _hook("pre", tmp_path, "full").returncode == 0
        ck = (tmp_path / "iso" / "cfg" / "compact-checkpoints" / "sid-1.md").read_text()
        assert "Close out the demo repo" in ck and "branch: main" in ck
        out = _hook("inject", tmp_path, "full")
        assert "Close out the demo repo" in json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_empty_stub_is_the_planted_bad_checkpoint(self, tmp_path: Path) -> None:
        assert _hook("pre", tmp_path, "empty").returncode == 0
        ck = (tmp_path / "iso" / "cfg" / "compact-checkpoints" / "sid-1.md").read_text()
        assert "Close out the demo repo" not in ck and "alpha" not in ck


class TestOverlay:
    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        co.seed_repo(str(repo))
        return repo

    def test_capture_finds_branches_todo_items_and_a_running_background_task(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        tr = tmp_path / "t.jsonl"
        tr.write_text(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content":
                       "Command running in background with ID: bx1. Output is being written to: /tmp/x/bx1.output."}]}}) + "\n")
        work = overlay.capture({"cwd": str(repo), "transcript_path": str(tr)})
        assert work["branches"] == ["alpha", "beta"] and len(work["todo"]) == 3
        assert work["tasks"] == [{"id": "bx1", "output": "/tmp/x/bx1.output"}]
        text = overlay.render(work)
        assert "bx1" in text and "alpha, beta" in text and text.rstrip().endswith(overlay.DIRECTIVE)

    def test_a_finished_task_and_an_empty_repo_state_render_nothing(self, tmp_path: Path) -> None:
        tr = tmp_path / "t.jsonl"
        tr.write_text(
            json.dumps({"type": "user", "message": {"content": "Command running in background with ID: b2. Output is being written to: /tmp/b2.output."}}) + "\n"
            + json.dumps({"type": "user", "message": {"content": "<task-notification>b2 completed</task-notification>"}}) + "\n")
        assert overlay.background_tasks(str(tr)) == []
        assert overlay.render({"tasks": [], "branches": [], "todo": [], "head_branch": "main"}) == ""
