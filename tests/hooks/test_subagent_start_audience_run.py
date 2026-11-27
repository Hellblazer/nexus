# SPDX-License-Identifier: AGPL-3.0-or-later
"""SubagentStart's audience split, driven through a child process like ``test_subagent_start_hook.py`` (nexus-sby49.3, T5).

These cases run the hook with no engine and need no tenant: the hook's T2 sections come back empty on an unreachable
service. They live here, not in ``test_subagent_start_hook.py``, because that module's ``pytestmark = t2_tenant`` makes
every test added to it mint one, and the python_unit ceiling in ``tests/storage_touch_ceiling.toml`` has no room (the
convention in ``tests/AGENTS.md``: a module mark counts every later test, pure ones included).
"""
from __future__ import annotations

import atexit
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.hooks.audience_harness import path_without, src_seam_env
from tests.hooks.test_subagent_start_hook import STDIN_PAYLOAD, _strict_home, _run_hook

# The hook lists T1 by running ``nx scratch list`` whenever ``nx`` is on PATH, and that child reaches storage: under CI's
# storage-touch guard (a thin venv puts ``nx`` on PATH) every case here ended in a teardown error, "reached storage without
# a tenant". These cases are about the injected text, so the hook runs with no ``nx`` on PATH and touches nothing.
_MIRROR = tempfile.mkdtemp(prefix="nx-path-no-nx-")
atexit.register(shutil.rmtree, _MIRROR, ignore_errors=True)
_NO_NX = {"PATH": path_without(("nx",), _MIRROR)}
_DEAD_ENGINE = {"NX_SERVICE_URL": "http://127.0.0.1:9", "NX_SERVICE_TOKEN": "unused", **_NO_NX}


def _context(*, payload: dict | None = None, account_home: str | None = None) -> str:
    env = dict(_DEAD_ENGINE)
    if account_home:
        env.update(src_seam_env(account_home))
    kwargs = {"stdin": json.dumps(payload)} if payload is not None else {}
    result = _run_hook(env_overrides=env, **kwargs)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


class TestOrchestrationRowsBySetting:
    def test_a_plain_session_gets_the_search_scope_row_and_no_guard_row(self) -> None:
        """The Search scope row is process, for every dispatch. The Git row is the prompt half of the strict guard, which
        is a setting: a plain session has the seatbelt and no such row."""
        ctx = _context()
        assert "| Directive | Rule |" in ctx
        assert "| Git |" not in ctx
        assert "NEVER git add/commit" not in ctx
        assert "| Search scope |" in ctx

    def test_a_project_with_the_strict_setting_gets_the_guard_row(self, project_strict_repo: Path) -> None:
        ctx = _context(payload={**json.loads(STDIN_PAYLOAD), "cwd": str(project_strict_repo)})
        assert "| Git |" in ctx
        assert "NEVER git add/commit" in ctx
        assert "orchestrator commits pathspec-limited" in ctx
        assert "| Search scope |" in ctx

    def test_a_repository_strict_only_at_head_gets_the_guard_row_the_guard_denies_for(self, tmp_path: Path) -> None:
        """The guard reads the committed copy, so a working copy that dropped the strict key still denies; the row says so too."""
        from tests.hooks.audience_harness import write_project_settings
        from tests.hooks.git_shim import init_repo

        repo = init_repo(tmp_path / "r", {".nexus/project.json": json.dumps({"version": 1, "subagent_git": "strict"})})
        write_project_settings(repo, subagent_git="seatbelt")
        ctx = _context(payload={**json.loads(STDIN_PAYLOAD), "cwd": str(repo)})
        assert "| Git |" in ctx and "NEVER git add/commit" in ctx

    def test_a_plain_repository_gets_no_guard_row_from_the_head_read(self, tmp_path: Path) -> None:
        from tests.hooks.git_shim import init_repo

        repo = init_repo(tmp_path / "plain", {"README": "x"})
        ctx = _context(payload={**json.loads(STDIN_PAYLOAD), "cwd": str(repo)})
        assert "| Git |" not in ctx

    def test_a_user_with_the_strict_setting_gets_the_guard_row_in_any_project(self, tmp_path: Path) -> None:
        ctx = _context(account_home=_strict_home(tmp_path))
        assert "NEVER git add/commit" in ctx
        assert "| Search scope |" in ctx

    def test_the_relay_bead_field_takes_a_tracking_id(self) -> None:
        """D10: the field keeps its name, and a user's tracker id is a valid value."""
        assert "| Bead | Bead ID with status, a tracking id, or 'none' |" in _context()


_REAL_SHAPE = {"session_id": "test-session", "hook_event_name": "SubagentStart", "prompt_id": "abc123"}
_IMPLEMENTERS = ["developer", "conexus:developer", "worktree-developer", "general-purpose", "claude", "conexus:debugger"]


class TestBriefCheckForEveryone:
    @pytest.mark.parametrize("agent_type", _IMPLEMENTERS)
    def test_every_implementer_gets_the_four_generic_questions(self, agent_type: str) -> None:
        """The brief check is process: a plain session's implementer gets the four questions, numbered 1 to 4."""
        ctx = _context(payload={**_REAL_SHAPE, "agent_type": agent_type})
        assert "## Brief check" in ctx
        assert "1. Consumers:" in ctx and "2. Final point:" in ctx
        assert "3. Bounds:" in ctx and "4. State:" in ctx
        assert "local AND cloud" not in ctx and "Modes:" not in ctx
        assert "5. " not in ctx.split("## Brief check", 1)[1].split("\n## ", 1)[0]

    def test_a_project_adds_its_own_question(self, project_settings_repo) -> None:
        repo = project_settings_repo(brief_questions=["Does it hold on every platform we ship?"])
        ctx = _context(payload={**_REAL_SHAPE, "agent_type": "general-purpose", "cwd": str(repo)})
        assert "5. Does it hold on every platform we ship?" in ctx
