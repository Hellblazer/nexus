# SPDX-License-Identifier: AGPL-3.0-or-later
"""SubagentStart's audience split, driven through a child process like ``test_subagent_start_hook.py`` (nexus-sby49.3, T5).

These cases run the hook with no engine and need no tenant: the hook's T2 sections come back empty on an unreachable
service. They live here, not in ``test_subagent_start_hook.py``, because that module's ``pytestmark = t2_tenant`` makes
every test added to it mint one, and the python_unit ceiling in ``tests/storage_touch_ceiling.toml`` has no room (the
convention in ``tests/AGENTS.md``: a module mark counts every later test, pure ones included).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.hooks.audience_harness import src_seam_env
from tests.hooks.test_subagent_start_hook import STDIN_PAYLOAD, _flagged_home, _run_hook

_DEAD_ENGINE = {"NX_SERVICE_URL": "http://127.0.0.1:9", "NX_SERVICE_TOKEN": "unused"}


def _context(*, payload: dict | None = None, account_home: str | None = None) -> str:
    env = dict(_DEAD_ENGINE)
    if account_home:
        env.update(src_seam_env(account_home))
    kwargs = {"stdin": json.dumps(payload)} if payload is not None else {}
    result = _run_hook(env_overrides=env, **kwargs)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


class TestOrchestrationRowsByAudience:
    def test_a_user_gets_no_git_row_and_no_search_scope_row(self) -> None:
        """U4 and the Search-scope row: outside nexus and with no flag, only Completion and Inbox. The Git row is the
        prompt half of the maintainer's git guard, which a user does not have."""
        ctx = _context()
        assert "| Directive | Rule |" in ctx
        assert "| Git |" not in ctx
        assert "NEVER git add/commit" not in ctx
        assert "| Search scope |" not in ctx

    def test_a_marked_nexus_checkout_gets_the_git_and_search_scope_rows(self, nexus_marked_repo: Path) -> None:
        ctx = _context(payload={**json.loads(STDIN_PAYLOAD), "cwd": str(nexus_marked_repo)})
        assert "| Git |" in ctx
        assert "NEVER git add/commit" in ctx
        assert "orchestrator commits pathspec-limited" in ctx
        assert "| Search scope |" in ctx

    def test_the_account_flag_alone_gets_the_git_and_search_scope_rows(self, tmp_path: Path) -> None:
        """SAM-WIDE: Sam's other repos carry the rows because his account does, with no marker in the repo."""
        ctx = _context(account_home=_flagged_home(tmp_path))
        assert "NEVER git add/commit" in ctx
        assert "| Search scope |" in ctx

    def test_the_relay_bead_field_takes_a_tracking_id(self) -> None:
        """D10: the field keeps its name, and a user's tracker id is a valid value."""
        assert "| Bead | Bead ID with status, a tracking id, or 'none' |" in _context()


_REAL_SHAPE = {"session_id": "test-session", "hook_event_name": "SubagentStart", "prompt_id": "abc123"}
_IMPLEMENTERS = ["developer", "conexus:developer", "worktree-developer", "general-purpose", "claude", "conexus:debugger"]


class TestBriefCheckByAudience:
    @pytest.mark.parametrize("agent_type", _IMPLEMENTERS)
    def test_a_user_gets_no_brief_check(self, agent_type: str) -> None:
        """D10 / T5: the brief check is the maintainer's (nexus_ffa, nexus-yjg4v); a user's implementer gets none."""
        assert "## Brief check" not in _context(payload={**_REAL_SHAPE, "agent_type": agent_type})

    def test_a_maintainer_outside_nexus_gets_four_renumbered_questions_without_modes(self, tmp_path: Path) -> None:
        """Question 3 is nexus's own local/cloud axis (NEXUS-REPO): Sam's other repos get 1, 2, 4 and 5, numbered 1 to 4."""
        ctx = _context(payload={**_REAL_SHAPE, "agent_type": "general-purpose"}, account_home=_flagged_home(tmp_path))
        assert "## Brief check" in ctx
        assert "local AND cloud" not in ctx and "Modes:" not in ctx
        assert "1. Consumers:" in ctx and "2. Final point:" in ctx
        assert "3. Bounds:" in ctx and "4. State:" in ctx
        assert "5. " not in ctx.split("## Brief check", 1)[1].split("\n## ", 1)[0]
