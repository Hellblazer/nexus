# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/claude-restart-sessions: the draft check and the relaunch command (nexus-nuyju).

The pane lines are real ``tmux capture-pane -e`` captures from the 2026-11-01 sessions
whose dim suggested prompt made the 7.92.0 restart skip six of nine handoffs.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import shlex
from pathlib import Path

import pytest

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
    ],
    ids=["ghost-elsinore", "ghost-no-suggestion", "ghost-command", "empty", "empty-grey",
         "typed", "typed-cursor", "typed-plus-ghost", "dim-combined-sgr"],
)
def test_has_draft_counts_only_text_that_is_not_dim(crs, prompt_line: str, draft: bool) -> None:
    assert crs.has_draft(pane(prompt_line)) is draft


def test_a_prompt_mark_outside_the_input_box_is_not_the_prompt(crs) -> None:
    """The footer's agent list can open with the same mark; only the line under a rule counts."""
    captured = "\n".join(["❯ an agent row with text", RULE, "\x1b[39m❯\xa0", RULE])
    assert crs.has_draft(captured) is False


def test_no_prompt_line_is_no_draft(crs) -> None:
    assert crs.has_draft("just output\nmore output") is False


def test_the_pre_fix_reading_called_a_ghost_a_draft(crs) -> None:
    """Without -e the dim suggestion is indistinguishable from typed text: the old false positive."""
    stripped = re.sub(r"\x1b\[[0-9;]*m", "", pane("\x1b[39m❯\xa0\x1b[2mgo Step 0\x1b[0m"))
    assert crs.has_draft(stripped) is True  # what capture-pane without -e handed the old check


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
