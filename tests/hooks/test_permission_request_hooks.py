# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for PermissionRequest auto-approve hooks (nx + sn plugins).

Both hooks must:
1. Output valid JSON with hookSpecificOutput.decision.behavior = "allow" for matching tools
2. Output nothing (empty stdout) for non-matching tools
3. Agree on the output format
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

NX_SCRIPT = Path(__file__).resolve().parents[2] / "nx" / "hooks" / "scripts" / "auto-approve-nx-mcp.sh"
SN_SCRIPT = Path(__file__).resolve().parents[2] / "sn" / "hooks" / "scripts" / "auto-approve-sn-mcp.sh"


def _run_hook(script: Path, tool_name: str) -> str:
    """Pipe a PermissionRequest payload into a hook script, return stdout."""
    payload = json.dumps({"tool_name": tool_name})
    result = subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Hook failed: {result.stderr}"
    return result.stdout.strip()


def _parse_decision(output: str) -> str | None:
    """Extract behavior from hook output, or None if empty."""
    if not output:
        return None
    data = json.loads(output)
    return data["hookSpecificOutput"]["decision"]["behavior"]


# ── nx plugin hook ───────────────────────────────────────────────────────────


class TestNxPermissionHook:
    """nx plugin auto-approves mcp__plugin_nx_* tools."""

    def test_approves_nexus_catalog_tool(self) -> None:
        output = _run_hook(NX_SCRIPT, "mcp__plugin_nx_nexus-catalog__catalog_search")
        assert _parse_decision(output) == "allow"

    def test_approves_nexus_search_tool(self) -> None:
        output = _run_hook(NX_SCRIPT, "mcp__plugin_nx_nexus__search")
        assert _parse_decision(output) == "allow"

    def test_approves_sequential_thinking(self) -> None:
        output = _run_hook(NX_SCRIPT, "mcp__plugin_nx_sequential-thinking__sequentialthinking")
        assert _parse_decision(output) == "allow"

    def test_ignores_sn_tools(self) -> None:
        output = _run_hook(NX_SCRIPT, "mcp__plugin_sn_serena__find_file")
        assert output == ""

    def test_ignores_unrelated_tools(self) -> None:
        output = _run_hook(NX_SCRIPT, "Bash")
        assert output == ""

    def test_output_is_valid_json(self) -> None:
        output = _run_hook(NX_SCRIPT, "mcp__plugin_nx_nexus__scratch")
        data = json.loads(output)
        assert "hookSpecificOutput" in data
        assert data["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"


# ── sn plugin hook ───────────────────────────────────────────────────────────


class TestSnPermissionHook:
    """sn plugin auto-approves mcp__plugin_sn_* tools."""

    def test_approves_serena_tool(self) -> None:
        output = _run_hook(SN_SCRIPT, "mcp__plugin_sn_serena__jet_brains_find_symbol")
        assert _parse_decision(output) == "allow"

    def test_approves_context7_tool(self) -> None:
        output = _run_hook(SN_SCRIPT, "mcp__plugin_sn_context7__resolve-library-id")
        assert _parse_decision(output) == "allow"

    def test_ignores_nx_tools(self) -> None:
        output = _run_hook(SN_SCRIPT, "mcp__plugin_nx_nexus__search")
        assert output == ""

    def test_ignores_unrelated_tools(self) -> None:
        output = _run_hook(SN_SCRIPT, "Read")
        assert output == ""

    def test_output_is_valid_json(self) -> None:
        output = _run_hook(SN_SCRIPT, "mcp__plugin_sn_serena__search_for_pattern")
        data = json.loads(output)
        assert "hookSpecificOutput" in data
        assert data["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"


# ── Cross-hook agreement ────────────────────────────────────────────────────


class TestHookAgreement:
    """Both hooks must produce identical output structure."""

    def test_same_decision_structure(self) -> None:
        """nx and sn hooks use the same JSON envelope for allow decisions."""
        nx_out = json.loads(_run_hook(NX_SCRIPT, "mcp__plugin_nx_nexus__search"))
        sn_out = json.loads(_run_hook(SN_SCRIPT, "mcp__plugin_sn_serena__find_file"))

        # Same top-level keys
        assert set(nx_out.keys()) == set(sn_out.keys())
        # Same nested structure
        assert set(nx_out["hookSpecificOutput"].keys()) == set(sn_out["hookSpecificOutput"].keys())
        # Same decision
        assert nx_out["hookSpecificOutput"]["decision"] == sn_out["hookSpecificOutput"]["decision"]

    def test_no_cross_approval(self) -> None:
        """nx hook doesn't approve sn tools, sn hook doesn't approve nx tools."""
        assert _run_hook(NX_SCRIPT, "mcp__plugin_sn_serena__find_file") == ""
        assert _run_hook(SN_SCRIPT, "mcp__plugin_nx_nexus__search") == ""

    def test_neither_approves_unknown(self) -> None:
        """Neither hook approves tools from unknown plugins."""
        assert _run_hook(NX_SCRIPT, "mcp__other_plugin__tool") == ""
        assert _run_hook(SN_SCRIPT, "mcp__other_plugin__tool") == ""
