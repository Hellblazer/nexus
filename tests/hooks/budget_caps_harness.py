# SPDX-License-Identifier: AGPL-3.0-or-later
"""The every-key-at-its-cap fixtures for the SubagentStart byte budget (nexus-sby49.11, RDR-259 Decision 2).

The project and user settings files with every key at its cap, the repo that carries the project file, and the label check.
The budget runs themselves live in ``test_subagent_start_byte_budget.py``, inside the default-case tenants, so the at-cap
measure adds no storage-touch tenant; the non-tenant cap tests are in ``test_subagent_start_byte_budget_at_caps.py``.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nexus.hooks._audience import MAX_BRIEF_QUESTIONS, provenance_label

#: The RDR's caps, in bytes of UTF-8. The test also reads them from the schema, so a change to either side fails there.
_CAPS = {"preflight": 160, "ci_wait": 200, "credential_helper": 600, "integration_branch": 100}
_QUESTION_CAP = 160


def _padded(lead: str, size: int) -> str:
    """*lead*, then two-byte characters, then ASCII, to exactly *size* bytes of UTF-8."""
    text = lead
    while len((text + "é").encode("utf-8")) <= size:
        text += "é"
    while len(text.encode("utf-8")) < size:
        text += "x"
    assert len(text.encode("utf-8")) == size
    return text


def _questions(tag: str, count: int) -> list[str]:
    return [_padded(f"{tag}{n}: does it hold? ", _QUESTION_CAP) for n in range(1, count + 1)]


def _project_at_caps() -> dict:
    return {
        "version": 1,
        "subagent_git": "strict",
        "preflight": _padded("scripts/preflight ", _CAPS["preflight"]),
        "ci_wait": _padded("ci-wait <sha> ", _CAPS["ci_wait"]),
        "credential_helper": _padded("Use tools/credentials.sh. ", _CAPS["credential_helper"]),
        "integration_branch": "release/" + "x" * (_CAPS["integration_branch"] - len("release/")),
        "brief_questions": _questions("project", MAX_BRIEF_QUESTIONS - 1),
    }


def _user_at_caps() -> dict:
    return {
        "version": 1,
        "subagent_git": "strict",
        "telemetry": False,
        "t1_nudge": True,
        "brief_questions": _questions("user", 1),
    }


def _project_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "capped-project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "project.json").write_text(json.dumps(_project_at_caps()), encoding="utf-8")
    return repo


def _worktree_of(repo: Path, tmp_path: Path) -> Path:
    """A linked worktree of *repo*, which carries the committed project file."""
    subprocess.run(["git", "-C", str(repo), "add", ".nexus/project.json"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"], check=True
    )
    worktree = tmp_path / "agent-worktree-caps-probe"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(worktree), "-b", "wt-branch"], check=True)
    return worktree


def _assert_every_subagent_start_text_is_labelled(ctx: str) -> None:
    project, user = _project_at_caps(), _user_at_caps()
    preflight = provenance_label("preflight", "project", project["preflight"])
    assert preflight in ctx and project["preflight"] in preflight, "the preflight row did not carry the full command"
    project_label = provenance_label("brief_questions", "project")
    user_label = provenance_label("brief_questions", "user")
    p1, p2 = project["brief_questions"]
    (u1,) = user["brief_questions"]
    assert ctx.index(project_label) < ctx.index(p1) < ctx.index(p2) < ctx.index(user_label) < ctx.index(u1), (
        "each question follows its own source's label, the project's list before the user's"
    )
    assert "| Git |" in ctx, "the strict Git row is in the measured injection"
