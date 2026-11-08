# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 2 review r1 F1 (nexus-rfvyy.7.11): a plan step names a tool, and the runner may dispatch only a
registered MCP tool or an operator (``_OPERATOR_TOOL_MAP``). Before the fix the dispatcher did
``getattr(nexus.mcp.core, name)``, so a step could call a private helper (``_store_get_many_impl``,
``_search_render``, ``_batched_get_by_ids``, ``_tidy_prefetch``) that takes ``sensitivity=`` and hand it
``"include"``: the posture was enforced only by the schema's absence of the argument, which a private helper
does not share.
"""
from __future__ import annotations

import pytest

from nexus.mcp import core
from nexus.plans.runner import PlanRunToolNotFoundError, _default_dispatcher

PRIVATE_HELPERS = (
    "_store_get_many_impl",
    "_search_render",
    "_batched_get_by_ids",
    "_tidy_prefetch",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", PRIVATE_HELPERS)
async def test_a_private_helper_is_not_dispatchable(name: str, monkeypatch) -> None:
    called: list[dict] = []

    def spy(**kw):
        called.append(kw)
        return {}

    monkeypatch.setattr(core, name, spy)  # even a callable the dispatcher could resolve must be refused
    with pytest.raises(PlanRunToolNotFoundError):
        await _default_dispatcher(name, {"ids": ["x"], "sensitivity": "include"})
    assert called == []


@pytest.mark.asyncio
async def test_a_public_but_unregistered_module_attribute_is_not_dispatchable(monkeypatch) -> None:
    """``nexus.mcp.core`` also exposes plain public functions (imports, demoted helpers) that are not tools."""
    called: list[dict] = []
    monkeypatch.setattr(core, "not_a_registered_tool", lambda **kw: called.append(kw) or {}, raising=False)
    with pytest.raises(PlanRunToolNotFoundError):
        await _default_dispatcher("not_a_registered_tool", {})
    assert called == []


@pytest.mark.asyncio
async def test_a_registered_tool_still_dispatches(monkeypatch) -> None:
    seen: list[dict] = []

    async def fake_traverse(**kw):
        seen.append(kw)
        return {"ids": [], "tumblers": [], "collections": []}

    monkeypatch.setattr(core, "traverse", fake_traverse)
    assert "traverse" in {t.name for t in core.mcp._tool_manager.list_tools()}
    result = await _default_dispatcher("traverse", {"seeds": []})
    assert seen, "the registered tool was not called"
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_an_operator_name_still_resolves_past_the_allowlist(monkeypatch) -> None:
    from nexus.plans.runner import _OPERATOR_TOOL_MAP

    seen: list[dict] = []

    async def fake_operator(**kw):
        seen.append(kw)
        return {"text": "ok"}

    monkeypatch.setattr(core, _OPERATOR_TOOL_MAP["summarize"], fake_operator)
    await _default_dispatcher("summarize", {"content": "x"})
    assert seen
