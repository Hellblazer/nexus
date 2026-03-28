# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural and functional tests for the sn (Serena + Context7) plugin."""
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SN_DIR = REPO_ROOT / "sn"
MARKETPLACE_PATH = REPO_ROOT / ".claude-plugin" / "marketplace.json"


# ── Plugin structure ─────────────────────────────────────────────────────────


class TestSnPluginStructure:
    """sn plugin must have required files with valid contents."""

    def test_plugin_json_exists(self) -> None:
        assert (SN_DIR / ".claude-plugin" / "plugin.json").exists()

    def test_plugin_json_valid(self) -> None:
        data = json.loads((SN_DIR / ".claude-plugin" / "plugin.json").read_text())
        assert data["name"] == "sn"
        assert "version" in data
        assert "description" in data

    def test_hooks_json_exists(self) -> None:
        assert (SN_DIR / "hooks" / "hooks.json").exists()

    def test_hooks_json_valid(self) -> None:
        data = json.loads((SN_DIR / "hooks" / "hooks.json").read_text())
        assert "hooks" in data
        assert "SubagentStart" in data["hooks"]
        hooks = data["hooks"]["SubagentStart"]
        assert len(hooks) >= 1
        # Hook must reference mcp-inject.sh
        commands = [h["command"] for entry in hooks for h in entry["hooks"]]
        assert any("mcp-inject.sh" in c for c in commands)

    def test_mcp_json_exists(self) -> None:
        assert (SN_DIR / ".mcp.json").exists()

    def test_readme_exists(self) -> None:
        assert (SN_DIR / "README.md").exists()

    def test_hook_script_exists_and_executable(self) -> None:
        script = SN_DIR / "hooks" / "scripts" / "mcp-inject.sh"
        assert script.exists()
        assert script.stat().st_mode & 0o111, "mcp-inject.sh must be executable"


# ── MCP configuration ────────────────────────────────────────────────────────


class TestSnMcpConfig:
    """MCP server definitions must have correct flags."""

    @pytest.fixture(scope="class")
    def mcp_config(self) -> dict:
        return json.loads((SN_DIR / ".mcp.json").read_text())

    def test_serena_server_defined(self, mcp_config: dict) -> None:
        assert "serena" in mcp_config

    def test_serena_uses_claude_code_context(self, mcp_config: dict) -> None:
        args = mcp_config["serena"]["args"]
        assert "--context" in args
        ctx_idx = args.index("--context")
        assert args[ctx_idx + 1] == "claude-code"

    def test_serena_uses_project_from_cwd(self, mcp_config: dict) -> None:
        args = mcp_config["serena"]["args"]
        assert "--project-from-cwd" in args

    def test_context7_server_defined(self, mcp_config: dict) -> None:
        assert "context7" in mcp_config

    def test_context7_uses_npx(self, mcp_config: dict) -> None:
        assert mcp_config["context7"]["command"] == "npx"


# ── Marketplace registration ─────────────────────────────────────────────────


class TestSnMarketplace:
    """sn must be listed in the marketplace."""

    @pytest.fixture(scope="class")
    def marketplace(self) -> dict:
        return json.loads(MARKETPLACE_PATH.read_text())

    def test_sn_in_marketplace(self, marketplace: dict) -> None:
        names = [p["name"] for p in marketplace["plugins"]]
        assert "sn" in names

    def test_sn_source_path(self, marketplace: dict) -> None:
        sn_entry = next(p for p in marketplace["plugins"] if p["name"] == "sn")
        assert sn_entry["source"] == "./sn"

    def test_sn_has_version(self, marketplace: dict) -> None:
        sn_entry = next(p for p in marketplace["plugins"] if p["name"] == "sn")
        assert "version" in sn_entry


# ── Hook output ──────────────────────────────────────────────────────────────


class TestSnHookOutput:
    """mcp-inject.sh must produce expected guidance sections."""

    @pytest.fixture(scope="class")
    def hook_output(self) -> str:
        script = SN_DIR / "hooks" / "scripts" / "mcp-inject.sh"
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),  # so git rev-parse works
        )
        return result.stdout

    def test_serena_section_present(self, hook_output: str) -> None:
        assert "## Serena MCP" in hook_output

    def test_context7_section_present(self, hook_output: str) -> None:
        assert "## Context7 MCP" in hook_output

    def test_serena_routing_table(self, hook_output: str) -> None:
        assert "jet_brains_find_symbol" in hook_output
        assert "jet_brains_find_referencing_symbols" in hook_output
        assert "jet_brains_get_symbols_overview" in hook_output

    def test_parameter_signatures_present(self, hook_output: str) -> None:
        assert "name_path_pattern" in hook_output
        assert "relative_path" in hook_output
        assert "include_body" in hook_output

    def test_context7_workflow(self, hook_output: str) -> None:
        assert "resolve-library-id" in hook_output
        assert "query-docs" in hook_output

    def test_no_activate_project_instruction(self, hook_output: str) -> None:
        """With --project-from-cwd, no manual activation should be instructed."""
        assert "activate_project(project=" not in hook_output

    def test_excluded_tools_not_in_routing(self, hook_output: str) -> None:
        """Tools excluded by claude-code context should not appear in routing table."""
        assert "replace_content" not in hook_output
        assert "create_text_file" not in hook_output
        assert "read_file" not in hook_output
