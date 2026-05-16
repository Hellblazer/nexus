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
        "verdict": "pass", "findings": [], "summary": "s",
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
    assert "pass" in result
    assert fake.await_count == 1


@pytest.mark.asyncio
async def test_global_qwen_env_still_routes_to_claude_due_to_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nx_plan_audit is in TIER_B_CLAUDE_PINNED. Setting the global
    ``NEXUS_TIER_B_DISPATCHER=qwen_agent`` MUST NOT route this tool
    through qwen — pin wins. Operators opt back in per-tool only.
    """
    monkeypatch.setenv("NEXUS_TIER_B_DISPATCHER", "qwen_agent")
    monkeypatch.delenv("NEXUS_TIER_B_NX_PLAN_AUDIT_DISPATCHER", raising=False)
    fake_claude = AsyncMock(return_value={
        "verdict": "pass", "findings": [], "summary": "claude-ran",
    })
    fake_qwen = AsyncMock(side_effect=AssertionError(
        "qwen_agent must not run — nx_plan_audit is pinned to claude"
    ))
    with (
        patch("nexus.operators.dispatch.claude_dispatch", fake_claude),
        patch(
            "nexus.operators.qwen_agent_dispatch.qwen_agent_dispatch",
            new=fake_qwen,
        ),
    ):
        from nexus.mcp.core import nx_plan_audit
        impl = getattr(nx_plan_audit, "fn", nx_plan_audit)
        result = await impl('{"phases": []}')
    assert "claude-ran" in result
    assert fake_claude.await_count == 1


@pytest.mark.asyncio
async def test_per_tool_override_routes_to_qwen_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-tool override beats the pin set. Operators who want to
    re-bench or experiment can opt back into qwen explicitly.
    """
    monkeypatch.setenv("NEXUS_TIER_B_NX_PLAN_AUDIT_DISPATCHER", "qwen_agent")
    fake_qwen = AsyncMock(return_value={
        "verdict": "pass", "findings": [], "summary": "qwen-summary",
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
    # Structural-honesty enforcement: per-finding verification_method +
    # `skipped` verdict for prompt_only findings (PR follow-on to #810).
    assert "verification_method" in args[0]
    assert "prompt_only" in args[0]
    assert "HONESTY RULE" in args[0]
    assert "`skipped`" in args[0]
    # Schema carries the enum-bound verdict + per-finding shape.
    verdict_schema = args[1]["properties"]["verdict"]
    assert verdict_schema.get("enum") == ["pass", "fail", "partial", "skipped"]
    findings_item = args[1]["properties"]["findings"]["items"]
    assert "verification_method" in findings_item["required"]
    assert findings_item["properties"]["verification_method"]["enum"] == [
        "mcp_search", "filesystem", "prompt_only", "n/a",
    ]


@pytest.mark.asyncio
async def test_rendered_output_includes_verification_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator-visible rendering surfaces verification_method per finding."""
    monkeypatch.delenv("NEXUS_TIER_B_DISPATCHER", raising=False)
    fake = AsyncMock(return_value={
        "verdict": "skipped",
        "findings": [
            {
                "title": "phase-A file path",
                "severity": "info",
                "verification_method": "prompt_only",
            },
            {
                "title": "phase-B dependency",
                "severity": "warn",
                "verification_method": "mcp_search",
            },
        ],
        "summary": "two findings",
    })
    with patch("nexus.operators.dispatch.claude_dispatch", fake):
        from nexus.mcp.core import nx_plan_audit
        impl = getattr(nx_plan_audit, "fn", nx_plan_audit)
        result = await impl('{"phases": []}')
    assert "[info/prompt_only] phase-A file path" in result
    assert "[warn/mcp_search] phase-B dependency" in result
    assert "Verdict: skipped" in result
