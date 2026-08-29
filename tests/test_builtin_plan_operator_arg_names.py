# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-50l6y: builtin plan steps must pass operator arg names the
resolved MCP operator actually accepts.

``hybrid-factual-lookup.yml``'s ``generate`` step passed ``outline`` /
``with_citations``; ``operator_generate``'s real parameters are
``template`` / ``cited``. Neither alias is mapped anywhere in the
runner's ``_INPUTS_TARGET`` table, so the mis-named kwargs were
silently dropped by ``_default_dispatcher``'s kwargs-filter, leaving
``template`` unset and the step failing loud with
"operator_generate() missing 1 required positional argument:
'template'".

``review-default.yml`` and ``document-default.yml``'s ``compare``
steps have the same defect one level worse: they pass ``criterion``
where ``operator_compare``'s real parameter is ``focus``.
``focus`` defaults to ``""``, so the mis-named kwarg is dropped with
no error at all — the comparison silently runs unfocused.

These tests dispatch each step's ACTUAL args (as authored in the
builtin YAML) through ``plan_run``'s real dispatcher, so the runner's
arg-hydration + kwargs-drop pass is genuinely exercised end to end.
``claude_dispatch`` — the one call that would shell out to a real
``claude -p`` subprocess — is replaced with a recording fake so the
test stays fast and hermetic; everything upstream of it (arg mapping,
kwargs-drop, the nexus-nyry9.4 missing-required-arg check) runs for
real.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

_BUILTIN_DIR = Path(__file__).parent.parent / "conexus" / "plans" / "builtin"


def _load_step(filename: str, tool: str) -> dict[str, Any]:
    """Return the first step in *filename* whose ``tool`` is *tool*."""
    raw = yaml.safe_load((_BUILTIN_DIR / filename).read_text())
    steps = raw["plan_json"]["steps"]
    step = next(s for s in steps if s.get("tool") == tool)
    return step


def _single_step_match(tool: str, args: dict[str, Any]):
    from nexus.plans.match import Match

    plan = {"steps": [{"tool": tool, "args": args}]}
    return Match(
        plan_id=1, name="test", description="test", confidence=0.9,
        dimensions={"verb": "test"}, tags="", plan_json=json.dumps(plan),
        required_bindings=[], optional_bindings=[], default_bindings={},
        parent_dims=None,
    )


@pytest.mark.asyncio
async def test_hybrid_factual_lookup_generate_step_dispatches_template_and_cited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generate step's real args must reach operator_generate as
    template/cited. Before the fix this raised
    PlanRunOperatorArgMissingError(missing_arg='template') before ever
    reaching claude_dispatch — the assertion below would not even run.
    """
    from nexus.plans.runner import plan_run

    step = _load_step("hybrid-factual-lookup.yml", "generate")
    args = dict(step["args"])
    # $step5.ranked resolved to a static value: step-ref resolution is
    # covered elsewhere; this test is scoped to arg-name mapping.
    args["inputs"] = "ranked candidate text"

    calls: list[dict[str, Any]] = []

    async def _fake_claude_dispatch(prompt, schema, *, timeout=300.0, model=None, operator=None):
        calls.append({"prompt": prompt, "operator": operator})
        return {"output": "stub"}

    monkeypatch.setattr(
        "nexus.operators.dispatch.claude_dispatch", _fake_claude_dispatch,
    )

    await plan_run(_single_step_match("generate", args), {})

    assert len(calls) == 1
    assert calls[0]["operator"] == "operator_generate"
    assert "factual-answer" in calls[0]["prompt"]
    assert "citations" in calls[0]["prompt"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "expected_focus"),
    [
        ("review-default.yml", "decision-drift"),
        ("document-default.yml", "doc-coverage"),
    ],
)
async def test_compare_step_dispatches_with_focus(
    monkeypatch: pytest.MonkeyPatch, filename: str, expected_focus: str,
) -> None:
    """The compare step's real args must reach operator_compare as
    focus, not criterion. Before the fix criterion was silently
    dropped (focus defaults to ''), so the prompt carried no
    'Focus on: ...' clause at all — this assertion would have failed
    silently rather than erroring.
    """
    from nexus.plans.runner import plan_run

    step = _load_step(filename, "compare")
    args = dict(step["args"])
    args["inputs"] = "some content"

    calls: list[dict[str, Any]] = []

    async def _fake_claude_dispatch(prompt, schema, *, timeout=300.0, model=None, operator=None):
        calls.append({"prompt": prompt, "operator": operator})
        return {"comparison": "stub"}

    monkeypatch.setattr(
        "nexus.operators.dispatch.claude_dispatch", _fake_claude_dispatch,
    )

    await plan_run(_single_step_match("compare", args), {})

    assert len(calls) == 1
    assert calls[0]["operator"] == "operator_compare"
    assert f"Focus on: {expected_focus}." in calls[0]["prompt"]
