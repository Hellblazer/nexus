# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The checkpoint's process-listing cases (nexus-ct74m), POSIX only.

They read a process table and working directories, which the hook lists only on POSIX; on
Windows it says it did not list them, and test_session_facts.py pins that line. They live in
their own module, skipped on win32 at module level, so the native Windows set leaves the module
out instead of counting five skips against its ceiling (the nexus-j738q pattern).
"""
from __future__ import annotations

import os
import sys

import pytest

from tests.hooks.test_compact_checkpoint import NAME, env, mod  # noqa: F401 -- fixtures
from tests.hooks.test_session_facts import SNAP, _repo, _section, world  # noqa: F401 -- fixtures

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="process listing is POSIX-only; Windows says so (pinned in test_session_facts.py)")


class TestRunningShowsTheSessionsProcesses:
    def test_background_shells_are_listed_with_start_and_command(self, world, mod) -> None:
        sf = mod._session_facts
        world["table"] = [sf.Proc(4242, 1, "Fri Nov 13 01:00:00 2026", "claude"),
                          sf.Proc(5000, 4242, "Fri Nov 13 01:05:00 2026", SNAP.format("nx ci wait abc > /tmp/ci.log 2>&1")),
                          sf.Proc(5001, 5000, "Fri Nov 13 01:05:00 2026", "python nx ci wait abc"),
                          sf.Proc(5100, 4242, "Fri Nov 13 01:06:00 2026", SNAP.format("nx tuple wait mailbox/x")),
                          sf.Proc(6000, 4242, "Fri Nov 13 01:00:01 2026", "node mcp-server.js")]
        running = _section(world["write"](), "Running for this session")
        assert "- background task pid 5000, started Fri Nov 13 01:05:00 2026: `nx ci wait abc > /tmp/ci.log 2>&1`" in running
        assert "- background task pid 5100, started Fri Nov 13 01:06:00 2026 (standing mail wait)" in running
        assert "mcp-server" not in running

    def test_no_background_shell_says_none_only_on_proof(self, world, mod) -> None:
        world["table"] = [mod._session_facts.Proc(4242, 1, "Fri Nov 13 01:00:00 2026", "claude")]
        assert "- background tasks: none running" in world["write"]()

    def test_processes_in_the_sessions_worktrees(self, world, mod, env, monkeypatch, tmp_path) -> None:
        _repo(tmp_path, {NAME: True})
        wt = tmp_path / "wt" / NAME
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        sf = mod._session_facts
        me = os.getpid()
        world["table"] = [sf.Proc(7000, 1, "Sat Nov 8 03:00:00 2026", "mineru-api --port 0"),
                          sf.Proc(me, 1, "Fri Nov 13 01:00:00 2026", "python compact_checkpoint.py pre"),
                          sf.Proc(7100, me, "Fri Nov 13 01:00:01 2026", "nx command-context continuation")]
        world["cwds"] = {7000: str(wt / "sub"), 7100: str(wt)}
        text = world["write"](cwd=str(tmp_path / "primary"))
        assert "- process pid 7000 in " in text and "`mineru-api --port 0`" in text
        assert "pid 7100" not in text  # the hook's own continuation child is not the session's work



class TestNoFalseClaimAboutProcesses:
    def test_a_lookup_that_failed_is_not_none_running(self, world, mod, env) -> None:
        (env.claude / "sessions" / "4242.json").unlink()  # the session file is gone (a /clear, a layout change)
        world["table"] = [mod._session_facts.Proc(1, 0, "Fri Nov 13 01:00:00 2026", "init")]
        text = world["write"]()
        assert "none running" not in text and "- background tasks: not listed (no " in text

    def test_unreadable_working_directories_are_not_no_processes(self, world, mod, env, monkeypatch, tmp_path) -> None:
        _repo(tmp_path, {NAME: True})
        monkeypatch.setattr(mod, "_git_worktrees", env.real_git_worktrees)
        world["table"] = [mod._session_facts.Proc(4242, 1, "Fri Nov 13 01:00:00 2026", "claude")]
        world["cwds"] = None
        assert "processes in this session's worktrees: not listed" in world["write"](cwd=str(tmp_path / "primary"))