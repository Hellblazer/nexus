# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 2 review r1 F2 (nexus-rfvyy.7.11): a refused ``exclude`` read is a refusal, never an empty answer.

``SensitivityExcludeUnavailable`` (the engine cannot be shown to filter sensitive chunks) was re-raised on purpose
by the batched chunk fetch, then swallowed by ``_store_get_many_impl``'s outer ``except`` into
``{"contents": [], "error": ...}``. The runner never read ``error``, so an operator step got zero evidence and
``nx_answer`` answered "no evidence" for a corpus it was never allowed to read. Sam's ruling on nexus-rfvyy.3 is
that the read refuses. These tests use a collection handle that refuses the way the real client does and assert
that the plan FAILS, on the chash route, the tumbler route and an explicit ``store_get_many`` step, and that the
public tool's structured error names the refusal.
"""
from __future__ import annotations

import pytest

import nexus.db.http_vector_client as hvc
from nexus.mcp import core
from nexus.plans.runner import PlanRunRetrievalRefusedError
from tests.plans.test_nx_answer_sensitivity import (  # noqa: F401 — world is a fixture
    CAN,
    COL_BGE,
    P1,
    T_PLAIN1,
    T_SECRET,
    _run,
    _SEARCH,
    world,
)

pytestmark = pytest.mark.sensitivity_reads


class _RefusingRows:
    """A collection handle whose ``exclude`` read refuses, as the client does when the engine is not shown to filter."""

    def get(self, ids=None, *, sensitivity, **_kw):
        if sensitivity == "exclude":
            raise hvc.SensitivityExcludeUnavailable("sensitivity='exclude' read refused: test engine does not filter")
        return {"ids": [], "documents": [], "metadatas": []}


@pytest.fixture
def refusing(world, monkeypatch):
    monkeypatch.setattr(world.engine.client, "get_or_create_collection", lambda _name: _RefusingRows())
    return world


def test_the_impl_lets_the_refusal_propagate(refusing):
    with pytest.raises(hvc.SensitivityExcludeUnavailable):
        core._store_get_many_impl([P1], COL_BGE, structured=True, sensitivity="exclude")


def test_the_public_tool_returns_an_error_that_names_the_refusal(refusing):
    out = core.store_get_many([P1], COL_BGE, structured=True)

    assert isinstance(out, dict) and out["contents"] == []
    assert "SensitivityExcludeUnavailable" in out["error"] or "refused" in out["error"]
    assert "sensitivity" in out["error"]


def test_the_public_tool_in_text_mode_names_the_refusal(refusing):
    out = core.store_get_many([P1], COL_BGE)

    assert isinstance(out, str) and "sensitivity" in out and "refused" in out


@pytest.mark.asyncio
async def test_the_chash_route_fails_the_plan(refusing):
    plan = {"steps": [_SEARCH, {"tool": "summarize", "args": {"ids": "$step1.ids", "collections": "$step1.collections"}}]}

    with pytest.raises(PlanRunRetrievalRefusedError, match="sensitivity"):
        await _run(plan)
    assert not refusing.prompts, "no operator prompt was built from zero evidence"


@pytest.mark.asyncio
async def test_the_tumbler_route_fails_the_plan(refusing):
    plan = {"steps": [{"tool": "summarize", "args": {"ids": [T_PLAIN1, T_SECRET]}}]}

    with pytest.raises(PlanRunRetrievalRefusedError, match="sensitivity"):
        await _run(plan)
    assert not refusing.prompts


@pytest.mark.asyncio
async def test_an_explicit_store_get_many_step_fails_the_plan(refusing):
    plan = {"steps": [{"tool": "store_get_many", "args": {"ids": [CAN], "collections": COL_BGE}}]}

    with pytest.raises(PlanRunRetrievalRefusedError, match="sensitivity"):
        await _run(plan)


@pytest.mark.asyncio
async def test_a_search_tool_error_naming_the_refusal_fails_the_plan_instead_of_an_empty_result(monkeypatch):
    """The text a search/query tool boundary renders for the same refusal is not a plan-binding error."""
    from nexus.plans.runner import _default_dispatcher

    text = "Error: search failed: sensitivity='exclude' read refused: the engine does not filter"
    monkeypatch.setattr(core, "search", lambda **_kw: text)
    with pytest.raises(PlanRunRetrievalRefusedError, match="refused"):
        await _default_dispatcher("search", {"query": "x"})
