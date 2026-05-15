# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tier-B dispatcher routing for ``nx_enrich_beads``.

Asserts that:

* Default env → calls ``claude_dispatch`` (preserves prior behavior).
* ``NEXUS_TIER_B_DISPATCHER=qwen_agent`` → calls ``qwen_agent_dispatch``
  with ``extensions=["nx"]``, ``operator_name="nx_enrich_beads"``,
  and the bead's enrichment prompt.

Both dispatchers are mocked; no real backend is exercised.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_default_env_routes_to_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEXUS_TIER_B_DISPATCHER", raising=False)
    fake = AsyncMock(return_value={"enriched_description": "out"})
    with (
        patch("nexus.operators.dispatch.claude_dispatch", fake),
        patch(
            "nexus.operators.qwen_agent_dispatch.qwen_agent_dispatch",
            new=AsyncMock(side_effect=AssertionError("qwen_agent must not run")),
        ),
    ):
        from nexus.mcp.core import nx_enrich_beads
        # The FastMCP @mcp.tool() decorator wraps the function; reach
        # the original async via .fn (FastMCP convention).
        impl = getattr(nx_enrich_beads, "fn", nx_enrich_beads)
        result = await impl("a bead about parsing widgets")
    assert result == "out"
    assert fake.await_count == 1


@pytest.mark.asyncio
async def test_qwen_agent_env_routes_to_qwen_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_TIER_B_DISPATCHER", "qwen_agent")
    fake_qwen = AsyncMock(return_value={"enriched_description": "qwen-out"})
    fake_claude = AsyncMock(side_effect=AssertionError("claude must not run"))
    with (
        patch(
            "nexus.operators.qwen_agent_dispatch.qwen_agent_dispatch",
            fake_qwen,
        ),
        patch("nexus.operators.dispatch.claude_dispatch", fake_claude),
    ):
        from nexus.mcp.core import nx_enrich_beads
        impl = getattr(nx_enrich_beads, "fn", nx_enrich_beads)
        result = await impl("a bead about parsing widgets")

    assert result == "qwen-out"
    assert fake_qwen.await_count == 1
    _, kwargs = fake_qwen.call_args
    assert kwargs.get("extensions") == ["nx"]
    assert kwargs.get("operator_name") == "nx_enrich_beads"
    assert kwargs.get("max_tool_calls") == 50
    # Prompt forwarded positionally; schema is the second positional arg.
    args, _ = fake_qwen.call_args
    assert "parsing widgets" in args[0]
    assert isinstance(args[1], dict)
    assert "enriched_description" in args[1].get("properties", {})
