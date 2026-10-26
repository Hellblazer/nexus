# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jypmj.1.19: a ``$stepN.field`` the runner cannot substitute fails the run before dispatch.

The live canary's plan-miss run G19 handed ``generate`` the literal ``$step3.contents ... $step5.ids``
because ``_resolve_value`` substitutes a ref only when it is the whole string and never looks inside
a dict. Tenant-free: ``plan_run`` with stubbed MCP tools and the continuation composers.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from nexus.plans.runner import PlanRunStepRefError, plan_run


def _match(plan: dict, bindings: dict | None = None):
    from nexus.plans.match import Match

    return Match(
        plan_id=0, name="ad-hoc", description="", confidence=None, dimensions={}, tags="",
        plan_json=json.dumps(plan), required_bindings=["intent"], optional_bindings=[],
        default_bindings={"intent": "q", **(bindings or {})}, parent_dims=None,
    )


def _retrieval() -> list[dict]:
    return [
        {"tool": "search", "args": {"query": "q", "corpus": "code"}},
        {"tool": "search", "args": {"query": "r", "corpus": "code"}},
        {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "$step1.collections"}},
        {"tool": "store_get_many", "args": {"ids": "$step2.ids", "collections": "$step2.collections"}},
    ]


class _Stubs:
    """Records every dispatch so a test can assert nothing ran."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.generate_context: object = None

    def patches(self):
        from nexus.mcp import core as mcp_core

        async def search(**kwargs):  # noqa: ARG001
            self.calls.append("search")
            return {"ids": [f"{0xA000:064x}"], "tumblers": [], "distances": [], "collections": ["code__x"]}

        def get_many(ids, collections=None, structured=False):  # noqa: ARG001
            self.calls.append("store_get_many")
            return {"contents": ["CHUNK" for _ in ids], "missing": []}

        async def generate(**kwargs):
            self.calls.append("generate")
            self.generate_context = kwargs.get("context")
            return {"text": "ok"}

        return (
            patch.object(mcp_core, "search", search),
            patch.object(mcp_core, "store_get_many", get_many),
            patch.object(mcp_core, "operator_generate", generate),
            patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()),
        )


async def _run(plan_steps: list[dict], *, bindings: dict | None = None, bundle: bool = False) -> _Stubs:
    stubs = _Stubs()
    p1, p2, p3, p4 = stubs.patches()
    with p1, p2, p3, p4:
        await plan_run(_match({"steps": plan_steps}, bindings), bundle_operators=bundle)
    return stubs


G19_CONTEXTS = {
    "interpolated_string": "Code:\n$step3.contents\n\nManifest:\n$step4.contents\n\nIds: $step5.ids",
    "dict_of_refs": {"a": "$step3.contents", "b": "$step4.contents"},
    "ref_inside_a_list_string": ["$step3.contents", "see $step4.contents"],
    "dict_inside_a_list": ["$step3.contents", {"x": "$step4.contents"}],
    "nested_dict": {"a": {"b": ["$step3.contents"]}},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(G19_CONTEXTS))
async def test_an_unsubstitutable_ref_fails_before_any_step_dispatches(shape: str) -> None:
    steps = [
        *_retrieval(),
        {"tool": "search", "args": {"query": "d", "corpus": "rdr,docs"}},
        {"tool": "generate", "args": {"template": "explain", "context": G19_CONTEXTS[shape]}},
    ]
    stubs = _Stubs()
    p1, p2, p3, p4 = stubs.patches()
    with p1, p2, p3, p4, pytest.raises(PlanRunStepRefError) as err:
        await plan_run(_match({"steps": steps}), bundle_operators=False)
    assert "step 6" in err.value.reason and "'context'" in err.value.reason
    assert err.value.ref.startswith("$step")
    assert stubs.calls == [], "the plan is rejected before the first dispatch, not after the retrievals were paid for"


@pytest.mark.asyncio
async def test_the_bundle_path_rejects_it_too() -> None:
    steps = [
        *_retrieval(),
        {"tool": "summarize", "args": {"content": "$step3.contents"}},
        {"tool": "generate", "args": {"template": "t", "context": "Summary: $step5.summary"}},
    ]
    with pytest.raises(PlanRunStepRefError, match="step 6"):
        await _run(steps, bundle=True)


def test_resolve_args_is_the_backstop_for_a_step_nobody_pre_validated() -> None:
    """The continuation composers call ``_resolve_args`` on a plan's later steps without
    ``plan_run``'s pre-dispatch pass."""
    from nexus.plans.runner import _resolve_args

    with pytest.raises(PlanRunStepRefError, match="step 3"):
        _resolve_args({"context": "x $step1.contents"}, bindings={}, step_outputs=[{"contents": ["a"]}], step_index=2)


def test_the_continuation_composers_reject_it() -> None:
    from nexus.plans.continuation import classify_continuation_cut
    from nexus.plans.continuation_envelope import _compose_shape_a

    steps = [
        {"tool": "summarize", "args": {"content": ["a", "b"]}},
        {"tool": "generate", "args": {"template": "t", "context": "S: $step1.summary"}},
    ]
    cut = classify_continuation_cut(steps)
    with pytest.raises(PlanRunStepRefError, match="step 2"):
        _compose_shape_a(cut, steps, {}, [])


@pytest.mark.asyncio
async def test_a_user_question_that_quotes_a_step_ref_is_not_rejected() -> None:
    """``$intent`` resolves to the caller's text, which may hold ``$step1.ids``; only the plan's
    own raw args are scanned."""
    steps = [
        {"tool": "search", "args": {"query": "$intent", "corpus": "code"}},
        {"tool": "generate", "args": {"template": "answer $intent", "context": "$intent"}},
    ]
    stubs = await _run(steps, bindings={"intent": "what does $step1.ids mean in a plan"})
    assert stubs.calls == ["search", "generate"]
    assert stubs.generate_context == "what does $step1.ids mean in a plan"


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [
    "$step3.contents",
    ["$step3.contents", "$step4.contents"],
    "a literal with a dollar sign $5 and $step alone",
])
async def test_whole_value_refs_lists_and_plain_text_still_run(context) -> None:
    steps = [*_retrieval(), {"tool": "generate", "args": {"template": "t", "context": context}}]
    stubs = await _run(steps)
    assert stubs.calls[-1] == "generate"


def test_the_planner_prompt_states_the_one_ref_or_a_list_rule() -> None:
    from nexus.mcp.core import _PLANNER_TOOL_REFERENCE

    ref = _PLANNER_TOOL_REFERENCE
    assert "exactly one $stepN.<field> reference OR a JSON" in " ".join(ref.split())
    assert "inside other text" in ref and "inside an object" in ref
    assert "`context` is a\n        string" not in ref, "generate.context is no longer described as a plain string"
