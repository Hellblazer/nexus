# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-rjk2a: ``ci_status.PUSH_CANCELS_IN_PROGRESS`` must match the workflow YAMLs.

The fold reads a cancelled job in a started run as a time limit unless the
workflow cancels started runs on a push; which workflows do is a fact of their
``concurrency`` blocks. This lint derives that set from
``.github/workflows/*.yml`` and fails when the constant and the files disagree,
so a workflow that gains or loses ``cancel-in-progress`` cannot silently change
how ``ci_status.py`` reads its cancelled jobs.

``cancel-in-progress`` may be an expression. The one form in use is
``${{ github.event_name == 'pull_request' }}`` (Service CI); this file evaluates
``github.event_name`` ``==`` / ``!=`` a string literal for a ``push`` event, and
fails loudly on any other expression rather than guessing.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.lint

_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = _ROOT / ".github" / "workflows"

_spec = importlib.util.spec_from_file_location("ci_status", _ROOT / "scripts" / "ci_status.py")
assert _spec is not None and _spec.loader is not None
cs = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("ci_status", cs)
_spec.loader.exec_module(cs)

_EXPR = re.compile(r"^\$\{\{\s*github\.event_name\s*(==|!=)\s*'([^']*)'\s*\}\}$")


def evaluate_cancel_in_progress(value: object, event: str) -> bool:
    """*value* of ``cancel-in-progress`` for a run triggered by *event*."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        m = _EXPR.match(value.strip())
        if m:
            return (event == m.group(2)) == (m.group(1) == "==")
    raise AssertionError(
        f"cancel-in-progress {value!r} is an expression this lint cannot evaluate; teach "
        "evaluate_cancel_in_progress the form, do not guess (nexus-rjk2a)")


def _triggers(doc: dict) -> set[str]:
    on = doc.get(True, doc.get("on"))  # YAML 1.1 reads a bare `on` key as True
    if isinstance(on, dict):
        return set(on)
    if isinstance(on, list):
        return set(on)
    return {on} if isinstance(on, str) else set()


def derived_policy() -> tuple[set[str], int]:
    """(workflows whose push runs cancel in progress, workflows examined)."""
    names: set[str] = set()
    examined = 0
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        examined += 1
        if "push" not in _triggers(doc):
            continue
        conc = doc.get("concurrency")
        if isinstance(conc, dict) and evaluate_cancel_in_progress(conc.get("cancel-in-progress", False), "push"):
            names.add(str(doc["name"]))
    return names, examined


def test_the_policy_set_matches_the_workflow_yamls() -> None:
    derived, examined = derived_policy()
    assert examined >= 10, "non-vacuity: the scan found almost no workflows"
    assert derived, "non-vacuity: no workflow cancels in progress on push"
    assert set(cs.PUSH_CANCELS_IN_PROGRESS) == derived, (
        "scripts/ci_status.py PUSH_CANCELS_IN_PROGRESS disagrees with the workflow concurrency blocks: "
        f"only in the script {sorted(set(cs.PUSH_CANCELS_IN_PROGRESS) - derived)}, "
        f"only in the YAMLs {sorted(derived - set(cs.PUSH_CANCELS_IN_PROGRESS))}")


def test_service_ci_is_outside_the_set_because_its_cancel_is_pull_request_only() -> None:
    doc = yaml.safe_load((WORKFLOWS / "service-ci.yml").read_text(encoding="utf-8"))
    value = doc["concurrency"]["cancel-in-progress"]
    assert evaluate_cancel_in_progress(value, "pull_request") is True
    assert evaluate_cancel_in_progress(value, "push") is False
    assert doc["name"] not in cs.PUSH_CANCELS_IN_PROGRESS


@pytest.mark.parametrize(
    ("value", "event", "expected"),
    [(True, "push", True), (False, "push", False),
     ("${{ github.event_name == 'pull_request' }}", "push", False),
     ("${{ github.event_name == 'pull_request' }}", "pull_request", True),
     ("${{ github.event_name != 'push' }}", "push", False),
     ("${{ github.event_name != 'push' }}", "schedule", True)],
)
def test_the_expression_evaluator(value, event, expected) -> None:
    assert evaluate_cancel_in_progress(value, event) is expected


def test_an_expression_the_lint_cannot_evaluate_fails_loud() -> None:
    with pytest.raises(AssertionError, match="cannot evaluate"):
        evaluate_cancel_in_progress("${{ github.ref == 'refs/heads/main' }}", "push")
