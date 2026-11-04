# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-z7ddd: the worktree Preflight row reaches a subagent only in a project that has the script.

Before this, every dispatch in every project was told to run
``scripts/agent-worktree-preflight.sh`` first, a script only the nexus repo ships. The helper is
pinned directly, and the hook's real output through the child-process driver of
test_subagent_start_hook.py in throwaway git repos. The two child-process tests take a tenant
(the hook's T2 scan reaches the engine); the helper tests take none.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nexus.hooks import subagent_start
from tests.hooks.test_subagent_start_hook import _run_hook


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


def _repo(path: Path, *, with_script: bool) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if with_script:
        script = path / subagent_start._WORKTREE_PREFLIGHT_SCRIPT
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n")
    return path


def _injected(cwd: Path) -> str:
    result = _run_hook(cwd=str(cwd))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.t2_tenant
def test_a_dispatch_in_another_project_gets_no_preflight_row(tmp_path: Path) -> None:
    ctx = _injected(_repo(tmp_path / "other-project", with_script=False))
    assert "| Search scope |" in ctx  # the Orchestration table is there
    assert "| Preflight |" not in ctx
    assert "agent-worktree-preflight.sh" not in ctx


@pytest.mark.t2_tenant
def test_a_dispatch_in_a_project_with_the_script_gets_the_row(tmp_path: Path) -> None:
    ctx = _injected(_repo(tmp_path / "nexus-like", with_script=True))
    assert "| Preflight |" in ctx
    assert "agent-worktree-preflight.sh" in ctx
