# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-z7ddd: the worktree Preflight row reaches a subagent only in a project that has the script.

Before this, every dispatch in every project was told to run
``scripts/agent-worktree-preflight.sh`` first, a script only the nexus repo ships. The helper is
pinned directly so the test mints no tenant (tests/storage_touch_ceiling.toml).
"""
from __future__ import annotations

from pathlib import Path

from nexus.hooks import subagent_start


def test_the_row_is_given_in_a_project_that_has_the_script(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "agent-worktree-preflight.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    row = subagent_start._worktree_preflight_row(str(tmp_path))
    assert row == subagent_start._WORKTREE_PREFLIGHT
    assert "agent-worktree-preflight.sh" in row


def test_the_row_is_withheld_in_a_project_without_the_script(tmp_path: Path) -> None:
    assert subagent_start._worktree_preflight_row(str(tmp_path)) == ""


def test_the_row_is_withheld_when_no_repo_root_was_found() -> None:
    assert subagent_start._worktree_preflight_row("") == ""


def test_the_nexus_repo_itself_still_gets_the_row() -> None:
    repo = Path(__file__).resolve().parents[2]
    assert (repo / subagent_start._WORKTREE_PREFLIGHT_SCRIPT).is_file()
    assert subagent_start._worktree_preflight_row(str(repo)) == subagent_start._WORKTREE_PREFLIGHT
