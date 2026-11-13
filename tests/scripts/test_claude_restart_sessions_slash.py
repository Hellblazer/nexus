# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""claude-restart-sessions resumes a session through Claude Code's own /restart first, and exits and
relaunches it only where /restart is not taken. The behaviour these fakes stand for was measured in the
restart sandbox's opt-in ``slash`` scenario (tests/e2e/restart-sessions/slash_restart.py, 2.1.323)."""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import shlex
from pathlib import Path

import pytest

from tests._module_seam import setattr_in

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude-restart-sessions"
P = "plugin:conexus@nexus-plugins"


@pytest.fixture
def crs(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions_slash", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setenv("HOME", str(tmp_path))  # transcripts are read under ~/.claude/projects
    monkeypatch.setattr(module, "LOG", tmp_path / "restart.log")
    monkeypatch.setattr(module, "SESSIONS", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()
    monkeypatch.setattr(module, "TO_WAKE", [])
    monkeypatch.setattr(module, "RESULTS", {})
    monkeypatch.setattr(module, "NO_SLASH", False)
    monkeypatch.setattr(module, "save_draft", lambda *_a: ("", ""))
    monkeypatch.setattr(module, "pane_state", lambda _p: None)
    monkeypatch.setattr(module, "pane_has_draft", lambda _p: False)
    monkeypatch.setattr(module, "_ps_args", lambda pid: f"/x/claude --channels {P} -n w --resume sid-1 #{pid}")
    monkeypatch.setattr(module, "prompt_count", lambda _sid: 2)
    setattr_in(monkeypatch, module, "time.sleep", lambda _s: None)
    return module


def _session(crs, tmp_path, *, argv: str = f"/x/claude --model sonnet --channels {P} -n w") -> dict:
    path = tmp_path / "sessions" / "10.json"
    d = {"sessionId": "sid-1", "pid": 10, "tmux": "rs:@1.%3", "status": "idle", "startedAt": 1000, "name": "w",
         "kind": "interactive", "cwd": str(tmp_path)}
    path.write_text(json.dumps(d), encoding="utf-8")
    return {**d, "pane": "%3", "argv": argv, "file": path}


def _transcript(tmp_path, text: str) -> None:
    f = tmp_path / ".claude" / "projects" / "p" / "sid-1.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "system", "content": text}) + "\n")


def _log(crs) -> str:
    return crs.LOG.read_text(encoding="utf-8") if crs.LOG.exists() else ""


def test_a_resume_restarts_in_place_through_slash_restart(crs, monkeypatch, tmp_path) -> None:
    s = _session(crs, tmp_path)
    typed: list[tuple] = []

    def fake_tmux(*a) -> None:
        typed.append(a)
        if a[-1] == "Enter":  # the same process comes back with a later startedAt
            s["file"].write_text(json.dumps({**json.loads(s["file"].read_text()), "startedAt": 2000}),
                                 encoding="utf-8")

    monkeypatch.setattr(crs, "tmux", fake_tmux)
    monkeypatch.setattr(crs, "alive", lambda pid: pid == 10)
    assert crs.restart(s) is True
    assert ("send-keys", "-t", "%3", "-l", "/restart") in typed
    assert not any("C-c" in a for a in typed), "nothing is exited"
    assert not any("--resume" in " ".join(a) for a in typed), "no relaunch line is typed"
    assert "OK w: restarted in place by /restart, pid 10 (same)" in _log(crs)
    assert crs.RESULTS == {"sid-1": "resumed"}
    assert [w["sessionId"] for w in crs.TO_WAKE] == ["sid-1"]


def test_an_unknown_command_falls_through_to_exit_and_resume(crs, monkeypatch, tmp_path) -> None:
    s = _session(crs, tmp_path)
    state = {"exited": False}
    typed: list[tuple] = []

    def fake_tmux(*a) -> None:
        typed.append(a)
        if a[-1] == "Enter" and "/restart" in str(typed[-2]):
            _transcript(tmp_path, "Unknown command: /restart")
        elif a[-1] == "C-c":
            state["exited"] = True
        elif "-l" in a and "--resume" in a[-1]:
            (tmp_path / "sessions" / "11.json").write_text(json.dumps(
                {"sessionId": "sid-1", "pid": 11, "tmux": "rs:@1.%3", "status": "idle"}), encoding="utf-8")

    monkeypatch.setattr(crs, "tmux", fake_tmux)
    monkeypatch.setattr(crs, "alive", lambda pid: pid == 11 or (pid == 10 and not state["exited"]))
    assert crs.restart(s) is True
    log = _log(crs)
    assert 'w: /restart not taken (Claude Code answered "Unknown command: /restart"); exiting and resuming instead' in log
    assert "OK w: resumed as pid 11" in log
    relaunch = [a[-1] for a in typed if "-l" in a and "--resume" in a[-1]]
    assert relaunch and "--resume sid-1" in relaunch[0]


def test_a_session_that_never_comes_back_falls_through_after_the_wait(crs, monkeypatch, tmp_path) -> None:
    s = _session(crs, tmp_path)
    monkeypatch.setattr(crs, "SLASH_WAIT", 3)
    monkeypatch.setattr(crs, "tmux", lambda *a: None)
    monkeypatch.setattr(crs, "alive", lambda pid: True)
    assert crs.slash_restart(s) == (False, "it did not come back within 3s of /restart")


def test_a_busy_restart_is_not_counted_until_the_session_is_idle(crs, monkeypatch, tmp_path) -> None:
    s = _session(crs, tmp_path)
    monkeypatch.setattr(crs, "SLASH_WAIT", 3)

    def fake_tmux(*a) -> None:
        if a[-1] == "Enter":
            s["file"].write_text(json.dumps({**json.loads(s["file"].read_text()), "startedAt": 2000,
                                             "status": "busy"}), encoding="utf-8")

    monkeypatch.setattr(crs, "tmux", fake_tmux)
    monkeypatch.setattr(crs, "alive", lambda pid: True)
    assert crs.slash_restart(s)[0] is False


def test_a_dev_channel_session_is_left_to_the_exit_path(crs, monkeypatch, tmp_path) -> None:
    s = _session(crs, tmp_path, argv="/x/claude --dangerously-load-development-channels server:nexus -n w")
    typed: list[tuple] = []
    monkeypatch.setattr(crs, "tmux", lambda *a: typed.append(a))
    ok, why = crs.slash_restart(s)
    assert ok is False and "--dangerously-load-development-channels" in why
    assert typed == []


def test_no_slash_restart_never_types_it(crs, monkeypatch, tmp_path) -> None:
    s = _session(crs, tmp_path)
    typed: list[tuple] = []
    monkeypatch.setattr(crs, "tmux", lambda *a: typed.append(a))
    monkeypatch.setattr(crs, "NO_SLASH", True)
    assert crs.slash_restart(s) == (False, "--no-slash-restart")
    assert typed == []


def test_a_draft_is_cleared_with_one_ctrl_c_before_slash_restart(crs, monkeypatch, tmp_path) -> None:
    s = _session(crs, tmp_path)
    drafts = iter([True, False])
    monkeypatch.setattr(crs, "pane_has_draft", lambda _p: next(drafts))
    typed: list[tuple] = []

    def fake_tmux(*a) -> None:
        typed.append(a)
        if a[-1] == "Enter":
            s["file"].write_text(json.dumps({**json.loads(s["file"].read_text()), "startedAt": 2000}),
                                 encoding="utf-8")

    monkeypatch.setattr(crs, "tmux", fake_tmux)
    monkeypatch.setattr(crs, "alive", lambda pid: True)
    assert crs.slash_restart(s) == (True, "")
    assert typed[0] == ("send-keys", "-t", "%3", "C-c")
    assert [a for a in typed if "C-c" in a] == [typed[0]], "one Ctrl-C, not the two that exit"
    assert ("send-keys", "-t", "%3", "-l", "/restart") in typed


def test_a_draft_one_ctrl_c_does_not_clear_is_not_restarted_over(crs, monkeypatch, tmp_path) -> None:
    s = _session(crs, tmp_path)
    monkeypatch.setattr(crs, "pane_has_draft", lambda _p: True)
    typed: list[tuple] = []
    monkeypatch.setattr(crs, "tmux", lambda *a: typed.append(a))
    monkeypatch.setattr(crs, "alive", lambda pid: True)
    assert crs.slash_restart(s) == (False, "one Ctrl-C did not clear its draft")
    assert not any("/restart" in a for a in typed)


def test_a_fresh_restart_never_tries_slash_restart(crs, monkeypatch, tmp_path) -> None:
    s = _session(crs, tmp_path)
    monkeypatch.setattr(crs, "slash_restart", lambda _s: pytest.fail("/restart tried on a --fresh restart"))
    handoff = tmp_path / "handoff.md"
    handoff.write_text("h", encoding="utf-8")
    monkeypatch.setattr(crs, "tmux", lambda *a: None)
    monkeypatch.setattr(crs, "alive", lambda pid: False)
    monkeypatch.setattr(crs, "spare_window", lambda *a: "")
    with pytest.raises(crs.RelaunchStalled):
        crs.restart(s, str(handoff))


def test_rerun_command_carries_no_slash_restart(crs) -> None:
    a = argparse.Namespace(fresh=False, wait=600, handoff_timeout=600, fallback_resume=False, keep_dev_channel=False,
                           force=False, no_ready_check=False, no_wake=False, no_slash_restart=True,
                           skip_sandbox_check=None, only=[], skip=[])
    assert "--no-slash-restart" in shlex.split(crs.rerun_command(a, []))
