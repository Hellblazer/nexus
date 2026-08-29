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

NX_SCRIPT = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "auto-approve-nx-mcp.sh"
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


def _registered_conexus_tools() -> list[str]:
    """Full ``mcp__plugin_conexus_<server>__<tool>`` names for every tool the
    conexus MCP servers register.

    Enumerates the live FastMCP tool registries so the auto-approve allow-list
    is validated against what the servers ACTUALLY expose. This catches drift
    where a new tool ships without a hook entry and therefore prompts the user
    (the operator_filter/check/verify/groupby/aggregate gap, 2026-05-27).
    sequential-thinking is an external npx server (not introspectable here), so
    its single tool is appended as a known constant.
    """
    import importlib

    names: list[str] = []
    for module, server in (
        ("nexus.mcp.core", "nexus"),
        ("nexus.mcp.catalog", "nexus-catalog"),
    ):
        mcp = importlib.import_module(module).mcp
        for tool in mcp._tool_manager._tools:  # FastMCP registry
            names.append(f"mcp__plugin_conexus_{server}__{tool}")
    names.append("mcp__plugin_conexus_sequential-thinking__sequentialthinking")
    return sorted(names)


# ── conexus plugin hook ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool_name", _registered_conexus_tools())
def test_every_registered_conexus_tool_is_auto_approved(tool_name: str) -> None:
    """Drift guard: every tool the conexus MCP servers register MUST be
    auto-approved by auto-approve-nx-mcp.sh. A registered tool missing from the
    hook's explicit allow-list would make Claude Code prompt for permission.
    """
    output = _run_hook(NX_SCRIPT, tool_name)
    assert _parse_decision(output) == "allow", (
        f"{tool_name} is registered by an MCP server but NOT auto-approved by "
        f"{NX_SCRIPT.name} -- add it to the case list (it will prompt otherwise)."
    )


class TestNxPermissionHook:
    """conexus plugin auto-approves mcp__plugin_conexus_* tools."""

    def test_approves_nexus_catalog_tool(self) -> None:
        output = _run_hook(NX_SCRIPT, "mcp__plugin_conexus_nexus-catalog__search")
        assert _parse_decision(output) == "allow"

    def test_approves_nexus_search_tool(self) -> None:
        output = _run_hook(NX_SCRIPT, "mcp__plugin_conexus_nexus__search")
        assert _parse_decision(output) == "allow"

    def test_approves_sequential_thinking(self) -> None:
        output = _run_hook(NX_SCRIPT, "mcp__plugin_conexus_sequential-thinking__sequentialthinking")
        assert _parse_decision(output) == "allow"

    def test_ignores_sn_tools(self) -> None:
        output = _run_hook(NX_SCRIPT, "mcp__plugin_sn_serena__find_file")
        assert output == ""

    def test_ignores_unrelated_tools(self) -> None:
        output = _run_hook(NX_SCRIPT, "Bash")
        assert output == ""

    def test_output_is_valid_json(self) -> None:
        output = _run_hook(NX_SCRIPT, "mcp__plugin_conexus_nexus__scratch")
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
        output = _run_hook(SN_SCRIPT, "mcp__plugin_conexus_nexus__search")
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
        nx_out = json.loads(_run_hook(NX_SCRIPT, "mcp__plugin_conexus_nexus__search"))
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
        assert _run_hook(SN_SCRIPT, "mcp__plugin_conexus_nexus__search") == ""

    def test_neither_approves_unknown(self) -> None:
        """Neither hook approves tools from unknown plugins."""
        assert _run_hook(NX_SCRIPT, "mcp__other_plugin__tool") == ""
        assert _run_hook(SN_SCRIPT, "mcp__other_plugin__tool") == ""


def _run_pretooluse(script: Path, tool_name: str) -> str:
    """Pipe a PreToolUse payload (``hook_event_name`` set) into a hook script."""
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool_name})
    result = subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Hook failed: {result.stderr}"
    return result.stdout.strip()


def _pretooluse_matchers(hooks_json: Path) -> list[str]:
    data = json.loads(hooks_json.read_text())
    return [entry["matcher"] for entry in data["hooks"].get("PreToolUse", [])]


class TestPreToolUseApproval:
    """The same allowlists are registered on PreToolUse.

    PermissionRequest fires only when a permission PROMPT would be shown.
    Under ``defaultMode: auto`` the classifier decides unlisted tools first
    and never consults PermissionRequest (cc-validation scenario 16,
    measured), so a plugin approver on that event alone is inert on an
    auto-mode box: a directive-mandated tool such as sequential thinking
    was classifier-denied on 2026-08-28 with the approver installed.
    ``permissionDecision: allow`` on PreToolUse lands before the classifier.
    One script per plugin serves both events, so the allowlist has one home.
    """

    NX_HOOKS = NX_SCRIPT.parent.parent / "hooks.json"
    SN_HOOKS = SN_SCRIPT.parent.parent / "hooks.json"

    @pytest.mark.parametrize(
        ("script", "tool_name"),
        [
            (NX_SCRIPT, "mcp__plugin_conexus_sequential-thinking__sequentialthinking"),
            (NX_SCRIPT, "mcp__plugin_conexus_nexus__search"),
            (SN_SCRIPT, "mcp__plugin_sn_serena__jet_brains_find_symbol"),
            (SN_SCRIPT, "mcp__plugin_sn_context7__query-docs"),
        ],
    )
    def test_pretooluse_payload_yields_permission_decision_allow(
        self, script: Path, tool_name: str
    ) -> None:
        data = json.loads(_run_pretooluse(script, tool_name))
        out = data["hookSpecificOutput"]
        assert out["hookEventName"] == "PreToolUse"
        assert out["permissionDecision"] == "allow"
        assert "decision" not in out  # the PermissionRequest shape must not leak

    @pytest.mark.parametrize("script", [NX_SCRIPT, SN_SCRIPT])
    def test_pretooluse_payload_for_unlisted_tool_is_silent(self, script: Path) -> None:
        assert _run_pretooluse(script, "mcp__other_plugin__tool") == ""

    def test_permissionrequest_shape_unchanged_without_event_name(self) -> None:
        """A payload with no hook_event_name keeps the PermissionRequest shape."""
        for script, tool in (
            (NX_SCRIPT, "mcp__plugin_conexus_nexus__search"),
            (SN_SCRIPT, "mcp__plugin_sn_serena__jet_brains_find_symbol"),
        ):
            data = json.loads(_run_hook(script, tool))
            assert data["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
            assert data["hookSpecificOutput"]["decision"] == {"behavior": "allow"}

    def test_hooks_json_registers_the_approver_on_pretooluse(self) -> None:
        assert "mcp__plugin_conexus_.*" in _pretooluse_matchers(self.NX_HOOKS)
        assert "mcp__plugin_sn_.*" in _pretooluse_matchers(self.SN_HOOKS)

    def test_pretooluse_entry_runs_the_same_script_as_permissionrequest(self) -> None:
        for hooks_json, script_name in (
            (self.NX_HOOKS, "auto-approve-nx-mcp.sh"),
            (self.SN_HOOKS, "auto-approve-sn-mcp.sh"),
        ):
            data = json.loads(hooks_json.read_text())["hooks"]
            def commands(event: str) -> set[str]:
                return {
                    h["command"]
                    for entry in data.get(event, [])
                    if entry["matcher"].startswith("mcp__plugin_")
                    for h in entry["hooks"]
                }
            pre, perm = commands("PreToolUse"), commands("PermissionRequest")
            assert pre == perm, f"{hooks_json}: PreToolUse {pre} vs PermissionRequest {perm}"
            assert any(script_name in c for c in pre)
