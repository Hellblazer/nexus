# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/claude-restart-sessions: the draft check and the relaunch command (nexus-nuyju).

The pane lines are real ``tmux capture-pane -e`` captures from the 2026-11-01 sessions
whose dim suggested prompt made the 7.92.0 restart skip six of nine handoffs.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tests._module_seam import setattr_in

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude-restart-sessions"

RULE = "\x1b[38;5;244m" + "─" * 40 + "\x1b[39m"
FOOTER = "  \x1b[2m? for shortcuts\x1b[0m"


@pytest.fixture(scope="module")
def crs():
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def pane(prompt_line: str) -> str:
    return "\n".join(["some transcript text", RULE, prompt_line, RULE, FOOTER])


@pytest.mark.parametrize(
    ("prompt_line", "draft"),
    [
        ("\x1b[39m❯\xa0\x1b[2mgo Step 0\x1b[0m", False),  # elsinore: a suggested prompt
        ("\x1b[39m❯\xa0\x1b[2m`[no suggestion]`\x1b[0m", False),  # labrun_559
        ("\x1b[39m❯\xa0\x1b[2m! bash ~/.local/state/conexus/run/v0174/catalogue-wt.sh\x1b[0m", False),  # bob
        ("\x1b[39m❯\xa0", False),  # empty prompt
        ("\x1b[38;5;241m❯\xa0\x1b[39m", False),  # empty prompt, grey mark
        ("\x1b[39m❯\xa0fix the build", True),  # typed text, default colour
        ("\x1b[39m❯\xa0half a thought\x1b[7m \x1b[27m", True),  # typed text and the cursor cell
        ("\x1b[39m❯\xa0typed \x1b[2mand a ghost tail\x1b[0m", True),  # typed text with a dim completion
        ("\x1b[39m❯\xa0\x1b[2;38;5;245mdim with a colour\x1b[22m", False),  # dim set with a colour in one SGR
        ("\x1b[39m❯\xa0\x1b[38;5;2mtyped in colour 2", True),  # 256-colour index 2 is not dim
        ("\x1b[39m❯\xa0\x1b[38;2;2;2;2mtyped in rgb 2,2,2", True),  # truecolour 2s are not dim
    ],
    ids=["ghost-elsinore", "ghost-no-suggestion", "ghost-command", "empty", "empty-grey",
         "typed", "typed-cursor", "typed-plus-ghost", "dim-combined-sgr", "colour-index-2", "truecolour-2s"],
)
def test_has_draft_counts_only_text_that_is_not_dim(crs, prompt_line: str, draft: bool) -> None:
    assert crs.has_draft(pane(prompt_line)) is draft


def test_a_prompt_mark_outside_the_input_box_is_not_the_prompt(crs) -> None:
    """The footer's agent list can open with the same mark; only the line under a rule counts."""
    captured = "\n".join(["❯ an agent row with text", RULE, "\x1b[39m❯\xa0", RULE])
    assert crs.has_draft(captured) is False


def test_no_prompt_line_is_no_draft(crs) -> None:
    assert crs.has_draft("just output\nmore output") is False


@pytest.mark.parametrize(
    ("argv", "fresh", "want", "unwanted"),
    [
        ("claude --channels plugin:conexus@nexus-plugins -n old_name --resume abc", False,
         ["--channels", "-n", "tamsin", "--resume", "sid-1"], ["old_name", "abc"]),
        ("claude --name=old --continue --permission-mode auto", True,
         ["-n", "tamsin", "--permission-mode", "auto"], ["--resume", "--continue", "--name=old"]),
    ],
    ids=["resume-current-id-and-name", "fresh-drops-resume-keeps-mode"],
)
def test_relaunch_command(crs, argv: str, fresh: bool, want: list[str], unwanted: list[str]) -> None:
    cmd = crs.relaunch_command({"argv": argv, "name": "tamsin", "sessionId": "sid-1", "cwd": "/w d"}, fresh=fresh)
    assert cmd.startswith("cd '/w d' && ")
    args = shlex.split(cmd.split(" && ", 1)[1])[1:]
    for w in want:
        assert w in args, (w, args)
    for u in unwanted:
        assert u not in args, (u, args)


def test_claude_binary_honours_the_override_then_path(crs, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NX_RESTART_CLAUDE", "/opt/x/claude")
    assert crs._claude_binary() == "/opt/x/claude"
    monkeypatch.delenv("NX_RESTART_CLAUDE")
    monkeypatch.setenv("HOME", str(tmp_path))
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake.parent))
    assert crs._claude_binary() == str(fake)  # no ~/.local/bin/claude under this HOME


def test_an_empty_capture_counts_as_a_draft(crs) -> None:
    """tmux failed to read the pane: typing a handoff ask into it is the unsafe direction."""
    assert crs.has_draft("") is True and crs.has_draft("\n  \n") is True


def test_colon_form_sgr_is_parsed(crs) -> None:
    assert crs.has_draft(pane("\x1b[39m❯\xa0\x1b[38:5:2mtyped in colour 2")) is True
    assert crs.has_draft(pane("\x1b[39m❯\xa0\x1b[2mghost\x1b[0m")) is False


def _session_file(directory: Path, name: str, sid: str, pid: int, mtime: float) -> Path:
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"kind": "interactive", "tmux": "rs:@1.%5", "pid": pid, "sessionId": sid,
                                "name": name, "cwd": "/w", "status": "idle"}), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_sessions_skips_a_gone_pid_and_keeps_the_newest_of_a_duplicated_id(crs, monkeypatch, tmp_path) -> None:
    """The two non-draft SKIP kinds in the restart log: 'pid already gone' and 'also live as'."""
    monkeypatch.setattr(crs, "SESSIONS", tmp_path)
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    gone = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    me = os.getpid()
    _session_file(tmp_path, "old_pane", "sid-dup", me, 1000.0)
    _session_file(tmp_path, "new_pane", "sid-dup", me, 2000.0)
    _session_file(tmp_path, "ghost", "sid-gone", int(gone.stdout.strip()), 1500.0)
    got = crs.sessions()
    assert [s["name"] for s in got] == ["new_pane"]
    assert "is also live as new_pane; resuming it twice would fork it" in (tmp_path / "restart.log").read_text()


def test_pane_has_draft_reads_the_pane_with_its_escapes(crs, monkeypatch) -> None:
    """The fix is the -e: without it the dim suggestion arrives as plain text (nexus-nuyju)."""
    seen: list[list[str]] = []

    def fake_run(argv, **_kw):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=pane("\x1b[39m\u276f\xa0\x1b[2mgo Step 0\x1b[0m"), stderr="")

    setattr_in(monkeypatch, crs, "subprocess.run", fake_run)
    assert crs.pane_has_draft("%5") is False
    assert seen and seen[0][:2] == ["tmux", "capture-pane"] and "-e" in seen[0]


def test_an_empty_prompt_under_a_reverse_video_cursor_is_no_draft(crs) -> None:
    assert crs.has_draft(pane("\x1b[39m\u276f\xa0\x1b[7m \x1b[27m")) is False


def test_the_local_claude_wins_over_path(crs, monkeypatch, tmp_path) -> None:
    """The operator's box: ~/.local/bin/claude exists and is used even with another claude on PATH."""
    monkeypatch.delenv("NX_RESTART_CLAUDE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    local = tmp_path / ".local" / "bin" / "claude"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    other = tmp_path / "other" / "claude"
    other.parent.mkdir()
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    monkeypatch.setenv("PATH", str(other.parent))
    assert crs._claude_binary() == str(local)
