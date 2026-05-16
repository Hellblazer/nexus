# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tier-B dispatcher routing for ``nx_tidy``.

Mirrors ``test_nx_enrich_beads_routing.py`` (PR #796). Asserts default
env routes to ``claude_dispatch``; ``NEXUS_TIER_B_DISPATCHER=qwen_agent``
routes to ``qwen_agent_dispatch`` with the expected kwargs.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_default_env_routes_to_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEXUS_TIER_B_DISPATCHER", raising=False)
    fake = AsyncMock(return_value={"summary": "out", "actions": []})
    with (
        patch("nexus.operators.dispatch.claude_dispatch", fake),
        patch(
            "nexus.operators.qwen_agent_dispatch.qwen_agent_dispatch",
            new=AsyncMock(side_effect=AssertionError("qwen_agent must not run")),
        ),
    ):
        from nexus.mcp.core import nx_tidy
        impl = getattr(nx_tidy, "fn", nx_tidy)
        result = await impl("chromadb quotas")
    assert "out" in result
    assert fake.await_count == 1


@pytest.mark.asyncio
async def test_qwen_agent_env_routes_to_qwen_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_TIER_B_DISPATCHER", "qwen_agent")
    fake_qwen = AsyncMock(return_value={"summary": "qwen-out", "actions": []})
    fake_claude = AsyncMock(side_effect=AssertionError("claude must not run"))
    with (
        patch(
            "nexus.operators.qwen_agent_dispatch.qwen_agent_dispatch",
            fake_qwen,
        ),
        patch("nexus.operators.dispatch.claude_dispatch", fake_claude),
    ):
        from nexus.mcp.core import nx_tidy
        impl = getattr(nx_tidy, "fn", nx_tidy)
        result = await impl("chromadb quotas")

    assert "qwen-out" in result
    assert fake_qwen.await_count == 1
    _, kwargs = fake_qwen.call_args
    assert kwargs.get("extensions") == ["nx"]
    assert kwargs.get("operator_name") == "nx_tidy"
    assert kwargs.get("max_tool_calls") == 50
    args, _ = fake_qwen.call_args
    assert "chromadb quotas" in args[0]
    assert isinstance(args[1], dict)
    assert "summary" in args[1].get("properties", {})
    # JSON-only trailer applied (#799 pattern).
    assert "ONLY a JSON object" in args[0]
