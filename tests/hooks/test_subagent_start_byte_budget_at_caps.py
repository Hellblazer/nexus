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

The two SubagentStart budget runs at the caps (plain and worktree) are inside the default-case tenants of
``test_subagent_start_byte_budget.py``, so the storage-touch tenant count does not grow; the fixtures are in
``budget_caps_harness.py``. This file keeps the fixture check and the two guard tests, which touch no tenant.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from nexus.hooks._audience import MAX_BRIEF_QUESTIONS, SETTINGS
from tests.hooks.budget_caps_harness import (
    _CAPS,
    _QUESTION_CAP,
    _project_at_caps,
    _project_repo,
    _user_at_caps,
)

_SCRIPTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"


def test_the_fixture_is_at_every_cap_and_the_caps_are_the_schemas() -> None:
    """Non-vacuity: a key under its cap would let the budget pass on a smaller case than the schema allows."""
    for key, cap in _CAPS.items():
        assert SETTINGS[key].limit == cap, key
        assert len(str(_project_at_caps()[key]).encode("utf-8")) == cap, key
    assert SETTINGS["brief_questions"].limit == _QUESTION_CAP and MAX_BRIEF_QUESTIONS == 3
    questions = _project_at_caps()["brief_questions"] + _user_at_caps()["brief_questions"]
    assert len(questions) == MAX_BRIEF_QUESTIONS and len(set(questions)) == len(questions)
    assert all(len(q.encode("utf-8")) == _QUESTION_CAP and len(q) < _QUESTION_CAP for q in questions), "multi-byte"


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
