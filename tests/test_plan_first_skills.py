# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-078 P5 — skills + hook edits + per-agent prompts
(nexus-05i.9).

Pins:

  * SC-7a: nine new skill files present and parseable (plan-first gate,
    five verb skills, three plan-mgmt skills).
  * SC-7b: SessionStart hook injects the ``## Plan Library (RDR-078)``
    block listing all five scenario verb names.
  * SC-7c: SubagentStart hook injects the plan-match-first preamble
    when the task text names any of the eight retrieval-shaped agents.
  * SC-7d: Each of the eight retrieval-shaped agent .md files cites
    ``plan_match`` independently (survives hook-context trimming).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "nx" / "skills"
AGENTS_DIR = REPO_ROOT / "nx" / "agents"
HOOKS_DIR = REPO_ROOT / "nx" / "hooks" / "scripts"


NEW_SKILLS: tuple[str, ...] = (
    "plan-first",
    "research", "review", "analyze", "debug", "document",
    "plan-author", "plan-inspect", "plan-promote",
)

RETRIEVAL_AGENTS: tuple[str, ...] = (
    "strategic-planner",
    "architect-planner",
    "code-review-expert",
    "substantive-critic",
    "deep-analyst",
    "deep-research-synthesizer",
    "debugger",
    "plan-auditor",
)


# ── SC-7a: skills present + frontmatter valid ──────────────────────────────


def test_all_nine_skills_present() -> None:
    for name in NEW_SKILLS:
        path = SKILLS_DIR / name / "SKILL.md"
        assert path.exists(), f"missing skill: {path}"


def test_all_nine_skills_have_frontmatter() -> None:
    for name in NEW_SKILLS:
        text = (SKILLS_DIR / name / "SKILL.md").read_text()
        assert text.startswith("---\n"), (
            f"{name}: SKILL.md must begin with YAML frontmatter"
        )
        # Frontmatter closes on the second '---' line.
        closing = re.search(r"^---\s*$", text[4:], flags=re.MULTILINE)
        assert closing is not None, f"{name}: frontmatter not closed"


def test_every_new_skill_registered_in_registry_yaml() -> None:
    """The bidirectional-registry test in test_plugin_structure pins
    that every skill dir is in registry.yaml; re-pin explicitly here."""
    import yaml

    registry = yaml.safe_load(
        (REPO_ROOT / "nx" / "registry.yaml").read_text()
    )
    standalone = set(registry.get("standalone_skills", {}).keys())
    for name in NEW_SKILLS:
        assert name in standalone, (
            f"skill '{name}' missing from registry.yaml standalone_skills"
        )


# ── SC-7b: SessionStart hook Plan Library block ────────────────────────────


def _run_session_start_hook() -> str:
    """Invoke the SessionStart hook with a benign env and return stdout."""
    proc = subprocess.run(
        ["python3", str(HOOKS_DIR / "session_start_hook.py")],
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "NX_HOOK_DEBUG": "0",
             "CLAUDE_PROJECT_DIR": str(REPO_ROOT)},
    )
    return proc.stdout


def test_session_start_hook_emits_plan_library_block() -> None:
    output = _run_session_start_hook()
    assert "## Plan Library (RDR-078)" in output


def test_session_start_hook_lists_five_scenario_verbs() -> None:
    output = _run_session_start_hook()
    for verb in ("research", "review", "analyze", "debug", "document"):
        assert f"**{verb}**" in output, (
            f"scenario verb '{verb}' missing from Plan Library block"
        )


def test_session_start_hook_points_to_plan_first_gate() -> None:
    output = _run_session_start_hook()
    assert "plan-first" in output or "/nx:plan-first" in output
    assert "plan_match" in output


# ── SC-7c: SubagentStart preamble injection ────────────────────────────────


def _run_subagent_start(task_text: str) -> str:
    """Invoke the SubagentStart hook with a task text and return stdout."""
    import json as _json

    stdin = _json.dumps({"task": task_text, "prompt": task_text})
    proc = subprocess.run(
        ["bash", str(HOOKS_DIR / "subagent-start.sh")],
        input=stdin, capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin",
             "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT / "nx")},
    )
    return proc.stdout


@pytest.mark.parametrize("agent", RETRIEVAL_AGENTS)
def test_subagent_start_preamble_present_for_each_retrieval_agent(
    agent: str,
) -> None:
    """Every one of the eight retrieval-shaped agents triggers the
    plan-match-first preamble when its name appears in the task text."""
    out = _run_subagent_start(f"dispatching to {agent} for this task")
    assert "RDR-078 Plan-match-first" in out, (
        f"preamble missing for {agent}:\n{out[:500]}"
    )
    assert "plan_match" in out


def test_subagent_start_preamble_absent_for_unrelated_agent() -> None:
    """Non-retrieval agent names (e.g. ``developer``) don't trigger
    the preamble — it'd waste tokens."""
    out = _run_subagent_start("dispatching to developer for implementation")
    assert "RDR-078 Plan-match-first" not in out


# ── SC-7d: Each agent-md cites plan_match independently ────────────────────


@pytest.mark.parametrize("agent", RETRIEVAL_AGENTS)
def test_each_agent_md_cites_plan_match(agent: str) -> None:
    text = (AGENTS_DIR / f"{agent}.md").read_text()
    assert "plan_match" in text, (
        f"{agent}.md must cite plan_match so the discipline survives "
        f"hook-context trimming (SC-7)"
    )


@pytest.mark.parametrize("agent", RETRIEVAL_AGENTS)
def test_each_agent_md_references_rdr_078(agent: str) -> None:
    text = (AGENTS_DIR / f"{agent}.md").read_text()
    assert "RDR-078" in text, (
        f"{agent}.md should reference RDR-078 for context"
    )
