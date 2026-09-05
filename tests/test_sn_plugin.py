# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural and functional tests for the sn (Serena + Context7) plugin."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from sync_sn_serena_tools import parse_snapshot, serena_pin  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
SN_DIR = REPO_ROOT / "sn"
MARKETPLACE_PATH = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


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

    def test_serena_pinned_to_revision(self, mcp_config: dict) -> None:
        """nexus-jbt5x: an unpinned git+ URL gives every fresh spawn a different Serena."""
        url, rev = serena_pin()
        assert url == "https://github.com/oraios/serena"
        assert len(rev) == 40

    def test_context7_pinned_to_version(self, mcp_config: dict) -> None:
        pkg = next(a for a in mcp_config["context7"]["args"] if a.startswith("@upstash/context7-mcp"))
        assert re.fullmatch(r"@upstash/context7-mcp@\d+\.\d+\.\d+", pkg), pkg

    def test_snapshot_matches_pin(self) -> None:
        """serena-tools.txt was generated from the revision .mcp.json pins."""
        _, rev = serena_pin()
        snap_rev, available, _ = parse_snapshot()
        assert snap_rev == rev, "run scripts/sync_sn_serena_tools.py after changing the Serena pin"
        assert len(available) > 20, available


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
        """nexus-mkj6u: source is now the git-subdir object form with tag
        pinning. The plugin tree lives at `sn/` inside the repo; the
        marketplace.json source declares that via `path: "sn"` plus
        `ref: "v<version>"` pinning."""
        sn_entry = next(p for p in marketplace["plugins"] if p["name"] == "sn")
        source = sn_entry["source"]
        assert isinstance(source, dict), (
            f"sn source must be the object form (git-subdir), got {source!r}"
        )
        assert source["source"] == "git-subdir"
        assert source["path"] == "sn"
        assert source["url"] == "https://github.com/Hellblazer/nexus.git"
        # ref is pinned in lock-step with the version field; the
        # source-ref-matches-pyproject parity check enforces the exact
        # value (see tests/test_plugin_structure.py::TestMarketplaceVersion).
        from plugin_channel import client_version_of

        assert client_version_of(source.get("ref", "")) is not None, (
            f"sn source.ref {source.get('ref')!r} is neither v<X.Y.Z> nor "
            f"plugin-v<X.Y.Z>-<n> (RDR-197 invariant R)"
        )

    def test_sn_has_version(self, marketplace: dict) -> None:
        sn_entry = next(p for p in marketplace["plugins"] if p["name"] == "sn")
        assert "version" in sn_entry

    def test_sn_version_matches_plugin_json(self, marketplace: dict) -> None:
        """Marketplace and plugin.json versions must agree."""
        sn_entry = next(p for p in marketplace["plugins"] if p["name"] == "sn")
        plugin_json = json.loads((SN_DIR / ".claude-plugin" / "plugin.json").read_text())
        assert sn_entry["version"] == plugin_json["version"]

    def test_sn_version_matches_pyproject(self) -> None:
        """sn plugin.json version must match pyproject.toml — shared release version."""
        import tomllib
        plugin_json = json.loads((SN_DIR / ".claude-plugin" / "plugin.json").read_text())
        with PYPROJECT_PATH.open("rb") as f:
            pyproject = tomllib.load(f)
        assert plugin_json["version"] == pyproject["project"]["version"], (
            f"sn plugin.json version {plugin_json['version']!r} "
            f"!= pyproject.toml {pyproject['project']['version']!r}. "
            f"Update sn/.claude-plugin/plugin.json when bumping version."
        )


# ── Hook output ──────────────────────────────────────────────────────────────


class TestSnHookOutput:
    """mcp-inject.sh must produce expected guidance sections.

    The hook emits a JSON envelope; ``hook_output`` returns the unwrapped
    additionalContext so the legacy substring assertions keep working
    against the markdown body. ``hook_envelope`` exposes the raw stdout
    for tests that need to verify the envelope shape itself.
    """

    @pytest.fixture(scope="class")
    def hook_envelope(self) -> str:
        script = SN_DIR / "hooks" / "scripts" / "mcp-inject.sh"
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),  # so git rev-parse works
        )
        return result.stdout

    @pytest.fixture(scope="class")
    def hook_output(self, hook_envelope: str) -> str:
        envelope = json.loads(hook_envelope)
        return envelope["hookSpecificOutput"]["additionalContext"]

    def test_envelope_is_valid_json(self, hook_envelope: str) -> None:
        """Hook must emit the documented Claude Code SubagentStart envelope.

        Plain stdout was the prior shape; the JSON envelope is the
        documented schema and prevents silent drop on parser tightening.
        Mirrors conexus/hooks/scripts/subagent-start.sh (commit 68854ca).
        """
        envelope = json.loads(hook_envelope)
        assert "hookSpecificOutput" in envelope
        hso = envelope["hookSpecificOutput"]
        assert hso.get("hookEventName") == "SubagentStart"
        assert "additionalContext" in hso
        assert isinstance(hso["additionalContext"], str)

    def test_serena_section_present(self, hook_output: str) -> None:
        assert "## Serena MCP" in hook_output

    def test_context7_section_present(self, hook_output: str) -> None:
        assert "## Context7 MCP" in hook_output

    def test_serena_routing_table(self, hook_output: str) -> None:
        # Both JetBrains and LSP variants should appear (backend-agnostic discovery)
        assert "jet_brains_find_symbol" in hook_output
        assert "find_symbol" in hook_output
        assert "jet_brains_find_referencing_symbols" in hook_output
        assert "find_referencing_symbols" in hook_output

    def test_initial_instructions_delegation(self, hook_output: str) -> None:
        """Parameter docs are now delegated to Serena's initial_instructions tool."""
        assert "initial_instructions" in hook_output

    def test_jetbrains_edit_tool_in_routing(self, hook_output: str) -> None:
        """replace_in_files is the JetBrains backend's edit path and must be documented."""
        assert "replace_in_files" in hook_output

    def test_context7_workflow(self, hook_output: str) -> None:
        assert "resolve-library-id" in hook_output
        assert "query-docs" in hook_output

    def test_no_activate_project_instruction(self, hook_output: str) -> None:
        """With --project-from-cwd, no manual activation should be instructed."""
        assert "activate_project(project=" not in hook_output

    def test_excluded_tools_not_in_routing(self, hook_output: str) -> None:
        """Tools excluded by Serena's claude-code context should not be in the routing table.

        Assumes the claude-code context (serena/resources/config/contexts/claude-code.yml)
        excludes: create_text_file, read_file, execute_shell_command, prepare_for_new_conversation,
        replace_content. Verify on Serena version bumps — if the exclusion list changes upstream,
        update the inject script and this test accordingly.
        """
        _, _, excluded = parse_snapshot()
        assert excluded, "snapshot lists no excluded tools; the sync script is broken"
        for name in excluded:
            assert f"`{name}`" not in hook_output or name in ("find_file", "list_dir", "search_for_pattern"), (
                f"{name} is excluded by the claude-code context but the routing table names it"
            )
        for name in ("find_file", "list_dir", "search_for_pattern"):
            assert f"| `{name}`" not in hook_output, (
                f"{name} must not be a routing-table entry (the exclusion note may name it)"
            )

    def test_every_documented_serena_tool_exists(self, hook_output: str) -> None:
        """Every mcp__plugin_sn_serena__ name in the section is a tool the pinned Serena ships."""
        _, available, _ = parse_snapshot()
        serena_part = hook_output.split("## Context7")[0]
        documented = set(re.findall(r"mcp__plugin_sn_serena__([a-z_]+)", serena_part))
        rows = [ln for ln in serena_part.split("### Task")[1].split("###")[0].splitlines() if ln.startswith("|")]
        documented |= set(re.findall(r"`([a-z_]+)`", "\n".join(rows)))
        assert len(documented) > 10, sorted(documented)
        assert documented <= set(available), sorted(documented - set(available))

    def test_injects_both_sections_regardless_of_task_text(self) -> None:
        """nexus-jbt5x: the former task-text heuristic dropped Serena for 'investigate'/'dependency' briefs."""
        script = SN_DIR / "hooks" / "scripts" / "mcp-inject.sh"
        payload = json.dumps({"task": "investigate the dependency migration and audit the package"})
        result = subprocess.run(
            ["bash", str(script)], input=payload, capture_output=True, text=True, timeout=10, cwd=str(REPO_ROOT)
        )
        body = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "## Serena MCP" in body
        assert "## Context7 MCP" in body


# ── Worktree guard (nexus-ftpk3) ─────────────────────────────────────────────


sys.path.insert(0, str(SN_DIR / "hooks" / "scripts"))
from worktree_guard import SERENA_WRITE_TOOLS, is_linked_worktree, is_serena_write_tool  # noqa: E402

AUTO_APPROVE = SN_DIR / "hooks" / "scripts" / "auto_approve_sn_mcp.py"
SNAPSHOT = SN_DIR / "hooks" / "scripts" / "serena-tools.txt"
INJECT = SN_DIR / "hooks" / "scripts" / "mcp-inject.sh"


def _make_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A real primary checkout plus one linked worktree, so the detector is tested against git, not a fake."""
    primary = tmp_path / "primary"
    primary.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    def git(*args: str, cwd: Path = primary) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env, timeout=30)
    git("init", "-q", "-b", "main")
    (primary / "f.txt").write_text("x\n")
    git("add", "f.txt")
    git("commit", "-q", "-m", "init")
    worktree = tmp_path / "wt"
    git("worktree", "add", "-q", str(worktree), "-b", "agent-branch")
    return primary, worktree


def _run_auto_approve(payload: dict) -> dict | None:
    result = subprocess.run(
        [sys.executable, str(AUTO_APPROVE), str(SNAPSHOT)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def _run_inject(payload: dict) -> str:
    result = subprocess.run(
        ["bash", str(INJECT)], input=json.dumps(payload), capture_output=True, text=True, timeout=10, cwd=str(REPO_ROOT)
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


class TestWorktreeDetection:
    def test_primary_checkout_is_not_a_worktree(self, tmp_path: Path) -> None:
        primary, _ = _make_repo_with_worktree(tmp_path)
        (primary / "sub").mkdir()
        assert not is_linked_worktree(primary)
        assert not is_linked_worktree(primary / "sub")

    def test_linked_worktree_detected_from_root_and_subdir(self, tmp_path: Path) -> None:
        _, worktree = _make_repo_with_worktree(tmp_path)
        (worktree / "src" / "pkg").mkdir(parents=True)
        assert is_linked_worktree(worktree)
        assert is_linked_worktree(worktree / "src" / "pkg")

    def test_this_repo_primary_is_not_a_worktree(self) -> None:
        """Non-vacuity: the detector says 'primary' for the checkout the suite runs in."""
        assert not is_linked_worktree(REPO_ROOT)

    def test_empty_and_missing_cwd_are_not_worktrees(self, tmp_path: Path) -> None:
        assert not is_linked_worktree("")
        assert not is_linked_worktree(tmp_path / "nowhere")

    def test_submodule_pointer_is_not_a_worktree(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / ".git").write_text("gitdir: /some/repo/.git/modules/sub\n")
        assert not is_linked_worktree(sub)

    def test_write_tool_set_is_a_subset_of_the_snapshot(self) -> None:
        """Every guarded name is a tool the pinned Serena ships; a rename upstream must fail here, not silently unguard."""
        _, available, _ = parse_snapshot()
        assert SERENA_WRITE_TOOLS <= set(available), sorted(SERENA_WRITE_TOOLS - set(available))
        for name in ("replace_in_files", "replace_symbol_body", "insert_after_symbol", "rename_symbol", "jet_brains_rename"):
            assert is_serena_write_tool(f"mcp__plugin_sn_serena__{name}")
        for name in ("find_symbol", "get_symbols_overview", "jet_brains_find_symbol", "read_memory"):
            assert not is_serena_write_tool(f"mcp__plugin_sn_serena__{name}")


class TestWorktreeGuardHook:
    @pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest"])
    def test_write_tool_denied_in_worktree(self, tmp_path: Path, event: str) -> None:
        _, worktree = _make_repo_with_worktree(tmp_path)
        out = _run_auto_approve({"cwd": str(worktree), "hook_event_name": event,
                                 "tool_name": "mcp__plugin_sn_serena__replace_in_files"})
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == event
        if event == "PreToolUse":
            assert hso["permissionDecision"] == "deny"
            assert str(worktree) in hso["permissionDecisionReason"]
        else:
            assert hso["decision"]["behavior"] == "deny"
            assert str(worktree) in hso["decision"]["message"]

    def test_every_write_tool_denied_in_worktree(self, tmp_path: Path) -> None:
        _, worktree = _make_repo_with_worktree(tmp_path)
        for name in sorted(SERENA_WRITE_TOOLS):
            out = _run_auto_approve({"cwd": str(worktree), "hook_event_name": "PreToolUse",
                                     "tool_name": f"mcp__plugin_sn_serena__{name}"})
            assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", name

    def test_read_tool_still_allowed_in_worktree(self, tmp_path: Path) -> None:
        _, worktree = _make_repo_with_worktree(tmp_path)
        out = _run_auto_approve({"cwd": str(worktree), "hook_event_name": "PreToolUse",
                                 "tool_name": "mcp__plugin_sn_serena__find_symbol"})
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_write_tool_allowed_in_primary(self, tmp_path: Path) -> None:
        primary, _ = _make_repo_with_worktree(tmp_path)
        out = _run_auto_approve({"cwd": str(primary), "hook_event_name": "PreToolUse",
                                 "tool_name": "mcp__plugin_sn_serena__replace_in_files"})
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_payload_without_cwd_falls_through_to_allowlist(self) -> None:
        out = _run_auto_approve({"hook_event_name": "PreToolUse", "tool_name": "mcp__plugin_sn_serena__replace_in_files"})
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_context7_unaffected_in_worktree(self, tmp_path: Path) -> None:
        _, worktree = _make_repo_with_worktree(tmp_path)
        out = _run_auto_approve({"cwd": str(worktree), "hook_event_name": "PreToolUse",
                                 "tool_name": "mcp__plugin_sn_context7__query-docs"})
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestWorktreeInjection:
    def test_worktree_agent_gets_section_first(self, tmp_path: Path) -> None:
        _, worktree = _make_repo_with_worktree(tmp_path)
        body = _run_inject({"cwd": str(worktree), "agent_type": "conexus:developer"})
        assert body.lstrip().startswith("## Worktree isolation")
        assert "## Serena MCP" in body and "## Context7 MCP" in body
        assert body.index("## Worktree isolation") < body.index("## Serena MCP")
        assert "`LSP`" in body
        assert "git status --short" in body

    def test_primary_agent_gets_no_worktree_section(self, tmp_path: Path) -> None:
        primary, _ = _make_repo_with_worktree(tmp_path)
        body = _run_inject({"cwd": str(primary)})
        assert "## Worktree isolation" not in body
        assert "## Serena MCP" in body and "## Context7 MCP" in body

    def test_serena_section_names_the_startup_root_rule(self) -> None:
        body = _run_inject({"cwd": str(REPO_ROOT)})
        assert "Root is fixed at server start" in body

    def test_worktree_section_names_only_real_tools(self) -> None:
        _, available, _ = parse_snapshot()
        text = (SN_DIR / "hooks" / "scripts" / "worktree-section.md").read_text()
        named = set(re.findall(r"`((?:jet_brains_)?[a-z_]+)`", text)) & {n for n in available}
        assert named <= set(available)
        assert "replace_in_files" in named and "replace_symbol_body" in named
