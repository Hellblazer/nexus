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
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    row = subagent_start._worktree_preflight_row(str(tmp_path))
    assert row == subagent_start._WORKTREE_PREFLIGHT
    assert "agent-worktree-preflight.sh" in row


def test_the_row_is_withheld_in_a_project_without_the_script(tmp_path: Path) -> None:
    assert subagent_start._worktree_preflight_row(str(tmp_path)) == ""


def test_the_row_is_withheld_when_no_repo_root_was_found() -> None:
    assert subagent_start._worktree_preflight_row("") == ""


def test_nexus_13mbx_cwd_inside_a_checkout_gives_the_row_when_git_found_no_root(tmp_path: Path) -> None:
    script = tmp_path / subagent_start._WORKTREE_PREFLIGHT_SCRIPT
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    deep = tmp_path / "src" / "pkg"
    deep.mkdir(parents=True)
    assert subagent_start._worktree_preflight_row("", str(deep)) == subagent_start._WORKTREE_PREFLIGHT
    assert subagent_start._worktree_preflight_row(str(tmp_path / "elsewhere"), str(deep)) \
        == subagent_start._WORKTREE_PREFLIGHT


def test_nexus_13mbx_cwd_with_no_script_above_it_gives_no_row(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert subagent_start._worktree_preflight_row("", str(tmp_path / "a" / "b")) == ""
    assert subagent_start._worktree_preflight_row("", str(tmp_path / "missing")) == ""


def test_nexus_13mbx_a_project_nested_under_a_checkout_with_the_script_gets_no_row(tmp_path: Path) -> None:
    """The walk stops at the cwd's own checkout: a clone inside a nexus tree is another project."""
    script = tmp_path / subagent_start._WORKTREE_PREFLIGHT_SCRIPT
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    nested = tmp_path / "vendor" / "other"
    (nested / "src").mkdir(parents=True)
    (nested / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")  # a worktree's .git file
    assert subagent_start._worktree_preflight_row("", str(nested / "src")) == ""
    assert subagent_start._worktree_preflight_row("", "${cwd}") == ""


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
        script.write_text("#!/bin/sh\n", encoding="utf-8")
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


@pytest.mark.t2_tenant
def test_nexus_13mbx_payload_cwd_gives_the_row_outside_any_git_repo(tmp_path: Path) -> None:
    """The hook process runs outside any repo, so git finds no root, and the payload's cwd is
    a checkout that carries the script (the shape of a bare repo with worktrees, or a git that
    timed out). Before nexus-13mbx the row was withheld here."""
    checkout = tmp_path / "checkout"
    script = checkout / subagent_start._WORKTREE_PREFLIGHT_SCRIPT
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = json.dumps({"session_id": "test-session", "hook_event_name": "SubagentStart",
                          "task": "general research task", "prompt": "look into something",
                          "cwd": str(checkout)})
    result = _run_hook(cwd=str(outside), stdin=payload,
                       env_overrides={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "| Preflight |" in ctx
