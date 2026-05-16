# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``_pick_tier_b_dispatcher``.

Covers the precedence ladder:
  per-tool env override > global env + pin set > default ``claude``.

The pin set currently contains ``nx_plan_audit`` (spike-D v3 2026-05-16
showed qwen fabricates ``verification_method=filesystem`` with zero
tool calls).
"""
from __future__ import annotations

import pytest

from nexus.mcp.core import TIER_B_CLAUDE_PINNED, _pick_tier_b_dispatcher


def test_pin_set_contains_nx_plan_audit() -> None:
    assert "nx_plan_audit" in TIER_B_CLAUDE_PINNED


def test_default_env_returns_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest already strips NEXUS_TIER_B_* — explicit for clarity.
    monkeypatch.delenv("NEXUS_TIER_B_DISPATCHER", raising=False)
    assert _pick_tier_b_dispatcher("nx_enrich_beads") == "claude"
    assert _pick_tier_b_dispatcher("nx_tidy") == "claude"
    assert _pick_tier_b_dispatcher("nx_plan_audit") == "claude"


def test_global_qwen_routes_non_pinned_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_TIER_B_DISPATCHER", "qwen_agent")
    assert _pick_tier_b_dispatcher("nx_enrich_beads") == "qwen_agent"
    assert _pick_tier_b_dispatcher("nx_tidy") == "qwen_agent"


def test_global_qwen_skips_pinned_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin wins over global env for nx_plan_audit."""
    monkeypatch.setenv("NEXUS_TIER_B_DISPATCHER", "qwen_agent")
    assert _pick_tier_b_dispatcher("nx_plan_audit") == "claude"


def test_per_tool_override_beats_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-tool override is absolute — operators can route pinned
    tools through qwen for re-bench / experimentation.
    """
    monkeypatch.setenv("NEXUS_TIER_B_DISPATCHER", "qwen_agent")
    monkeypatch.setenv(
        "NEXUS_TIER_B_NX_PLAN_AUDIT_DISPATCHER", "qwen_agent",
    )
    assert _pick_tier_b_dispatcher("nx_plan_audit") == "qwen_agent"


def test_per_tool_override_can_opt_in_without_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-tool override works standalone — no global env needed."""
    monkeypatch.delenv("NEXUS_TIER_B_DISPATCHER", raising=False)
    monkeypatch.setenv("NEXUS_TIER_B_NX_TIDY_DISPATCHER", "qwen_agent")
    assert _pick_tier_b_dispatcher("nx_tidy") == "qwen_agent"
    # Sibling tools untouched.
    assert _pick_tier_b_dispatcher("nx_enrich_beads") == "claude"
    assert _pick_tier_b_dispatcher("nx_plan_audit") == "claude"


def test_per_tool_override_can_force_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-tool override is symmetric: a tool can be forced back to
    claude even when the global is qwen_agent.
    """
    monkeypatch.setenv("NEXUS_TIER_B_DISPATCHER", "qwen_agent")
    monkeypatch.setenv("NEXUS_TIER_B_NX_TIDY_DISPATCHER", "claude")
    assert _pick_tier_b_dispatcher("nx_tidy") == "claude"
