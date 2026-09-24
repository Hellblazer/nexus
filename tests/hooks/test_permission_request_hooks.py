# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for PermissionRequest auto-approve hooks (nx + sn plugins).

Both hooks must:
1. Output valid JSON with hookSpecificOutput.decision.behavior = "allow" for matching tools
2. Output nothing (empty stdout) for non-matching tools
3. Agree on the output format

**RDR-215 bead nexus-q02nx.4 retargeted the nx half of this file.** The nx
plugin's decision logic no longer lives in
``conexus/hooks/scripts/auto-approve-nx-mcp.sh``: it is ported to
``nexus.hooks.auto_approve.run()``, registered as the ``hook_auto_approve``
tool on the live ``nx-mcp`` server (``nexus.mcp.hooks.HOOK_TOOLS``). Every nx
assertion below drives that tool through the real server's in-process
dispatch (:func:`_run_nx_hook`) rather than spawning a script -- "the
registered tool through the server's in-process dispatch" the RDR's Tests
section calls for. Bead nexus-q02nx.21 re-declared both the PreToolUse and
PermissionRequest ``hooks.json`` entries to that mcp_tool and deleted the
bash script; the byte-for-byte parity class that used to compare the port
against it (``TestNxPortMatchesBashByteForByte``) is gone with it, its
scenarios already covered directly elsewhere in this file. The sn plugin is
sn followed at bead nexus-q02nx.23: its wrapper is gone too, so every sn
assertion here now runs ``auto_approve_sn_mcp.py`` directly, argv-free,
under this interpreter, standing in for the one ``uv run`` picks in the
exec form ``sn/hooks/hooks.json`` declares (nexus-j4iy0), so the snapshot
path resolves the way it does in production.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

NX_SCRIPT = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "auto-approve-nx-mcp.sh"
SN_SCRIPT = Path(__file__).resolve().parents[2] / "sn" / "hooks" / "scripts" / "auto_approve_sn_mcp.py"


def _run_hook(script: Path, tool_name: str) -> str:
    """Pipe a PermissionRequest payload into a hook script, return stdout."""
    payload = json.dumps({"tool_name": tool_name})
    result = subprocess.run(
        [sys.executable, str(script)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Hook failed: {result.stderr}"
    return result.stdout.strip()


def _run_nx_hook(tool_name: str, hook_event_name: str | None = None) -> str:
    """Call the ported ``hook_auto_approve`` tool on the REAL, live
    ``nx-mcp`` server (``nexus.mcp.core.mcp``) through its own in-process
    ``call_tool`` dispatch -- the same path a ``tools/call`` request over
    stdio takes, minus the transport. ``hook_event_name`` omitted matches
    the bash script's own default (absent stdin field -> ``PermissionRequest``,
    see ``nexus.hooks.auto_approve.run``'s docstring).
    """
    core_mcp = importlib.import_module("nexus.mcp.core").mcp
    kwargs: dict[str, str] = {"tool_name": tool_name}
    if hook_event_name is not None:
        kwargs["hook_event_name"] = hook_event_name
    result = asyncio.run(core_mcp.call_tool("hook_auto_approve", kwargs))
    return result.content[0].text if result.content else ""


def _parse_decision(output: str) -> str | None:
    """Extract behavior from hook output, or None if empty."""
    if not output:
        return None
    data = json.loads(output)
    return data["hookSpecificOutput"]["decision"]["behavior"]


# nexus-cnzei.5: destructiveHint tools deliberately EXCLUDED from
# auto-approval — see auto-approve-nx-mcp.sh's own header comment for the
# rationale on each. A tool landing here must also gain a
# test_*_requires_manual_approval case below, so the exemption from
# test_every_registered_conexus_tool_is_auto_approved is never silent.
_MANUAL_APPROVAL_REQUIRED = {
    "mcp__plugin_conexus_nexus__daemon_uninstall",
}


def _registered_conexus_tools() -> list[str]:
    """Full ``mcp__plugin_conexus_<server>__<tool>`` names for every tool the
    conexus MCP servers register.

    Enumerates the live FastMCP tool registries so the auto-approve allow-list
    is validated against what the servers ACTUALLY expose. This catches drift
    where a new tool ships without a hook entry and therefore prompts the user
    (the operator_filter/check/verify/groupby/aggregate gap, 2026-05-27).
    sequential-thinking is an external npx server (not introspectable here), so
    its single tool is appended as a known constant. Excludes
    ``_MANUAL_APPROVAL_REQUIRED`` — those are asserted NOT auto-approved
    instead (see ``TestDestructiveToolsRequireManualApproval``).
    """
    names: list[str] = []
    for module, server in (
        ("nexus.mcp.core", "nexus"),
        ("nexus.mcp.catalog", "nexus-catalog"),
    ):
        mcp = importlib.import_module(module).mcp
        for tool in mcp._tool_manager._tools:  # FastMCP registry
            names.append(f"mcp__plugin_conexus_{server}__{tool}")
    names.append("mcp__plugin_conexus_sequential-thinking__sequentialthinking")
    return sorted(n for n in names if n not in _MANUAL_APPROVAL_REQUIRED)


# ── conexus plugin hook ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool_name", _registered_conexus_tools())
def test_every_registered_conexus_tool_is_auto_approved(tool_name: str) -> None:
    """Drift guard: every tool the conexus MCP servers register MUST be
    auto-approved by the ported hook_auto_approve tool. A registered tool
    missing from the allowlist would make Claude Code prompt for permission.

    Includes hook_auto_approve itself once it registers -- see
    nexus.hooks.auto_approve's module docstring for why the port adds a
    generic hook_-prefix carve-out the frozen bash script never had.
    """
    output = _run_nx_hook(tool_name)
    assert _parse_decision(output) == "allow", (
        f"{tool_name} is registered by an MCP server but NOT auto-approved by "
        "nexus.hooks.auto_approve -- add it to _ALLOWED_TOOLS (it will prompt "
        "otherwise)."
    )


class TestNxPermissionHook:
    """conexus plugin auto-approves mcp__plugin_conexus_* tools."""

    def test_approves_nexus_catalog_tool(self) -> None:
        output = _run_nx_hook("mcp__plugin_conexus_nexus-catalog__search")
        assert _parse_decision(output) == "allow"

    def test_approves_nexus_search_tool(self) -> None:
        output = _run_nx_hook("mcp__plugin_conexus_nexus__search")
        assert _parse_decision(output) == "allow"

    def test_approves_sequential_thinking(self) -> None:
        output = _run_nx_hook("mcp__plugin_conexus_sequential-thinking__sequentialthinking")
        assert _parse_decision(output) == "allow"

    def test_approves_hook_tools(self) -> None:
        """RDR-215 Cross-Cutting: the auto-approve matcher covers hook_
        tools themselves, since MCP cannot hide them from the model's
        tool list."""
        output = _run_nx_hook("mcp__plugin_conexus_nexus__hook_auto_approve")
        assert _parse_decision(output) == "allow"

    def test_ignores_sn_tools(self) -> None:
        output = _run_nx_hook("mcp__plugin_sn_serena__find_file")
        assert output == ""

    def test_ignores_unrelated_tools(self) -> None:
        output = _run_nx_hook("Bash")
        assert output == ""

    def test_output_is_valid_json(self) -> None:
        output = _run_nx_hook("mcp__plugin_conexus_nexus__scratch")
        data = json.loads(output)
        assert "hookSpecificOutput" in data
        assert data["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"


class TestDestructiveToolsRequireManualApproval:
    """nexus-cnzei.5: a destructive tool with a trivial self-gate (confirm=true
    satisfied by the calling agent itself, not a human) must stay behind the
    normal Claude Code permission prompt — never silently allowed.
    """

    def test_registry_names_at_least_one_tool(self) -> None:
        """Non-vacuity: this exemption set must name something real, or the
        drift guard above would be silently exempting nothing."""
        assert _MANUAL_APPROVAL_REQUIRED

    @pytest.mark.parametrize("tool_name", sorted(_MANUAL_APPROVAL_REQUIRED))
    def test_registered_tool_is_not_auto_approved_on_permissionrequest(
        self, tool_name: str
    ) -> None:
        output = _run_nx_hook(tool_name)
        assert output == "", (
            f"{tool_name} is in _MANUAL_APPROVAL_REQUIRED but nexus.hooks.auto_approve "
            f"still approves it on PermissionRequest — the allowlist exclusion "
            f"regressed."
        )

    @pytest.mark.parametrize("tool_name", sorted(_MANUAL_APPROVAL_REQUIRED))
    def test_registered_tool_is_not_auto_approved_on_pretooluse(
        self, tool_name: str
    ) -> None:
        output = _run_nx_hook(tool_name, hook_event_name="PreToolUse")
        assert output == "", (
            f"{tool_name} is in _MANUAL_APPROVAL_REQUIRED but nexus.hooks.auto_approve "
            f"still approves it on PreToolUse — the allowlist exclusion "
            f"regressed."
        )

    def test_exempted_tools_are_still_actually_registered(self) -> None:
        """A name in _MANUAL_APPROVAL_REQUIRED that no longer exists on any
        server would silently stop exempting anything real — catch a rename
        or deletion here rather than the drift guard quietly widening."""
        all_registered: set[str] = set()
        for module, server in (
            ("nexus.mcp.core", "nexus"),
            ("nexus.mcp.catalog", "nexus-catalog"),
        ):
            mcp = importlib.import_module(module).mcp
            for tool in mcp._tool_manager._tools:
                all_registered.add(f"mcp__plugin_conexus_{server}__{tool}")
        missing = _MANUAL_APPROVAL_REQUIRED - all_registered
        assert not missing, f"exempted tool(s) no longer registered: {missing}"


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

    def test_ignores_context_excluded_serena_tool(self) -> None:
        """search_for_pattern is excluded by the claude-code context; no wildcard approves it."""
        output = _run_hook(SN_SCRIPT, "mcp__plugin_sn_serena__search_for_pattern")
        assert output == ""

    def test_approves_every_snapshot_tool(self) -> None:
        """The allowlist is the generated snapshot, not a hand-kept case list (nexus-jbt5x)."""
        snapshot = SN_SCRIPT.parent / "serena-tools.txt"
        names = [l.strip() for l in snapshot.read_text().splitlines() if l.strip() and not l.startswith("#")]
        assert len(names) > 20
        for name in names:
            assert _parse_decision(_run_hook(SN_SCRIPT, f"mcp__plugin_sn_serena__{name}")) == "allow", name

    def test_output_is_valid_json(self) -> None:
        output = _run_hook(SN_SCRIPT, "mcp__plugin_sn_serena__replace_in_files")
        data = json.loads(output)
        assert "hookSpecificOutput" in data
        assert data["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"


# ── Cross-hook agreement ────────────────────────────────────────────────────


class TestHookAgreement:
    """Both hooks must produce identical output structure."""

    def test_same_decision_structure(self) -> None:
        """nx and sn hooks use the same JSON envelope for allow decisions."""
        nx_out = json.loads(_run_nx_hook("mcp__plugin_conexus_nexus__search"))
        sn_out = json.loads(_run_hook(SN_SCRIPT, "mcp__plugin_sn_serena__jet_brains_find_symbol"))

        # Same top-level keys
        assert set(nx_out.keys()) == set(sn_out.keys())
        # Same nested structure
        assert set(nx_out["hookSpecificOutput"].keys()) == set(sn_out["hookSpecificOutput"].keys())
        # Same decision
        assert nx_out["hookSpecificOutput"]["decision"] == sn_out["hookSpecificOutput"]["decision"]

    def test_no_cross_approval(self) -> None:
        """nx hook doesn't approve sn tools, sn hook doesn't approve nx tools."""
        assert _run_nx_hook("mcp__plugin_sn_serena__find_file") == ""
        assert _run_hook(SN_SCRIPT, "mcp__plugin_conexus_nexus__search") == ""

    def test_neither_approves_unknown(self) -> None:
        """Neither hook approves tools from unknown plugins."""
        assert _run_nx_hook("mcp__other_plugin__tool") == ""
        assert _run_hook(SN_SCRIPT, "mcp__other_plugin__tool") == ""


def _run_pretooluse(script: Path, tool_name: str) -> str:
    """Pipe a PreToolUse payload (``hook_event_name`` set) into a hook script."""
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool_name})
    result = subprocess.run(
        [sys.executable, str(script)],
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

    # NX_SCRIPT is deleted (RDR-215 bead nexus-q02nx.21); used here only as
    # a path anchor for hooks.json's directory, never read.
    NX_HOOKS = NX_SCRIPT.parent.parent / "hooks.json"
    SN_HOOKS = SN_SCRIPT.parent.parent / "hooks.json"

    #: Sentinel routing an (nx | sn) parametrized case at the ported tool
    #: rather than a script path -- nx has no script left to spawn here.
    NX = "nx"

    @pytest.mark.parametrize(
        ("target", "tool_name"),
        [
            (NX, "mcp__plugin_conexus_sequential-thinking__sequentialthinking"),
            (NX, "mcp__plugin_conexus_nexus__search"),
            (SN_SCRIPT, "mcp__plugin_sn_serena__jet_brains_find_symbol"),
            (SN_SCRIPT, "mcp__plugin_sn_context7__query-docs"),
        ],
    )
    def test_pretooluse_payload_yields_permission_decision_allow(
        self, target: str | Path, tool_name: str
    ) -> None:
        raw = (
            _run_nx_hook(tool_name, hook_event_name="PreToolUse")
            if target == self.NX
            else _run_pretooluse(target, tool_name)
        )
        data = json.loads(raw)
        out = data["hookSpecificOutput"]
        assert out["hookEventName"] == "PreToolUse"
        assert out["permissionDecision"] == "allow"
        assert "decision" not in out  # the PermissionRequest shape must not leak

    @pytest.mark.parametrize("target", [NX, SN_SCRIPT])
    def test_pretooluse_payload_for_unlisted_tool_is_silent(self, target: str | Path) -> None:
        output = (
            _run_nx_hook("mcp__other_plugin__tool", hook_event_name="PreToolUse")
            if target == self.NX
            else _run_pretooluse(target, "mcp__other_plugin__tool")
        )
        assert output == ""

    def test_permissionrequest_shape_unchanged_without_event_name(self) -> None:
        """A payload with no hook_event_name keeps the PermissionRequest shape."""
        nx_data = json.loads(_run_nx_hook("mcp__plugin_conexus_nexus__search"))
        sn_data = json.loads(_run_hook(SN_SCRIPT, "mcp__plugin_sn_serena__jet_brains_find_symbol"))
        for data in (nx_data, sn_data):
            assert data["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
            assert data["hookSpecificOutput"]["decision"] == {"behavior": "allow"}

    def test_hooks_json_registers_the_approver_on_pretooluse(self) -> None:
        assert "mcp__plugin_conexus_.*" in _pretooluse_matchers(self.NX_HOOKS)
        assert "mcp__plugin_sn_.*" in _pretooluse_matchers(self.SN_HOOKS)

    def test_pretooluse_entry_runs_the_same_handler_as_permissionrequest(self) -> None:
        """Each plugin's PreToolUse and PermissionRequest entries must
        invoke the identical handler — exec-form ``uv run ... <script>`` for sn
        (RDR-215 bead nexus-q02nx.23; launcher nexus-j4iy0), ``nx-hook auto-approve`` for nx.

        The nx side has now been through three shapes: a bash ``command``
        string, then the ``hook_auto_approve`` mcp_tool at bead
        nexus-q02nx.21, and back to the command tier at bead nexus-17i1n
        because an ``mcp_tool`` hook cannot return a verdict and the
        approver's whole output is one. The comparison itself never
        cared which shape; only the expected value did, so that is all
        that moves here.

        The sn side joins ``command`` with ``args``. Under exec form the
        ``command`` alone is the bare word ``uv`` for BOTH events, so
        comparing it would report agreement no matter which scripts the two
        entries named — the equality would hold vacuously."""

        def handlers(hooks_json: Path, event: str) -> set[str]:
            data = json.loads(hooks_json.read_text())["hooks"]
            out: set[str] = set()
            for entry in data.get(event, []):
                if not entry["matcher"].startswith("mcp__plugin_"):
                    continue
                for h in entry["hooks"]:
                    if h.get("type") == "mcp_tool":
                        out.add(h["tool"])
                    else:
                        out.add(" ".join([h["command"], *h.get("args", [])]))
            return out

        nx_pre, nx_perm = handlers(self.NX_HOOKS, "PreToolUse"), handlers(self.NX_HOOKS, "PermissionRequest")
        # Through the nx-hook shim since nexus-rcoze: a 7.55.0 CLI does not
        # register auto-approve and a direct entry exits 2 on every MCP call.
        assert nx_pre == nx_perm == {"python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/nx_hook_shim.py auto-approve"}, (
            f"{self.NX_HOOKS}: PreToolUse {nx_pre} vs PermissionRequest {nx_perm}"
        )

        sn_pre, sn_perm = handlers(self.SN_HOOKS, "PreToolUse"), handlers(self.SN_HOOKS, "PermissionRequest")
        assert sn_pre == sn_perm, f"{self.SN_HOOKS}: PreToolUse {sn_pre} vs PermissionRequest {sn_perm}"
        assert all(c.startswith("uv run ") for c in sn_pre), sn_pre
        assert any(c.endswith("/auto_approve_sn_mcp.py") for c in sn_pre), sn_pre


# TestNxPortMatchesBashByteForByte REMOVED (RDR-215 bead nexus-q02nx.21):
# it drove conexus/hooks/scripts/auto-approve-nx-mcp.sh, deleted in this
# same change now that hooks.json re-declares both the PreToolUse and
# PermissionRequest nx entries to the hook_auto_approve mcp_tool. Every
# tool name it parametrized over is covered directly elsewhere in this
# file: test_approves_nexus_catalog_tool / test_approves_hook_tools /
# test_ignores_sn_tools / test_ignores_unrelated_tools for the allow/deny
# shapes, TestDestructiveToolsRequireManualApproval for
# mcp__plugin_conexus_nexus__daemon_uninstall's exclusion, and
# TestPreToolUseApproval for the PreToolUse variant.
