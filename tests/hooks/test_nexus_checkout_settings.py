# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stage 2 of RDR-259 (nexus-sby49.17): nexus's own words live in nexus's own files.

The plugin carries the development process for every user; what only nexus has (the strict guard, the preflight script, the
local-and-cloud brief question, the CI wait command, the develop branch) is turned on by nexus's tracked ``.nexus/project.json``.
This file reads the REAL checkout's file and the REAL hook functions, then runs the same functions in a plain repository, so a
nexus session is shown to get the full guard, the preflight row, the question and the CI command, and a plain one none of them.

Pure functions and the plugin's settings reader only: no engine, no T2 tenant.
"""
from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path

import pytest

from nexus.hooks import subagent_start
from tests._changelog_entry import changelog_entry

ROOT = Path(__file__).resolve().parents[2]
PROJECT_FILE = ROOT / ".nexus" / "project.json"
_SCRIPTS = ROOT / "conexus" / "hooks" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _audience  # noqa: E402 — the plugin's helper, imported as the hook scripts import it
import stop_wake_guard as swg  # noqa: E402

_QUESTION = "Does it hold in both modes, local AND cloud?"
_CI_BLOCK = "The runs are still going."


@pytest.fixture
def nexus_payload(plugin_account) -> dict:
    """A payload whose cwd is this checkout, with the plugin helper pinned to an account that has no settings file, so only
    the checkout's own ``.nexus/project.json`` can speak."""
    return {"cwd": str(ROOT)}


@pytest.fixture
def plain_payload(tmp_path, plugin_account) -> dict:
    (tmp_path / "plain" / ".git").mkdir(parents=True)
    return {"cwd": str(tmp_path / "plain")}


def test_the_project_file_is_the_one_nexus_settings_file() -> None:
    assert PROJECT_FILE.is_file()
    assert not (ROOT / ".nexus" / "dev-process.json").exists(), "the Phase 2 file is deleted (Decision 7)"
    body = json.loads(PROJECT_FILE.read_text(encoding="utf-8"))
    assert body["version"] == 1 and "schema" not in body
    assert body["subagent_git"] == "strict"
    assert body["integration_branch"] == "develop"
    assert body["brief_questions"] == [_QUESTION]
    assert body["ci_wait"].startswith("nx ci wait ") and "--branch develop" in body["ci_wait"]
    assert body["credential_helper"].strip()


def test_the_preflight_the_file_names_is_the_script_nexus_keeps() -> None:
    body = json.loads(PROJECT_FILE.read_text(encoding="utf-8"))
    script = ROOT / body["preflight"]
    assert script.is_file()
    if sys.platform != "win32":
        assert script.stat().st_mode & stat.S_IXUSR


def test_the_reader_finds_no_problem_in_the_file(nexus_payload) -> None:
    assert _audience.settings_problems(nexus_payload) == []


def test_a_nexus_session_gets_the_full_guard_the_preflight_row_and_the_question(nexus_payload) -> None:
    assert _audience.guard_mode(nexus_payload) == "strict"
    assert "| Git |" in subagent_start._orchestration(nexus_payload)
    row = subagent_start._worktree_preflight_row(nexus_payload)
    assert row.startswith("| Preflight |") and "scripts/agent-worktree-preflight.sh" in row
    brief = subagent_start._brief_check(nexus_payload)
    assert brief.rstrip().endswith(f"5. {_QUESTION}") and "`.nexus/project.json`" in brief
    assert _audience.integration_branch(nexus_payload) == "develop"


def test_a_nexus_session_gets_the_ci_wait_command_in_the_stop_guard(nexus_payload) -> None:
    command = _audience.project_text(nexus_payload, "ci_wait")
    assert command == "nx ci wait <sha> --branch develop"
    text = swg.block_reason(swg.CLASS_CI_WAIT, _CI_BLOCK, ci_wait=_audience.project_text(nexus_payload, "ci_wait"))
    assert f"`{command}`" in text


def test_a_plain_repository_gets_none_of_them(plain_payload) -> None:
    assert _audience.guard_mode(plain_payload) != "strict"
    assert "| Git |" not in subagent_start._orchestration(plain_payload)
    assert subagent_start._worktree_preflight_row(plain_payload) == ""
    brief = subagent_start._brief_check(plain_payload)
    assert _QUESTION not in brief and "adds these questions" not in brief
    assert _audience.integration_branch(plain_payload) in (None, "")
    assert _audience.project_text(plain_payload, "ci_wait") is None
    text = swg.block_reason(swg.CLASS_CI_WAIT, _CI_BLOCK, ci_wait=_audience.project_text(plain_payload, "ci_wait"))
    assert "nx ci wait" not in text and "develop" not in text


# --- the prose that left the plugin's text is in nexus's own files -------------------------------------------------------

_RULES = ROOT / ".claude" / "rules"


def test_the_phase_gate_incident_is_nexus_prose_in_a_rule_not_plugin_text() -> None:
    rule = (_RULES / "phase-review-gate.md").read_text(encoding="utf-8")
    assert rule.startswith("---\npaths:\n")
    assert "RDR-112" in rule and "nexus-52lb" in rule and "three phases later" in rule
    for plugin_file in (ROOT / "src" / "nexus" / "hooks" / "subagent_start.py", *(ROOT / "conexus").rglob("*.md")):
        if plugin_file.name == "CHANGELOG.md":
            continue  # the released history of the plugin keeps the incident where it was written
        text = plugin_file.read_text(encoding="utf-8")
        assert "nexus-52lb" not in text, plugin_file


def test_the_ci_board_wake_fact_is_nexus_prose_in_a_rule() -> None:
    rule = (_RULES / "ci-wait.md").read_text(encoding="utf-8")
    command = json.loads(PROJECT_FILE.read_text(encoding="utf-8"))["ci_wait"]
    assert "--done" in rule and f"`{command}`" in rule and "channel" in rule


# --- the unreleased ledgers do not describe the gate that was removed ----------------------------------------------------

def _ledgers() -> dict[str, str]:
    return {
        "PENDING_RELEASE.md": (ROOT / "conexus" / "PENDING_RELEASE.md").read_text(encoding="utf-8"),
        "nexus-sby49.3-audience": changelog_entry("nexus-sby49.3-audience.md", "nexus-sby49.3"),
    }


@pytest.mark.parametrize("name", ["PENDING_RELEASE.md", "nexus-sby49.3-audience"])
def test_no_ledger_says_a_hook_runs_only_for_a_maintainer(name: str) -> None:
    text = _ledgers()[name]
    assert text, name
    stale = [ln for ln in text.splitlines() if re.search(r"only under `?maintainer\(\)", ln)]
    assert not stale, stale
