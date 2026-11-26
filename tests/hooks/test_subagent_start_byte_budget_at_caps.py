# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-259 Decision 2, the byte budget: with every settings key at its cap, each text the plugin prints from a settings file
stays within its budget (nexus-sby49.11).

The caps are the schema's (``SETTINGS`` in ``_audience.py``): ``preflight`` 160 bytes, ``brief_questions`` at most 3 in total
across both files at 160 bytes each (multi-byte UTF-8, so a byte count that a character count would miss is measured),
``ci_wait`` 200, ``credential_helper`` 600, ``integration_branch`` 100. The SubagentStart injection carries the preflight row and
the three questions with their Provenance labels and is held to the existing combined budget, with and without a worktree;
the Stop guard's block and the credential guard's deny carry ``ci_wait`` and ``credential_helper`` and are held to the text
they add. Raising a budget is a change to a pre-declared test criterion and is not this test's to do: if the measure here
does not fit, the caps shrink.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.hooks._audience import MAX_BRIEF_QUESTIONS, SETTINGS, provenance_label
from tests.hooks.test_subagent_start_byte_budget import (
    _REAL_SHAPE_PAYLOAD,
    _SUBAGENT_START_BUDGET_BYTES,
    _SUBAGENT_START_WORKTREE_BUDGET_BYTES,
    _assert_seeds_reached,
    _run_conexus,
    _run_sn,
)

_SCRIPTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"

#: The RDR's caps, in bytes of UTF-8. The test also reads them from the schema, so a change to either side fails here.
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


def test_the_fixture_is_at_every_cap_and_the_caps_are_the_schemas() -> None:
    """Non-vacuity: a key under its cap would let the budget pass on a smaller case than the schema allows."""
    for key, cap in _CAPS.items():
        assert SETTINGS[key].limit == cap, key
        assert len(str(_project_at_caps()[key]).encode("utf-8")) == cap, key
    assert SETTINGS["brief_questions"].limit == _QUESTION_CAP and MAX_BRIEF_QUESTIONS == 3
    questions = _project_at_caps()["brief_questions"] + _user_at_caps()["brief_questions"]
    assert len(questions) == MAX_BRIEF_QUESTIONS and len(set(questions)) == len(questions)
    assert all(len(q.encode("utf-8")) == _QUESTION_CAP and len(q) < _QUESTION_CAP for q in questions), "multi-byte"


def _project_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "capped-project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "project.json").write_text(json.dumps(_project_at_caps()), encoding="utf-8")
    return repo


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


@pytest.mark.t2_tenant
def test_every_key_at_its_cap_fits_the_subagent_start_budget(tmp_path: Path) -> None:
    repo = _project_repo(tmp_path)
    payload = json.dumps({**json.loads(_REAL_SHAPE_PAYLOAD), "cwd": str(repo)})
    conexus_ctx = _run_conexus(home=tmp_path / "home", cwd=str(repo), payload=payload, user_settings=_user_at_caps())
    _assert_seeds_reached(conexus_ctx)
    _assert_every_subagent_start_text_is_labelled(conexus_ctx)
    conexus_bytes = len(conexus_ctx.encode("utf-8"))
    sn_bytes = len(_run_sn().encode("utf-8"))
    total = conexus_bytes + sn_bytes
    assert total < _SUBAGENT_START_BUDGET_BYTES, (
        f"every key at its cap: combined SubagentStart total {total}B (conexus {conexus_bytes}B + sn {sn_bytes}B) "
        f">= budget {_SUBAGENT_START_BUDGET_BYTES}B; the caps shrink, the budget does not rise"
    )


@pytest.mark.t2_tenant
def test_every_key_at_its_cap_fits_the_worktree_budget(tmp_path: Path) -> None:
    repo = _project_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "add", ".nexus/project.json"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"], check=True)
    worktree = tmp_path / "agent-worktree-caps-probe"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(worktree), "-b", "wt-branch"], check=True)
    payload = json.dumps({**json.loads(_REAL_SHAPE_PAYLOAD), "cwd": str(worktree)})
    conexus_ctx = _run_conexus(home=tmp_path / "home", cwd=str(worktree), payload=payload, user_settings=_user_at_caps())
    _assert_seeds_reached(conexus_ctx)
    _assert_every_subagent_start_text_is_labelled(conexus_ctx)
    sn_ctx = _run_sn(payload=payload)
    assert "worktree" in sn_ctx.lower(), "the worktree path did not fire, so this would measure the smaller case"
    conexus_bytes = len(conexus_ctx.encode("utf-8"))
    sn_bytes = len(sn_ctx.encode("utf-8"))
    total = conexus_bytes + sn_bytes
    assert total < _SUBAGENT_START_WORKTREE_BUDGET_BYTES, (
        f"every key at its cap, worktree: combined total {total}B (conexus {conexus_bytes}B + sn {sn_bytes}B) "
        f">= budget {_SUBAGENT_START_WORKTREE_BUDGET_BYTES}B; the caps shrink, the budget does not rise"
    )


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_SCRIPTS))
    return module


#: What a label adds around a value (the longest, ``ci_wait``'s, measured at 121 bytes) with room for a re-wording.
_LABEL_ALLOWANCE_BYTES = 160


def test_the_stop_block_adds_no_more_than_the_ci_wait_cap_and_its_label() -> None:
    swg = _load("stop_wake_guard_at_caps", _SCRIPTS / "stop_wake_guard.py")
    clause = "The runs are still going."
    plain = swg.block_reason(swg.CLASS_CI_WAIT, clause)
    named = swg.block_reason(swg.CLASS_CI_WAIT, clause, ci_wait=_project_at_caps()["ci_wait"])
    added = len(named.encode("utf-8")) - len(plain.encode("utf-8"))
    assert _project_at_caps()["ci_wait"] in named, "the whole command is printed"
    assert 0 < added <= _CAPS["ci_wait"] + _LABEL_ALLOWANCE_BYTES, added


def test_the_credential_deny_adds_no_more_than_the_helper_cap_and_its_label(tmp_path: Path, plugin_account) -> None:
    guard = _load("credential_print_guard_at_caps", _SCRIPTS / "routing" / "credential_print_guard.py")
    repo = _project_repo(tmp_path)
    reason = guard._redirect({"cwd": str(repo)})
    refusal, _, line = reason.partition("\n")
    assert refusal == guard._REFUSAL
    assert _project_at_caps()["credential_helper"] in line, "the whole helper text is printed"
    assert len(line.encode("utf-8")) <= _CAPS["credential_helper"] + _LABEL_ALLOWANCE_BYTES, len(line.encode("utf-8"))
