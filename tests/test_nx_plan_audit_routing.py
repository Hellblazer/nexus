# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tier-B dispatcher routing for ``nx_plan_audit``.

Mirrors ``test_nx_enrich_beads_routing.py`` (PR #796).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_default_env_routes_to_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEXUS_TIER_B_DISPATCHER", raising=False)
    fake = AsyncMock(return_value={
        "verdict": "ok", "findings": [], "summary": "s",
    })
    with (
        patch("nexus.operators.dispatch.claude_dispatch", fake),
        patch(
            "nexus.operators.qwen_agent_dispatch.qwen_agent_dispatch",
            new=AsyncMock(side_effect=AssertionError("qwen_agent must not run")),
        ),
    ):
        from nexus.mcp.core import nx_plan_audit
        impl = getattr(nx_plan_audit, "fn", nx_plan_audit)
        result = await impl('{"phases": []}')
    assert "ok" in result
    assert fake.await_count == 1


@pytest.mark.asyncio
async def test_qwen_agent_env_routes_to_qwen_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_TIER_B_DISPATCHER", "qwen_agent")
    fake_qwen = AsyncMock(return_value={
        "verdict": "ok", "findings": [], "summary": "qwen-summary",
    })
    fake_claude = AsyncMock(side_effect=AssertionError("claude must not run"))
    with (
        patch(
            "nexus.operators.qwen_agent_dispatch.qwen_agent_dispatch",
            fake_qwen,
        ),
        patch("nexus.operators.dispatch.claude_dispatch", fake_claude),
    ):
        from nexus.mcp.core import nx_plan_audit
        impl = getattr(nx_plan_audit, "fn", nx_plan_audit)
        result = await impl('{"phases": [{"id": "p1"}]}')

    assert "qwen-summary" in result
    assert fake_qwen.await_count == 1
    _, kwargs = fake_qwen.call_args
    assert kwargs.get("extensions") == ["nx"]
    assert kwargs.get("operator_name") == "nx_plan_audit"
    assert kwargs.get("max_tool_calls") == 50
    args, _ = fake_qwen.call_args
    assert "p1" in args[0]
    assert isinstance(args[1], dict)
    assert "verdict" in args[1].get("properties", {})
    assert "ONLY a JSON object" in args[0]
    # Mandatory tool-use directive (spike_d follow-on): 0 tool calls
    # observed on 2/2 nx_plan_audit cases when prompt was soft.
    assert "You MUST call `mcp__nx__search`" in args[0]
    # `partial` verdict is gated on attempted-but-failed tool calls.
    assert "verdict=`partial`" in args[0]
