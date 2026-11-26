# SPDX-License-Identifier: AGPL-3.0-or-later
"""The worktree Preflight row reaches a subagent only in a project whose tracked settings name a preflight command.

The row used to go into every dispatch in every project (nexus-z7ddd), then only where a nexus script was present. It is now the
project's own ``preflight`` setting in ``.nexus/project.json``: the command a worktree agent runs as its first action. A project
with no such setting gets no row. The helper is pinned directly, and the hook's real output through the child-process driver of
test_subagent_start_hook.py. The child-process tests take a tenant (the hook's T2 scan reaches the engine); the helper tests take
none.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nexus.hooks import subagent_start
from tests.hooks.test_subagent_start_hook import _run_hook

_ROW = (
    "| Preflight | If dispatched with isolation:worktree: run `{cmd}` as your FIRST action; stop and report if it "
    "prints PREFLIGHT_FAIL or exits non-zero |\n"
)


def test_the_row_is_the_projects_preflight_command(project_settings_repo) -> None:
    repo = project_settings_repo(preflight="scripts/agent-worktree-preflight.sh")
    assert subagent_start._worktree_preflight_row({"cwd": str(repo)}) == _ROW.format(cmd="scripts/agent-worktree-preflight.sh")


def test_the_row_is_withheld_in_a_project_with_no_preflight_setting(tmp_path: Path, project_strict_repo: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert subagent_start._worktree_preflight_row({"cwd": str(tmp_path)}) == ""
    assert subagent_start._worktree_preflight_row({"cwd": str(project_strict_repo)}) == ""


def test_the_row_is_withheld_when_no_checkout_is_named() -> None:
    assert subagent_start._worktree_preflight_row({}) == ""
    assert subagent_start._worktree_preflight_row({"cwd": "${cwd}"}) == ""


def test_a_script_in_the_tree_without_the_setting_gives_no_row(tmp_path: Path) -> None:
    """The old keying on the script's presence is gone: the plugin names no script, the project's settings do."""
    (tmp_path / ".git").mkdir()
    script = tmp_path / "scripts" / "agent-worktree-preflight.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    assert subagent_start._worktree_preflight_row({"cwd": str(tmp_path)}) == ""


def test_a_cwd_deep_in_the_project_finds_the_settings(project_settings_repo) -> None:
    repo = project_settings_repo(preflight="tools/pre.sh")
    deep = repo / "src" / "pkg"
    deep.mkdir(parents=True)
    assert subagent_start._worktree_preflight_row({"cwd": str(deep)}) == _ROW.format(cmd="tools/pre.sh")


def test_a_project_nested_under_one_with_a_setting_gets_no_row(project_settings_repo) -> None:
    """The walk stops at the cwd's own checkout: a clone inside a project is another project."""
    outer = project_settings_repo(preflight="tools/pre.sh")
    nested = outer / "vendor" / "other"
    (nested / "src").mkdir(parents=True)
    (nested / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")  # a worktree's .git file
    assert subagent_start._worktree_preflight_row({"cwd": str(nested / "src")}) == ""


def test_the_nexus_repo_itself_names_its_script_in_its_own_settings() -> None:
    repo = Path(__file__).resolve().parents[2]
    settings = json.loads((repo / ".nexus" / "project.json").read_text(encoding="utf-8"))
    assert settings["preflight"] == "scripts/agent-worktree-preflight.sh"
    assert (repo / settings["preflight"]).is_file()
    assert subagent_start._worktree_preflight_row({"cwd": str(repo)}) == _ROW.format(cmd=settings["preflight"])


def _repo(path: Path, *, preflight: str | None) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if preflight is not None:
        settings = path / ".nexus" / "project.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"schema": 1, "preflight": preflight}), encoding="utf-8")
    return path


def _injected(cwd: Path) -> str:
    result = _run_hook(cwd=str(cwd), stdin=json.dumps({
        "session_id": "test-session", "hook_event_name": "SubagentStart", "task": "general research task",
        "prompt": "look into something", "cwd": str(cwd)}))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.t2_tenant
def test_a_dispatch_in_a_project_with_no_setting_gets_no_preflight_row(tmp_path: Path) -> None:
    ctx = _injected(_repo(tmp_path / "other-project", preflight=None))
    assert "| Completion |" in ctx and "| Search scope |" in ctx  # the Orchestration table is there
    assert "| Preflight |" not in ctx
    assert "agent-worktree-preflight.sh" not in ctx


@pytest.mark.t2_tenant
def test_a_dispatch_in_a_project_with_the_setting_gets_the_row(tmp_path: Path) -> None:
    ctx = _injected(_repo(tmp_path / "with-setting", preflight="tools/preflight.sh"))
    assert _ROW.format(cmd="tools/preflight.sh") in ctx
