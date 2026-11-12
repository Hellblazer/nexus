# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/claude-restart-sessions: a resumed session gets one wake prompt, and its relaunch line does not repeat
its channel flags (Sam's restart round, 2026-11-11).

A resumed session came back to an empty prompt and sat idle until Sam typed to it ("seems like a flaw"); and each
round added one more `--channels plugin:conexus@nexus-plugins` to the line (labrun_79e had three).
"""
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
DEV = "--dangerously-load-development-channels"


@pytest.fixture
def crs(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions_wake", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, "LOG", tmp_path / "restart.log")
    setattr_in(monkeypatch, module, "time.sleep", lambda _s: None)
    return module


def _relaunch_args(crs, argv: str) -> list[str]:
    s = {"argv": argv, "name": "x", "sessionId": "sid-1", "cwd": "/w"}
    return shlex.split(crs.relaunch_command(s).split(" && ", 1)[1])[1:]


# ── the relaunch line ────────────────────────────────────────────────────────────────────


def test_three_identical_channels_flags_relaunch_as_one(crs) -> None:
    args = _relaunch_args(crs, f"claude --channels {P} --channels {P} --channels {P} -n labrun_79e --resume old")
    assert args == ["--channels", P, "-n", "x", "--resume", "sid-1"]


def test_an_inline_repeat_of_the_same_values_is_dropped(crs) -> None:
    args = _relaunch_args(crs, f"claude --channels={P} --channels {P} -n x")
    assert args == [f"--channels={P}", "-n", "x", "--resume", "sid-1"]


def test_channels_flags_with_different_values_both_stay(crs) -> None:
    # a later --channels can replace the earlier one's values, so only an exact repeat goes
    args = _relaunch_args(crs, f"claude --channels plugin:other@m --channels {P} --channels plugin:other@m -n x")
    assert args == ["--channels", "plugin:other@m", "--channels", P, "-n", "x", "--resume", "sid-1"]


def test_a_repeated_dev_channel_flag_relaunches_as_one(crs, monkeypatch) -> None:
    monkeypatch.setattr(crs, "KEEP_DEV_CHANNEL", True)  # no swap to the plugin form: the dev flag itself is kept
    args = _relaunch_args(crs, f"claude {DEV} server:nexus {DEV} server:nexus -n x")
    assert args == [DEV, "server:nexus", "-n", "x", "--resume", "sid-1"]


def test_the_other_flags_and_the_first_prompt_are_untouched(crs) -> None:
    args = _relaunch_args(crs, f"claude --model opus --channels {P} --channels {P} -n x -- cat /tmp/h.md")
    assert args == ["--model", "opus", "--channels", P, "-n", "x", "--resume", "sid-1"]


# ── the wake ─────────────────────────────────────────────────────────────────────────────


class Box:
    """A resumed session's registry file, and fakes for the mail, the pane and the typed prompt."""

    def __init__(self, crs, monkeypatch, tmp_path: Path, *, channel: bool, status: str = "idle",
                 draft: bool = False, mail_starts_turn: bool = True, mail_ok: bool = True) -> None:
        self.file = tmp_path / "sessions" / "999.json"
        self.file.parent.mkdir(exist_ok=True)
        self.set_status(status)
        self.mailed: list[tuple[str, str]] = []
        self.typed: list[str] = []
        self.withdrawn: list[tuple] = []
        argv = f"/x/claude --channels {P} -n w --resume sid-new" if channel else "/x/claude -n w --resume sid-new"
        self.s = {"sessionId": "sid-new", "name": "w", "pid": 999, "pane": "%9", "file": self.file, "argv": argv}

        def mail(sid: str, text: str) -> str:
            self.mailed.append((sid, text))
            if not mail_ok:
                return ""
            if mail_starts_turn:
                self.set_status("busy")
            return "tid-1"

        def submit(pane: str, text: str, _f) -> bool:
            self.typed.append(text)
            self.set_status("busy")
            return True

        monkeypatch.setattr(crs, "_mail_wake", mail)
        monkeypatch.setattr(crs, "submit", submit)
        monkeypatch.setattr(crs, "pane_has_draft", lambda _p: draft)
        monkeypatch.setattr(crs, "withdraw_ask", lambda *a: self.withdrawn.append(a) or "withdrawn")
        monkeypatch.setattr(crs, "plugin_versions", lambda: {"conexus@nexus-plugins": "8.2.0"})
        monkeypatch.setattr(crs, "WAKE_IDLE_WAIT", 0)
        monkeypatch.setattr(crs, "WAKE_BUSY_WAIT", 0)

    def set_status(self, status: str) -> None:
        self.file.write_text(json.dumps({"sessionId": "sid-new", "pid": 999, "status": status}), encoding="utf-8")


def test_the_wake_text_names_the_version_the_script_and_the_board(crs, monkeypatch) -> None:
    monkeypatch.setattr(crs, "plugin_versions", lambda: {"conexus@nexus-plugins": "8.2.0"})
    text = crs.wake_text()
    assert text.startswith("You were restarted onto conexus 8.2.0 at ") and "by claude-restart-sessions" in text
    assert "Re-read your board entry and resume your open threads." in text


def test_a_channel_session_is_woken_by_mail_and_nothing_is_typed(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=True)
    assert crs.wake(box.s) == "mailed"
    assert [sid for sid, _ in box.mailed] == ["sid-new"] and box.typed == [] and box.withdrawn == []


def test_a_mailed_wake_that_starts_no_turn_is_withdrawn_and_typed_instead(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=True, mail_starts_turn=False)
    assert crs.wake(box.s) == "typed"
    assert box.withdrawn == [("sid-new", "tid-1", "wake")]   # no second wake left in the mailbox
    assert box.typed == [box.mailed[0][1]]


def test_a_wake_that_could_not_be_mailed_is_typed(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=True, mail_ok=False)
    assert crs.wake(box.s) == "typed" and box.withdrawn == [] and len(box.typed) == 1


def test_a_session_without_the_channel_gets_the_wake_typed(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=False)
    assert crs.wake(box.s) == "typed"
    assert box.mailed == [] and len(box.typed) == 1 and "board entry" in box.typed[0]


@pytest.mark.parametrize("channel", [False, True])
def test_nothing_is_typed_over_an_unsent_draft(crs, monkeypatch, tmp_path, channel) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=channel, draft=True, mail_starts_turn=False)
    assert crs.wake(box.s) == "not woken: unsent text in its prompt"
    assert box.typed == []


def test_a_session_that_is_busy_already_is_left_alone(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=True, status="busy")
    assert crs.wake(box.s) == "not woken: busy already"
    assert box.mailed == [] and box.typed == []


def test_no_wake_leaves_the_session_idle(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=True)
    monkeypatch.setattr(crs, "NO_WAKE", True)
    assert crs.wake(box.s) == "not woken (--no-wake)"
    assert box.mailed == [] and box.typed == []


def test_no_wake_is_parsed_and_carried_to_the_rerun_command(crs, monkeypatch) -> None:
    monkeypatch.setattr(crs, "sessions", lambda: [])
    monkeypatch.setattr("sys.argv", ["claude-restart-sessions", "--no-wake", "--dry-run", "--resume"])
    crs.main()
    assert crs.NO_WAKE is True
    a = argparse.Namespace(fresh=False, wait=600, handoff_timeout=900, fallback_resume=False, keep_dev_channel=False,
                           force=False, no_ready_check=False, no_wake=True, skip_sandbox_check=None, only=[], skip=[])
    assert "--no-wake" in shlex.split(crs.rerun_command(a, []))


# ── the two resume paths call it ─────────────────────────────────────────────────────────


def test_restart_wakes_the_session_it_resumed(crs, monkeypatch, tmp_path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(crs, "SESSIONS", sessions_dir)
    monkeypatch.setattr(crs, "save_draft", lambda *_a: ("", ""))
    monkeypatch.setattr(crs, "pane_state", lambda _p: None)

    def fake_tmux(*a) -> None:
        if "-l" in a:  # the launch line is typed: the resumed session registers under its same id
            (sessions_dir / "999.json").write_text(json.dumps(
                {"sessionId": "sid-1", "pid": 999, "kind": "interactive", "tmux": "x:@1.%1"}), encoding="utf-8")

    monkeypatch.setattr(crs, "tmux", fake_tmux)
    monkeypatch.setattr(crs, "alive", lambda pid: pid == 999)
    woken: list[dict] = []
    monkeypatch.setattr(crs, "wake", lambda r: woken.append(r) or "mailed")
    s = {"sessionId": "sid-1", "name": "w", "pid": 111, "pane": "%1", "cwd": "/w", "argv": f"claude --channels {P}"}
    assert crs.restart(s) is True
    assert len(woken) == 1
    r = woken[0]
    assert r["pid"] == 999 and r["pane"] == "%1" and Path(r["file"]).name == "999.json" and "argv" in r


def test_a_fresh_start_is_not_woken(crs, monkeypatch, tmp_path) -> None:
    # it starts on `cat <handoff>`, which is its prompt
    monkeypatch.setattr(crs, "wake", lambda _r: pytest.fail("a fresh start was woken"))
    monkeypatch.setattr(crs, "transcript_has", lambda *_a: True)
    monkeypatch.setattr(crs, "alive", lambda _pid: True)
    s = {"sessionId": "old", "pane": "%1"}
    d = {"sessionId": "new-sid", "pid": 5}
    crs.started_on("w", s, d, "/tmp/h.md")


def test_revive_wakes_each_session_it_resumed(crs, monkeypatch, tmp_path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(crs, "SESSIONS", sessions_dir)
    monkeypatch.setattr(crs, "boot_time", lambda: 1000.0)
    monkeypatch.setattr(crs, "revivable", lambda *_a: [
        {"name": "wren", "sessionId": "sid-1", "cwd": str(tmp_path), "mtime": 900.0}])

    def fake_tmux(*a) -> None:
        if "new-window" in a:
            (sessions_dir / "42.json").write_text(json.dumps(
                {"sessionId": "sid-1", "pid": 42, "tmux": "claude:@3.%7"}), encoding="utf-8")

    monkeypatch.setattr(crs, "tmux", fake_tmux)
    monkeypatch.setattr(crs, "alive", lambda pid: pid == 42)
    woken: list[dict] = []
    monkeypatch.setattr(crs, "wake", lambda r: woken.append(r) or "typed")
    a = argparse.Namespace(since=60, only=[], skip=[], flags="", dry_run=False, tmux_session="claude",
                           verify_timeout=5)
    assert crs.revive(a) == 0
    assert [(r["pid"], r["pane"]) for r in woken] == [(42, "%7")]
