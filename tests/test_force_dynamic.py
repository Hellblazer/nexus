# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-dslg: ``nx_answer(force_dynamic=True)`` skips plan_match.

RDR-090 P1.1. The bench harness's path C (forced-dynamic generation)
needs a way to bypass the plan-match gate without using ``scope`` as
a forced-miss proxy. The flag opts in to the inline-LLM-planner +
``plan_run`` dynamic path with no plan-library lookup.

Contracts pinned here:

  - ``force_dynamic=False`` (default): plan_match runs as today.
  - ``force_dynamic=True``: plan_match is NOT called — even when the
    library carries a matching plan, the dynamic planner runs.
  - ``force_dynamic=True`` with no matches AND no inline planner
    available behaves identically to the existing plan-miss path
    (the planner-failed fallback message).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.plans.match import Match


def _ad_hoc_match() -> Match:
    return Match(
        plan_id=0,
        name="ad-hoc",
        description="what is the meaning of life",
        confidence=None,
        dimensions={},
        tags="ad-hoc",
        plan_json=json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "$intent",
                                            "corpus": "knowledge"}},
                {"tool": "summarize", "args": {"content": "$step1.ids"}},
            ],
        }),
        required_bindings=["intent"],
        optional_bindings=[],
        default_bindings={"intent": "what is the meaning of life"},
        parent_dims=None,
    )


def _matched_plan(plan_id: int = 7) -> Match:
    return Match(
        plan_id=plan_id,
        name="library-plan",
        description="library plan",
        confidence=0.55,
        dimensions={},
        tags="builtin-template",
        plan_json=json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "$intent"}},
                {"tool": "summarize", "args": {"content": "$step1.ids"}},
            ],
        }),
        required_bindings=["intent"],
        optional_bindings=[],
        default_bindings={"intent": "q"},
        parent_dims=None,
    )


def _plan_run_success():
    result = MagicMock()
    result.steps = [{"text": "dynamic answer"}]
    return result


@pytest.mark.asyncio
async def test_default_calls_plan_match() -> None:
    """force_dynamic=False (default): plan_match is called once."""
    from nexus.mcp.core import nx_answer

    db_stub = MagicMock()
    db_stub.plans.save_plan = MagicMock(return_value=999)
    db_stub.plans.get_plan = MagicMock(return_value={"id": 999, "query": "q"})
    plan_match_mock = MagicMock(return_value=[_matched_plan(plan_id=7)])

    with patch("nexus.plans.matcher.plan_match", plan_match_mock), \
         patch("nexus.plans.runner.plan_run",
               AsyncMock(return_value=_plan_run_success())), \
         patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
        t2_ctx.return_value.__enter__.return_value = db_stub
        await nx_answer(question="some question")

    assert plan_match_mock.called, (
        "plan_match must run on the default code path"
    )


@pytest.mark.asyncio
async def test_force_dynamic_skips_plan_match() -> None:
    """force_dynamic=True: plan_match is not called; inline planner fires."""
    from nexus.mcp.core import nx_answer

    db_stub = MagicMock()
    db_stub.plans.save_plan = MagicMock(return_value=999)
    db_stub.plans.get_plan = MagicMock(return_value={"id": 999, "query": "q"})
    plan_match_mock = MagicMock(return_value=[_matched_plan(plan_id=7)])
    plan_miss_mock = AsyncMock(return_value=_ad_hoc_match())

    with patch("nexus.plans.matcher.plan_match", plan_match_mock), \
         patch("nexus.mcp.core._nx_answer_plan_miss", plan_miss_mock), \
         patch("nexus.plans.runner.plan_run",
               AsyncMock(return_value=_plan_run_success())), \
         patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
        t2_ctx.return_value.__enter__.return_value = db_stub
        await nx_answer(question="some question", force_dynamic=True)

    assert not plan_match_mock.called, (
        "plan_match must NOT run when force_dynamic=True"
    )
    assert plan_miss_mock.called, (
        "inline planner must run on force_dynamic=True"
    )


@pytest.mark.asyncio
async def test_force_dynamic_ignores_lib_plans_even_when_present() -> None:
    """force_dynamic=True wins even when a library plan would match.

    Provided as a regression test against a future refactor that
    might "optimize" the flag away when matches are available.
    """
    from nexus.mcp.core import nx_answer

    db_stub = MagicMock()
    db_stub.plans.save_plan = MagicMock(return_value=999)
    db_stub.plans.get_plan = MagicMock(return_value={"id": 999, "query": "q"})

    # Even if plan_match WERE called, it would return a matching plan.
    # The flag must short-circuit before that observation is possible.
    plan_match_mock = MagicMock(return_value=[_matched_plan(plan_id=7)])
    plan_miss_mock = AsyncMock(return_value=_ad_hoc_match())
    plan_run_mock = AsyncMock(return_value=_plan_run_success())

    with patch("nexus.plans.matcher.plan_match", plan_match_mock), \
         patch("nexus.mcp.core._nx_answer_plan_miss", plan_miss_mock), \
         patch("nexus.plans.runner.plan_run", plan_run_mock), \
         patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
        t2_ctx.return_value.__enter__.return_value = db_stub
        await nx_answer(question="some question", force_dynamic=True)

    # The matched plan's plan_id was 7. Verify the path that ran was
    # the dynamic one — best.plan_id == 0 (ad-hoc) — by checking that
    # save_plan was called with the ad-hoc tags (RDR-084 plan-grow
    # path), not the matched-plan path which never saves.
    assert db_stub.plans.save_plan.called, (
        "ad-hoc save path must be taken; matched-plan path skips save"
    )
    save_kwargs = db_stub.plans.save_plan.call_args.kwargs
    assert "ad-hoc" in (save_kwargs.get("tags") or ""), (
        "save_plan must carry the ad-hoc tag, proving dynamic path "
        f"was chosen; got tags={save_kwargs.get('tags')!r}"
    )
