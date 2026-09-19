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
SUBAGENT_START = SN_DIR / "hooks" / "scripts" / "subagent_start.py"
SESSION_START = SN_DIR / "hooks" / "scripts" / "session_start.py"


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
        # Exec form (RDR-215 bead nexus-q02nx.23): ``command`` is the
        # interpreter and the script is an ``args`` entry, so a
        # ``command``-only walk sees the bare word ``python3`` and matches
        # nothing while the entry still names whatever it likes. Join both,
        # the same reassembly ``_extract_hooks_json`` does in
        # tests/test_release_artifact_verb_rot.py.
        lines = [
            " ".join([h["command"], *h.get("args", [])])
            for entry in hooks
            for h in entry["hooks"]
        ]
        assert any(ln.startswith("python3 ") and ln.endswith("/subagent_start.py") for ln in lines), lines

    def test_mcp_json_exists(self) -> None:
        assert (SN_DIR / ".mcp.json").exists()

    def test_readme_exists(self) -> None:
        assert (SN_DIR / "README.md").exists()

    def test_every_hook_script_named_by_hooks_json_exists(self) -> None:
        """Exec form runs ``python3 <path>``, so the +x bit the bash wrappers
        needed is no longer part of the contract — asserting it would be a
        check whose domain no longer contains the claim. What still has to
        hold is that every path the manifest names is a file that is there.
        Resolved against SN_DIR, which is what ``$CLAUDE_PLUGIN_ROOT``
        expands to for an installed sn.
        """
        data = json.loads((SN_DIR / "hooks" / "hooks.json").read_text())
        named = [
            arg
            for hooks in data["hooks"].values()
            for entry in hooks
            for h in entry["hooks"]
            for arg in h.get("args", [])
            if arg.startswith("${CLAUDE_PLUGIN_ROOT}/")
        ]
        assert len(named) == 4, f"expected 4 exec-form script paths, got {named}"
        for arg in named:
            script = SN_DIR / arg[len("${CLAUDE_PLUGIN_ROOT}/"):]
            assert script.is_file(), f"hooks.json names {arg}, which does not exist"

    def test_no_hook_entry_spawns_bash(self) -> None:
        """RDR-215: the sn bash wrapper layer is gone. A re-introduced
        ``bash ...`` entry is the regression this pins."""
        data = json.loads((SN_DIR / "hooks" / "hooks.json").read_text())
        commands = [
            h["command"]
            for hooks in data["hooks"].values()
            for entry in hooks
            for h in entry["hooks"]
            if "command" in h
        ]
        assert commands and set(commands) == {"python3"}, commands


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
    """subagent_start.py must produce expected guidance sections.

    The hook emits a JSON envelope; ``hook_output`` returns the unwrapped
    additionalContext so the legacy substring assertions keep working
    against the markdown body. ``hook_envelope`` exposes the raw stdout
    for tests that need to verify the envelope shape itself.
    """

    @pytest.fixture(scope="class")
    def hook_envelope(self) -> str:
        result = subprocess.run(
            [sys.executable, str(SUBAGENT_START)],
            input="", capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    @pytest.fixture(scope="class")
    def hook_output(self, hook_envelope: str) -> str:
        envelope = json.loads(hook_envelope)
        return envelope["hookSpecificOutput"]["additionalContext"]

    def test_envelope_is_valid_json(self, hook_envelope: str) -> None:
        """Hook must emit the documented Claude Code SubagentStart envelope.

        Plain stdout was the prior shape; the JSON envelope is the
        documented schema and prevents silent drop on parser tightening.
        Mirrors the conexus SubagentStart hook, which emitted this envelope
        as conexus/hooks/scripts/subagent-start.sh at commit 68854ca and
        emits it from nexus.hooks.subagent_start since RDR-215 bead
        nexus-q02nx.21 ported and deleted that script. sn's own half moved
        from mcp-inject.sh to subagent_start.py at bead nexus-q02nx.23.
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
        payload = json.dumps({"task": "investigate the dependency migration and audit the package"})
        result = subprocess.run(
            [sys.executable, str(SUBAGENT_START)], input=payload,
            capture_output=True, text=True, timeout=10, cwd=str(REPO_ROOT),
        )
        body = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "## Serena MCP" in body
        assert "## Context7 MCP" in body


# ── Worktree guard (nexus-ftpk3) ─────────────────────────────────────────────


sys.path.insert(0, str(SN_DIR / "hooks" / "scripts"))
from worktree_guard import SERENA_WRITE_TOOLS, is_linked_worktree, is_serena_write_tool  # noqa: E402

AUTO_APPROVE = SN_DIR / "hooks" / "scripts" / "auto_approve_sn_mcp.py"
SNAPSHOT = SN_DIR / "hooks" / "scripts" / "serena-tools.txt"
INJECT = SUBAGENT_START


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
        [sys.executable, str(INJECT)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=10, cwd=str(REPO_ROOT),
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
        """Non-vacuity: the detector says 'primary' for this repository's
        primary checkout. Resolved through git's common dir so the assertion
        holds when the suite itself runs inside a linked worktree (every
        worktree suite run used to carry this one red, 2026-09-09)."""
        common = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        primary = Path(common).parent
        assert not is_linked_worktree(primary)
        if is_linked_worktree(REPO_ROOT):
            assert primary != REPO_ROOT.resolve()

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


class TestWorktreeDeveloperExample:
    """sn/examples/worktree-developer.md: the opt-in per-worktree Serena agent (nexus-ftpk3)."""

    EXAMPLE = SN_DIR / "examples" / "worktree-developer.md"

    def _frontmatter(self) -> str:
        text = self.EXAMPLE.read_text()
        assert text.startswith("---\n")
        return text.split("---\n")[1]

    def test_pinned_to_the_same_serena_revision(self) -> None:
        url, rev = serena_pin()
        assert f"{url}@{rev}" in self._frontmatter(), "bump the example when bumping sn/.mcp.json"

    def test_server_starts_with_no_project(self) -> None:
        """Measured (cc-validation scenario 30): the inline server spawns in the parent's cwd, so
        --project-from-cwd would root it at the primary. The agent activates its own pwd instead."""
        fm = self._frontmatter()
        assert "--project-from-cwd" not in fm
        assert "--context" in fm and "claude-code" in fm
        body = self.EXAMPLE.read_text().split("---\n", 2)[2]
        assert "activate_project" in body and "pwd" in body

    def test_tool_prefix_is_not_the_plugin_prefix(self) -> None:
        """The sn guard keys on mcp__plugin_sn_serena__; the private server must not collide with it."""
        fm = self._frontmatter()
        assert "serena-wt:" in fm
        assert "plugin_sn_serena" not in fm

    def test_readme_documents_the_example(self) -> None:
        readme = (SN_DIR / "README.md").read_text()
        assert "examples/worktree-developer.md" in readme
        assert "mcp__serena-wt__*" in readme


# ── SessionStart port (RDR-215 bead nexus-q02nx.23) ──────────────────────────


class TestSnSessionStart:
    """``session_start.py`` replaces ``session-start.sh``.

    The bash it replaces was the one script in the whole hook set whose exit
    code was not unconditionally 0: a bare ``cat`` with no ``2>/dev/null``
    and no fallback, so a missing ``session-start-section.md`` failed the
    event and printed to stderr. The port keeps the plain-text stdout shape
    (SessionStart takes stdout as context; this hook never emitted JSON) and
    adds the error boundary every other script in the set already had.
    """

    def _run(self, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SESSION_START)],
            input="", capture_output=True, text=True, timeout=10,
            cwd=str(cwd or REPO_ROOT), env=env,
        )

    def test_emits_the_session_start_section_verbatim(self) -> None:
        result = self._run()
        assert result.returncode == 0, result.stderr
        expected = (SN_DIR / "hooks" / "scripts" / "session-start-section.md").read_text()
        assert result.stdout == expected

    def test_output_is_plain_text_not_a_json_envelope(self) -> None:
        """Move, do not rewrite: the bash emitted the markdown bare, and a
        SessionStart hook's stdout is taken as context as-is. Wrapping it in
        ``hookSpecificOutput`` here would be a rewrite, not a port."""
        out = self._run().stdout
        assert out.lstrip().startswith("##"), out[:80]
        with pytest.raises(ValueError):
            json.loads(out)

    def test_missing_section_file_is_survivable(self, tmp_path: Path) -> None:
        """The defect the port fixes. A copy of the script with no sibling
        section file must still exit 0 and emit nothing on stdout, and must
        say on stderr that it did — logged, not silently swallowed.

        ``_hook_boundary.py`` is copied across with it deliberately: it is
        imported at module level, so a copy without it fails at import,
        before any boundary exists to catch anything, and this test would
        pass on the wrong crash. A missing boundary module is a broken
        install, which is not what is under test here — a missing SECTION
        file is."""
        scripts = SESSION_START.parent
        orphan = tmp_path / SESSION_START.name
        orphan.write_text(SESSION_START.read_text())
        (tmp_path / "_hook_boundary.py").write_text((scripts / "_hook_boundary.py").read_text())
        assert not (tmp_path / "session-start-section.md").exists()
        result = subprocess.run(
            [sys.executable, str(orphan)], input="",
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert result.stderr.strip(), "a missing section file must be logged, not swallowed silently"

    def test_hooks_json_wires_it_on_sessionstart(self) -> None:
        data = json.loads((SN_DIR / "hooks" / "hooks.json").read_text())
        lines = [
            " ".join([h["command"], *h.get("args", [])])
            for entry in data["hooks"]["SessionStart"]
            for h in entry["hooks"]
        ]
        assert any(ln.startswith("python3 ") and ln.endswith("/session_start.py") for ln in lines), lines


class TestSnHookErrorBoundary:
    """Every sn hook script survives its own crash (RDR-215).

    ``auto-approve-sn-mcp.sh`` ended in an unconditional ``exit 0`` that hid
    a Python crash completely — the wrapper is gone, so the boundary has to
    be in the Python or a crash becomes the event's problem.
    """

    SCRIPTS = (
        SN_DIR / "hooks" / "scripts" / "auto_approve_sn_mcp.py",
        SUBAGENT_START,
        SESSION_START,
    )

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_garbage_stdin_never_fails_the_event(self, script: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(script)], input="}{not json at all",
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr

    def test_the_boundary_swallows_and_logs(self, capsys: pytest.CaptureFixture) -> None:
        """Non-vacuity for the boundary itself. Without this, a boundary
        that is never entered passes the garbage-stdin cases above just as
        well as one that works."""
        sys.path.insert(0, str(SN_DIR / "hooks" / "scripts"))
        import _hook_boundary  # noqa: PLC0415 — bundled sibling, not a package import

        def boom() -> int:
            raise RuntimeError("forced")

        assert _hook_boundary.guard(boom, "probe") == 0
        err = capsys.readouterr().err
        assert "probe" in err and "RuntimeError" in err, err

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_each_script_routes_its_entry_point_through_the_boundary(self, script: Path) -> None:
        """The wiring half: the boundary existing proves nothing if a script
        calls ``main()`` directly at ``__main__``."""
        src = script.read_text()
        tail = src.split('if __name__ == "__main__":')[-1]
        assert "_hook_boundary.guard(" in tail or "guard(" in tail, tail


class TestBrokenWorktreeGuardSibling:
    """A broken ``worktree_guard.py`` degrades in one script and refuses in the other.

    The old `mcp-inject.sh` ran detection in its own subprocess under
    ``2>/dev/null``, so nothing it could do reached the ``cat`` calls after
    it. Importing it at module scope put it ahead of the boundary; measured
    on the first version of this port, a sibling that raises at import took
    the WHOLE envelope (rc=1, empty stdout) where the bash had exited 0 with
    both universal sections. Found by code review, by execution.
    """

    BROKEN = "this is not valid python(\n"

    def _install(self, tmp_path: Path, script: Path) -> Path:
        """*script* plus its real siblings, but with a worktree_guard that cannot import."""
        src = script.parent
        for name in (script.name, "_hook_boundary.py", "serena-section.md",
                     "context7-section.md", "worktree-section.md", "serena-tools.txt"):
            if (src / name).exists():
                (tmp_path / name).write_text((src / name).read_text())
        (tmp_path / "worktree_guard.py").write_text(self.BROKEN)
        return tmp_path / script.name

    def test_subagent_start_still_delivers_the_universal_sections(self, tmp_path: Path) -> None:
        target = self._install(tmp_path, SUBAGENT_START)
        result = subprocess.run(
            [sys.executable, str(target)], input=json.dumps({"cwd": str(tmp_path)}),
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        body = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "## Serena MCP" in body and "## Context7 MCP" in body
        assert "worktree_guard unavailable" in result.stderr

    def test_auto_approve_refuses_rather_than_unguarding(self, tmp_path: Path) -> None:
        """The opposite call, deliberately. Degrading here would approve a
        Serena WRITE from a worktree — the incident the guard exists for —
        so an unimportable guard must stop the allowlist, not bypass it."""
        target = self._install(tmp_path, AUTO_APPROVE)
        result = subprocess.run(
            [sys.executable, str(target)],
            input=json.dumps({"hook_event_name": "PreToolUse",
                              "tool_name": "mcp__plugin_sn_serena__replace_in_files"}),
            capture_output=True, text=True, timeout=10,
        )
        assert result.stdout.strip() == "", "an unguarded allowlist must not emit an allow"
