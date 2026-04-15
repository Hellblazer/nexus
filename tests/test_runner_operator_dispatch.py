# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-079 P4 — runner integration routing operator steps.

``_default_dispatcher`` now:
  * Maps plan-step operator names (``extract``, ``rank``, ``compare``,
    ``summarize``, ``generate``) to the ``operator_*`` MCP tools from
    RDR-079 P3.
  * Handles async MCP tools via a thread bridge (FastMCP runs sync MCP
    tools on its event loop; calling ``asyncio.run`` from within would
    deadlock — review finding C-1 in P3).
  * Leaves retrieval tools (search, query, traverse) and the generic
    sync-MCP-tool pass-through unchanged.
"""
from __future__ import annotations

import asyncio

import pytest


# ── Operator-name → operator_* MCP tool routing ────────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_routes_bare_extract_to_operator_extract(monkeypatch) -> None:
    """A plan step with ``tool: extract`` must invoke ``operator_extract``
    on the MCP core, not look up a non-existent ``extract`` attribute
    and raise PlanRunToolNotFoundError."""
    from nexus.plans.runner import _default_dispatcher
    from nexus.mcp import core as mcp_core

    calls: list[dict] = []

    async def fake_operator_extract(**kwargs):
        calls.append(kwargs)
        return {"extractions": [{"echo": kwargs.get("inputs", "")}]}

    monkeypatch.setattr(mcp_core, "operator_extract", fake_operator_extract)

    result = await _default_dispatcher(
        "extract",
        {"inputs": '["hello"]', "fields": "x,y"},
    )
    assert result == {"extractions": [{"echo": '["hello"]'}]}
    assert len(calls) == 1
    assert calls[0]["inputs"] == '["hello"]'


@pytest.mark.asyncio
async def test_dispatcher_routes_all_five_operator_names(monkeypatch) -> None:
    """All five RDR-079 P3 operator names map to their operator_*
    counterparts. Regression guard for the _OPERATOR_TOOL_MAP dict."""
    from nexus.plans.runner import _default_dispatcher
    from nexus.mcp import core as mcp_core

    called_tools: list[str] = []

    def make_stub(name):
        async def stub(**kw):
            called_tools.append(name)
            return {"ok": True}
        return stub

    for bare, full in (
        ("extract", "operator_extract"),
        ("rank", "operator_rank"),
        ("compare", "operator_compare"),
        ("summarize", "operator_summarize"),
        ("generate", "operator_generate"),
    ):
        monkeypatch.setattr(mcp_core, full, make_stub(full))

    for bare in ("extract", "rank", "compare", "summarize", "generate"):
        await _default_dispatcher(bare, {})

    assert called_tools == [
        "operator_extract", "operator_rank", "operator_compare",
        "operator_summarize", "operator_generate",
    ]


@pytest.mark.asyncio
async def test_dispatcher_async_tools_routed_via_thread_bridge(monkeypatch) -> None:
    """The thread bridge is triggered for ANY async tool, not only by
    name. An async tool that is not in _OPERATOR_TOOL_MAP but is
    accessible by its own name (e.g. a future async MCP tool) also
    works through the bridge — detected by inspect.iscoroutinefunction."""
    from nexus.plans.runner import _default_dispatcher
    from nexus.mcp import core as mcp_core

    async def future_async_tool(**kwargs):
        # Verify we're on a real event loop (not just a sync shim)
        assert asyncio.get_running_loop() is not None
        return {"result": "async-ok"}

    monkeypatch.setattr(mcp_core, "future_async_tool", future_async_tool, raising=False)

    result = await _default_dispatcher("future_async_tool", {})
    assert result == {"result": "async-ok"}


@pytest.mark.asyncio
async def test_dispatcher_sync_tools_still_use_direct_call(monkeypatch) -> None:
    """Non-async MCP tools (e.g. traverse, search without async-ification)
    must NOT go through the thread bridge — direct call preserves the
    existing zero-overhead path."""
    from nexus.plans.runner import _default_dispatcher
    from nexus.mcp import core as mcp_core

    call_count = [0]

    def sync_tool(**kwargs):
        call_count[0] += 1
        return {"sync": True}

    monkeypatch.setattr(mcp_core, "sync_tool", sync_tool, raising=False)

    result = await _default_dispatcher("sync_tool", {})
    assert result == {"sync": True}
    assert call_count[0] == 1


@pytest.mark.asyncio
async def test_dispatcher_propagates_async_exception(monkeypatch) -> None:
    """When the async tool raises, the error must surface to the caller,
    not be swallowed by the thread bridge."""
    from nexus.plans.runner import _default_dispatcher
    from nexus.mcp import core as mcp_core
    from nexus.plans.runner import PlanRunOperatorOutputError

    async def failing_extract(**kwargs):
        raise PlanRunOperatorOutputError(
            operator="extract", reason="induced failure",
        )

    monkeypatch.setattr(mcp_core, "operator_extract", failing_extract)

    with pytest.raises(PlanRunOperatorOutputError, match="induced failure"):
        await _default_dispatcher("extract", {"inputs": "[]", "fields": "x"})


# ── Unknown tool name: existing behavior preserved ─────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_raises_tool_not_found_for_unknown_bare_name() -> None:
    """A bare name that's neither an operator nor a known MCP tool must
    still raise PlanRunToolNotFoundError (not AttributeError, not
    silently succeed via mapping)."""
    from nexus.plans.runner import _default_dispatcher, PlanRunToolNotFoundError

    with pytest.raises(PlanRunToolNotFoundError):
        await _default_dispatcher("not_a_real_tool_xyz", {})


# ── Retrieval tools unaffected by operator routing ─────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_still_passes_structured_true_for_retrieval_tools(
    monkeypatch,
) -> None:
    """P1 invariant: retrieval tools (search/query) auto-receive
    structured=True. P4 adds operator routing but must not regress this."""
    from nexus.plans.runner import _default_dispatcher
    from nexus.mcp import core as mcp_core

    received: dict = {}

    def fake_search(**kwargs):
        received.update(kwargs)
        return {"ids": [], "tumblers": [], "distances": [], "collections": []}

    monkeypatch.setattr(mcp_core, "search", fake_search)

    await _default_dispatcher("search", {"query": "x"})
    assert received.get("structured") is True


# ── End-to-end composition: plan_run with an operator step ────────────────


@pytest.mark.asyncio
async def test_plan_run_executes_operator_step_end_to_end(monkeypatch) -> None:
    """End-to-end smoke: a plan with an ``extract`` step routes through
    the default dispatcher → operator_extract (stubbed async) → returns
    the dict to the step-outputs list.

    This is the test that composes RDR-079 P1+P3+P4 in one place."""
    from nexus.plans.runner import plan_run
    from nexus.plans.match import Match
    from nexus.mcp import core as mcp_core
    import json as _json

    async def fake_operator_extract(**kwargs):
        return {"extractions": [{"echo": kwargs.get("inputs", "")}]}

    async def fake_operator_rank(**kwargs):
        return {"ranked": [{"rank": 1, "score": 0.9, "input_index": 0,
                            "justification": "only item"}]}

    monkeypatch.setattr(mcp_core, "operator_extract", fake_operator_extract)
    monkeypatch.setattr(mcp_core, "operator_rank", fake_operator_rank)

    plan = {
        "steps": [
            {"tool": "extract", "args": {"inputs": '["foo"]', "fields": "x"}},
            {"tool": "rank", "args": {"criterion": "c", "inputs": '["a"]'}},
        ],
    }
    match = Match(
        plan_id=1, name="test", description="e2e",
        confidence=0.9, dimensions={},
        tags="", plan_json=_json.dumps(plan),
        required_bindings=[], optional_bindings=[],
        default_bindings={}, parent_dims=None,
    )
    result = await plan_run(match, {})
    assert len(result.steps) == 2
    assert "extractions" in result.steps[0]
    assert "ranked" in result.steps[1]
