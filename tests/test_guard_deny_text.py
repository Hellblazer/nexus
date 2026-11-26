# SPDX-License-Identifier: AGPL-3.0-or-later
"""The strict subagent git guard's deny text (RDR-259 Stage 1, nexus-sby49.9).

The text names "the orchestrator" and defines it once, says what the scan reads in plain words (no ``STRUCTURE-AGNOSTIC``), and
carries the forced-strict clause only when the strict reading comes from a settings file that could not be read. The rule's
internal name stays: routing events and the registry key on it.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from tests.hooks.audience_harness import plain_home, strict_repo
from tests.test_user_git_seatbelt import clean_repo, dirty_repo  # noqa: F401 -- the seatbelt's repo fixtures

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
GUARD = PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "subagent_git_write_requires_orchestrator.py"
HARNESS = PROJECT_ROOT / "tests" / "hooks" / "audience_harness.py"
DEFINITION = "the orchestrator (the session that dispatched you)"


def _deny(repo: pathlib.Path, home: pathlib.Path, cmd: str = "git commit -am x") -> str:
    payload = {
        "tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s1", "cwd": str(repo),
        "agent_id": "aworker-x-1", "agent_type": "general-purpose",
    }
    env = {
        **os.environ, "NX_HOOK_PYTHON": sys.executable,
        "NX_ROUTING_LOG_PATH": str(home / "routing.jsonl"), "NEXUS_CONFIG_DIR": str(home / "cfg"),
        "NX_DROPPED_WRITES_LOG_PATH": str(home / "dropped.jsonl"),
    }
    env.pop("NX_REMOTE_TEST_HOSTS", None)
    env.pop("NX_SERVICE_URL", None)
    proc = subprocess.run(
        [sys.executable, str(HARNESS), str(home), str(GUARD)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny", out
    return out["permissionDecisionReason"]


@pytest.fixture()
def strict_text(tmp_path) -> str:
    return _deny(pathlib.Path(strict_repo(tmp_path / "r")), pathlib.Path(plain_home(tmp_path / "h")))


def test_the_text_says_it_reads_the_raw_command_text_and_has_no_shouted_jargon(strict_text):
    assert "reads the raw command text" in strict_text
    assert "STRUCTURE-AGNOSTIC" not in strict_text


def test_the_orchestrator_is_defined_once_and_before_any_other_use(strict_text):
    assert strict_text.count(DEFINITION) == 1
    assert strict_text.index("orchestrator") == strict_text.index(DEFINITION) + len("the ")


def test_a_file_that_asked_for_strict_adds_no_forced_clause(strict_text):
    assert "the full guard applies because" not in strict_text


def test_an_unreadable_project_file_forces_strict_and_the_text_says_why(tmp_path):
    repo = pathlib.Path(strict_repo(tmp_path / "r"))
    (repo / ".nexus" / "project.json").write_text("{not json", encoding="utf-8")
    text = _deny(repo, pathlib.Path(plain_home(tmp_path / "h")))
    assert "the full guard applies because `" in text and "project.json" in text, text


def test_a_plain_repository_gets_the_seatbelt_and_no_orchestrator_text(dirty_repo, tmp_path):
    text = _deny(dirty_repo, pathlib.Path(plain_home(tmp_path / "h")), "git checkout -- f")
    assert "orchestrator" not in text and "the full guard applies" not in text
