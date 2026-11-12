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
import os
import shlex
import subprocess
import time
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


def test_only_a_repeat_of_the_previous_occurrence_is_dropped(crs) -> None:
    # `a b a` keeps all three: dropping the last `a` would change the effective value if a later --channels
    # replaces an earlier one's values; `a a b` loses only the second `a`
    args = _relaunch_args(crs, f"claude --channels plugin:other@m --channels {P} --channels plugin:other@m -n x")
    assert args == ["--channels", "plugin:other@m", "--channels", P, "--channels", "plugin:other@m",
                    "-n", "x", "--resume", "sid-1"]
    args = _relaunch_args(crs, f"claude --channels {P} --channels {P} --channels plugin:other@m -n x")
    assert args == ["--channels", P, "--channels", "plugin:other@m", "-n", "x", "--resume", "sid-1"]


def test_a_repeated_dev_channel_flag_relaunches_as_one(crs, monkeypatch) -> None:
    monkeypatch.setattr(crs, "KEEP_DEV_CHANNEL", True)  # no swap to the plugin form: the dev flag itself is kept
    args = _relaunch_args(crs, f"claude {DEV} server:nexus {DEV} server:nexus -n x")
    assert args == [DEV, "server:nexus", "-n", "x", "--resume", "sid-1"]


def test_the_other_flags_and_the_first_prompt_are_untouched(crs) -> None:
    args = _relaunch_args(crs, f"claude --model opus --channels {P} --channels {P} -n x -- cat /tmp/h.md")
    assert args == ["--model", "opus", "--channels", P, "-n", "x", "--resume", "sid-1"]


# ── the wake ─────────────────────────────────────────────────────────────────────────────


class Box:
    """A resumed session's registry file, and fakes for the mail, the withdraw, the pane and the typed prompt."""

    def __init__(self, crs, monkeypatch, tmp_path: Path, *, channel: bool, status: str = "idle", draft: bool = False,
                 mail: str = "sent", mail_starts_turn: bool = True, withdraw: str = "withdrawn") -> None:
        self.file = tmp_path / "sessions" / "999.json"
        self.file.parent.mkdir(exist_ok=True)
        self.set_status(status)
        self.mailed: list[str] = []
        self.typed: list[str] = []
        self.withdrawn: list[tuple] = []
        argv = f"/x/claude --channels {P} -n w --resume sid-new" if channel else "/x/claude -n w --resume sid-new"
        self.s = {"sessionId": "sid-new", "name": "w", "pid": 999, "pane": "%9", "file": self.file, "argv": argv}

        def fake_mail(_s: dict, text: str) -> tuple[str, str, Path | None]:
            self.mailed.append(text)
            if mail_starts_turn and mail != "failed":
                self.set_status("busy")
            return mail, ("tid-1" if mail == "sent" else ""), None

        def fake_submit(_pane: str, line: str, _f) -> bool:
            self.typed.append(line)
            self.set_status("busy")
            return True

        monkeypatch.setattr(crs, "_mail_wake", fake_mail)
        monkeypatch.setattr(crs, "submit", fake_submit)
        monkeypatch.setattr(crs, "pane_has_draft", lambda _p: draft)
        monkeypatch.setattr(crs, "withdraw_ask", lambda *a: self.withdrawn.append(a) or withdraw)
        monkeypatch.setattr(crs, "plugin_versions", lambda: {"conexus@nexus-plugins": "8.2.0"})
        monkeypatch.setattr(crs, "WAKE_DIR", tmp_path / "restart-wake")
        monkeypatch.setattr(crs, "UNWITHDRAWN", [])
        monkeypatch.setattr(crs, "WAKE_IDLE_WAIT", 0)
        monkeypatch.setattr(crs, "WAKE_BUSY_WAIT", 0)
        self.tmp = tmp_path

    def set_status(self, status: str) -> None:
        self.file.write_text(json.dumps({"sessionId": "sid-new", "pid": 999, "status": status}), encoding="utf-8")

    def typed_text(self) -> str:
        """What the typed pointer points at: the line is short, the text is in the file it names."""
        assert self.typed == [f"Read {self.tmp / 'restart-wake' / 'rw-sid-new.txt'}"]
        return (self.tmp / "restart-wake" / "rw-sid-new.txt").read_text(encoding="utf-8")


def test_the_wake_text_names_the_version_the_script_and_the_board(crs, monkeypatch) -> None:
    monkeypatch.setattr(crs, "plugin_versions", lambda: {"conexus@nexus-plugins": "8.2.0"})
    text = crs.wake_text()
    assert text.startswith("You were restarted onto conexus 8.2.0 at ") and "by claude-restart-sessions" in text
    assert "Re-read your board entry and resume your open threads." in text


def test_a_channel_session_is_woken_by_mail_and_nothing_is_typed(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=True)
    assert crs.wake(box.s) == "mailed"
    assert len(box.mailed) == 1 and box.typed == [] and box.withdrawn == []


def test_a_mailed_wake_that_starts_no_turn_is_withdrawn_and_then_typed(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=True, mail_starts_turn=False)
    assert crs.wake(box.s) == "typed"
    assert box.withdrawn == [("sid-new", "tid-1", "wake")]
    assert box.typed_text().startswith("You were restarted onto conexus 8.2.0")


@pytest.mark.parametrize("outcome", ["already taken or expired", "not withdrawn (in exited 1: boom)"])
def test_a_wake_mail_that_was_not_withdrawn_is_never_typed_over(crs, monkeypatch, tmp_path, outcome) -> None:
    # taken late, or still live: typing would wake the session twice
    box = Box(crs, monkeypatch, tmp_path, channel=True, mail_starts_turn=False, withdraw=outcome)
    assert crs.wake(box.s) == f"mailed, {outcome}"
    assert box.typed == []
    assert bool(crs.UNWITHDRAWN) is (outcome not in crs.GONE)   # a live one is named in the footprint


def test_a_wake_mail_that_may_have_landed_is_never_typed_over(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=True, mail="maybe", mail_starts_turn=False)
    assert crs.wake(box.s) == "not woken: mail may have landed"
    assert box.typed == [] and box.withdrawn == []


def test_a_wake_that_could_not_be_mailed_at_all_is_typed(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=True, mail="failed")
    assert crs.wake(box.s) == "typed" and box.withdrawn == []
    assert "board entry" in box.typed_text()


def test_a_session_without_the_channel_gets_the_wake_typed(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=False)
    assert crs.wake(box.s) == "typed"
    assert box.mailed == [] and "board entry" in box.typed_text()


@pytest.mark.parametrize("channel", [False, True])
def test_nothing_is_typed_over_an_unsent_draft(crs, monkeypatch, tmp_path, channel) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=channel, draft=True, mail_starts_turn=False)
    assert crs.wake(box.s) == "not woken: unsent text in its prompt"
    assert box.typed == []


def test_a_session_that_is_busy_already_is_left_alone_at_once(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=True, status="busy")
    monkeypatch.setattr(crs, "WAKE_IDLE_WAIT", 30)
    setattr_in(monkeypatch, crs, "time.sleep", lambda _s: pytest.fail("waited on a busy session"))
    assert crs.wake(box.s) == "not woken: busy already"
    assert box.mailed == [] and box.typed == []


def test_no_wake_leaves_the_session_idle(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=True)
    monkeypatch.setattr(crs, "NO_WAKE", True)
    assert crs.wake(box.s) == "not woken (--no-wake)"
    assert box.mailed == [] and box.typed == []


def test_a_failing_wake_never_stops_the_roll(crs, monkeypatch) -> None:
    calls: list[str] = []

    def boom(r: dict) -> str:
        calls.append(r["name"])
        raise KeyError("odd installed_plugins.json")

    monkeypatch.setattr(crs, "wake", boom)
    monkeypatch.setattr(crs, "TO_WAKE", [{"name": "a", "sessionId": "1"}, {"name": "b", "sessionId": "2"}])
    crs.wake_resumed()
    assert calls == ["a", "b"] and crs.TO_WAKE == []


def test_the_wake_mail_carries_the_origin_note_and_marker_and_leaves_the_handoff_bookkeeping(
        crs, monkeypatch, tmp_path) -> None:
    sent: list[list[str]] = []

    def fake_run(argv, **_kw):
        sent.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "tid-wake\n", "")

    setattr_in(monkeypatch, crs, "subprocess.run", fake_run)
    monkeypatch.setattr(crs, "_nx_binary", lambda: "nx")
    monkeypatch.setattr(crs, "SENT_ASKS", {"sid-new": "tid-handoff"})
    monkeypatch.setattr(crs, "SENT_MARKERS", {})
    monkeypatch.setattr(crs, "MADE_MARKERS", set())
    monkeypatch.setattr(crs, "ASK_MARKERS", tmp_path / "restart-asks")
    s = {"sessionId": "sid-new-0000", "name": "w", "pane": "%9"}
    monkeypatch.setattr(crs, "SENT_ASKS", {"sid-new-0000": "tid-handoff"})
    outcome, tid, marker = crs._mail_wake(s, "WAKE TEXT")
    assert (outcome, tid) == ("sent", "tid-wake") and marker is not None and marker.is_file()
    argv = sent[0]
    body = argv[argv.index("--body") + 1]
    assert body.startswith("This message comes from claude-restart-sessions") and body.endswith("WAKE TEXT")
    assert str(marker) in body and "kind=restart-wake" in argv and "to=sid-new-0000" in argv
    assert argv[argv.index("--ttl-seconds") + 1] == str(crs.WAKE_TTL)
    assert crs.SENT_ASKS == {"sid-new-0000": "tid-handoff"}      # the handoff ask's entry is untouched


def test_no_wake_is_parsed_and_carried_to_the_rerun_command(crs, monkeypatch) -> None:
    monkeypatch.setattr(crs, "sessions", lambda: [])
    monkeypatch.setattr("sys.argv", ["claude-restart-sessions", "--no-wake", "--dry-run", "--resume"])
    crs.main()
    assert crs.NO_WAKE is True
    a = argparse.Namespace(fresh=False, wait=600, handoff_timeout=900, fallback_resume=False, keep_dev_channel=False,
                           force=False, no_ready_check=False, no_wake=True, skip_sandbox_check=None, only=[], skip=[])
    assert "--no-wake" in shlex.split(crs.rerun_command(a, []))


# ── the two resume paths call it ─────────────────────────────────────────────────────────


def _restart_box(crs, monkeypatch, tmp_path) -> Path:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(crs, "SESSIONS", sessions_dir)
    monkeypatch.setattr(crs, "save_draft", lambda *_a: ("", ""))
    monkeypatch.setattr(crs, "pane_state", lambda _p: None)
    monkeypatch.setattr(crs, "TO_WAKE", [])
    monkeypatch.setattr(crs, "_ps_args", lambda pid: f"/x/claude --channels {P} -n w --resume sid-1 #{pid}")
    monkeypatch.setattr(crs, "wake", lambda _r: pytest.fail("woken inside restart(): it waits for the loop"))
    return sessions_dir


def test_restart_queues_the_session_it_resumed_for_one_wake_after_the_loop(crs, monkeypatch, tmp_path) -> None:
    sessions_dir = _restart_box(crs, monkeypatch, tmp_path)

    def fake_tmux(*a) -> None:
        if "-l" in a:  # the launch line is typed: the resumed session registers under its same id
            (sessions_dir / "999.json").write_text(json.dumps(
                {"sessionId": "sid-1", "pid": 999, "kind": "interactive", "tmux": "x:@1.%1"}), encoding="utf-8")

    monkeypatch.setattr(crs, "tmux", fake_tmux)
    monkeypatch.setattr(crs, "alive", lambda pid: pid == 999)
    s = {"sessionId": "sid-1", "name": "w", "pid": 111, "pane": "%1", "cwd": "/w", "argv": f"claude --channels {P}"}
    assert crs.restart(s) is True
    assert len(crs.TO_WAKE) == 1
    r = crs.TO_WAKE[0]
    assert r["pid"] == 999 and r["pane"] == "%1" and Path(r["file"]).name == "999.json"
    assert r["argv"].endswith("#999") and crs.has_channel(r)   # the RESUMED process's own line decides mail


def test_a_fresh_start_is_not_queued_for_a_wake(crs, monkeypatch, tmp_path) -> None:
    # it starts on `cat <handoff>`, which is its prompt
    sessions_dir = _restart_box(crs, monkeypatch, tmp_path)
    handoff = tmp_path / "h.md"
    handoff.write_text("# a\n# b\n", encoding="utf-8")

    def fake_tmux(*a) -> None:
        if "-l" in a:
            (sessions_dir / "new.json").write_text(json.dumps(
                {"sessionId": "new-sid", "pid": 999, "kind": "interactive", "tmux": "x:@1.%1"}), encoding="utf-8")

    monkeypatch.setattr(crs, "tmux", fake_tmux)
    monkeypatch.setattr(crs, "alive", lambda pid: pid == 999)
    monkeypatch.setattr(crs, "started_on", lambda *_a, **_kw: True)
    s = {"sessionId": "old-sid", "name": "w", "pid": 111, "pane": "%1", "cwd": "/w", "argv": f"claude --channels {P}"}
    assert crs.restart(s, str(handoff)) is True
    assert crs.TO_WAKE == []


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


def test_the_typed_line_in_production_is_short(crs) -> None:
    # long send-keys text arrives truncated (only the last 50-65 characters, ask_line): the line points at a file
    assert crs.WAKE_DIR == Path.home() / ".claude" / "restart-wake"
    assert len("Read ~/.claude/restart-wake/rw-0123abcd.txt") < 50


def test_a_typed_wake_file_lives_in_home_and_old_ones_are_pruned(crs, monkeypatch, tmp_path) -> None:
    box = Box(crs, monkeypatch, tmp_path, channel=False)
    stale = tmp_path / "restart-wake" / "rw-deadbeef.txt"
    stale.parent.mkdir()
    stale.write_text("old", encoding="utf-8")
    os.utime(stale, (time.time() - 2 * 86400,) * 2)
    fresh = tmp_path / "restart-wake" / "rw-cafef00d.txt"
    fresh.write_text("today", encoding="utf-8")
    assert crs.wake(box.s) == "typed"
    assert not stale.exists() and fresh.exists() and "board entry" in box.typed_text()
