# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural validation tests for the nx Claude Code plugin.

Validates that all agent .md files, skill SKILL.md files, command .md files,
and registry.yaml follow the documented conventions and are internally consistent.
These tests act as a regression guard against documentation drift and syntax rot.
"""
import re
import subprocess
import shutil
from pathlib import Path
from typing import Generator

import yaml
import pytest

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
PLUGIN_DIR = REPO_ROOT / "nx"
AGENTS_DIR = PLUGIN_DIR / "agents"
SHARED_DIR = AGENTS_DIR / "_shared"
SKILLS_DIR = PLUGIN_DIR / "skills"
COMMANDS_DIR = PLUGIN_DIR / "commands"
REGISTRY_PATH = PLUGIN_DIR / "registry.yaml"

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def registry() -> dict:
    with REGISTRY_PATH.open() as f:
        return yaml.safe_load(f)


def agent_files() -> list[Path]:
    """All top-level agent .md files (excludes _shared/)."""
    return sorted(p for p in AGENTS_DIR.glob("*.md"))


def skill_skill_mds() -> list[Path]:
    """All SKILL.md files in the skills directory."""
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def command_files() -> list[Path]:
    """All command .md files."""
    return sorted(COMMANDS_DIR.glob("*.md"))


# ── Registry integrity ────────────────────────────────────────────────────────


class TestRegistryIntegrity:
    """registry.yaml must reference files that actually exist."""

    def test_registry_file_exists(self) -> None:
        assert REGISTRY_PATH.exists(), f"registry.yaml not found at {REGISTRY_PATH}"

    def test_registry_parses(self, registry: dict) -> None:
        assert "agents" in registry, "registry.yaml missing 'agents' key"
        assert "version" in registry, "registry.yaml missing 'version' key"

    @pytest.mark.parametrize("agent_name", [
        pytest.param(name, id=name)
        for name in yaml.safe_load(REGISTRY_PATH.read_text()).get("agents", {})
    ])
    def test_agent_has_md_file(self, agent_name: str) -> None:
        """Every agent in registry.yaml must have a corresponding agents/{name}.md."""
        md_path = AGENTS_DIR / f"{agent_name}.md"
        assert md_path.exists(), (
            f"Agent '{agent_name}' in registry.yaml has no {md_path}"
        )

    @pytest.mark.parametrize("agent_name,agent_meta", [
        pytest.param(name, meta, id=name)
        for name, meta in yaml.safe_load(REGISTRY_PATH.read_text()).get("agents", {}).items()
        if meta.get("skill")
    ])
    def test_agent_skill_directory_exists(self, agent_name: str, agent_meta: dict) -> None:
        """Every agent's skill directory must exist with a SKILL.md."""
        skill_dir = SKILLS_DIR / agent_meta["skill"]
        skill_md = skill_dir / "SKILL.md"
        assert skill_md.exists(), (
            f"Agent '{agent_name}' references skill '{agent_meta['skill']}' "
            f"but {skill_md} does not exist"
        )

    @pytest.mark.parametrize("pipeline_name,pipeline_meta", [
        pytest.param(name, meta, id=name)
        for name, meta in yaml.safe_load(REGISTRY_PATH.read_text()).get("pipelines", {}).items()
    ])
    def test_pipeline_agents_exist(
        self, registry: dict, pipeline_name: str, pipeline_meta: dict
    ) -> None:
        """All agents referenced in pipelines must be defined in the agents section."""
        known = set(registry["agents"].keys())
        for step in pipeline_meta.get("sequence", []):
            assert step in known, (
                f"Pipeline '{pipeline_name}' references unknown agent '{step}'"
            )

    @pytest.mark.parametrize("agent_name,agent_meta", [
        pytest.param(name, meta, id=name)
        for name, meta in yaml.safe_load(REGISTRY_PATH.read_text()).get("agents", {}).items()
    ])
    def test_predecessors_and_successors_exist(
        self, registry: dict, agent_name: str, agent_meta: dict
    ) -> None:
        """predecessors and successors must reference known agents."""
        known = set(registry["agents"].keys())
        for rel in ("predecessors", "successors"):
            for ref in agent_meta.get(rel, []):
                assert ref in known, (
                    f"Agent '{agent_name}' has {rel} entry '{ref}' "
                    f"which is not in the agents section"
                )

    def test_model_summary_matches_agents(self, registry: dict) -> None:
        """model_summary agents must all exist in the agents section."""
        known = set(registry["agents"].keys())
        for model, listed in registry.get("model_summary", {}).items():
            for name in listed:
                assert name in known, (
                    f"model_summary/{model} references unknown agent '{name}'"
                )


# ── Agent structure ───────────────────────────────────────────────────────────


class TestAgentStructure:
    """All agent .md files must contain required sections."""

    REQUIRED_SECTIONS = [
        "## Relay Reception",
        "## Context Protocol",
        "### Agent-Specific PRODUCE",
        "RECOVER protocol",
    ]

    @pytest.mark.parametrize("agent_path", [
        pytest.param(p, id=p.name) for p in agent_files()
    ])
    def test_required_sections_present(self, agent_path: Path) -> None:
        text = agent_path.read_text()
        for section in self.REQUIRED_SECTIONS:
            assert section in text, (
                f"{agent_path.name}: missing '{section}'"
            )

    @pytest.mark.parametrize("agent_path", [
        pytest.param(p, id=p.name) for p in agent_files()
    ])
    def test_project_context_block_present(self, agent_path: Path) -> None:
        """All agents must reference CONTEXT_PROTOCOL.md for PM context."""
        text = agent_path.read_text()
        assert "CONTEXT_PROTOCOL.md" in text, (
            f"{agent_path.name}: missing CONTEXT_PROTOCOL.md reference"
        )

    @pytest.mark.parametrize("agent_path", [
        pytest.param(p, id=p.name) for p in agent_files()
    ])
    def test_frontmatter_has_required_fields(self, agent_path: Path) -> None:
        """Agent frontmatter must have name, version, description, model, color."""
        text = agent_path.read_text()
        # Extract YAML frontmatter between --- delimiters
        match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        assert match, f"{agent_path.name}: no YAML frontmatter found"
        fm = yaml.safe_load(match.group(1))
        for field in ("name", "version", "description", "model", "color"):
            assert field in fm, (
                f"{agent_path.name}: frontmatter missing '{field}'"
            )


# ── RECOVER protocol completeness ─────────────────────────────────────────────


class TestRecoverProtocol:
    """Every agent RECOVER block must be 6 steps with T1 scratch at step 3."""

    @pytest.mark.parametrize("agent_path", [
        pytest.param(p, id=p.name) for p in agent_files()
    ])
    def test_recover_has_six_steps(self, agent_path: Path) -> None:
        text = agent_path.read_text()
        # Find the RECOVER block
        match = re.search(
            r"If validation fails.*?(?=\n###|\n##|\Z)",
            text,
            re.DOTALL,
        )
        assert match, f"{agent_path.name}: no 'If validation fails' block found"
        block = match.group(0)
        assert "6. Proceed with available context" in block, (
            f"{agent_path.name}: RECOVER block does not have 6 steps "
            "(missing '6. Proceed with available context')"
        )

    @pytest.mark.parametrize("agent_path", [
        pytest.param(p, id=p.name) for p in agent_files()
    ])
    def test_recover_has_t1_scratch_step(self, agent_path: Path) -> None:
        """RECOVER step 3 must include T1 scratch search."""
        text = agent_path.read_text()
        match = re.search(
            r"If validation fails.*?(?=\n###|\n##|\Z)",
            text,
            re.DOTALL,
        )
        assert match, f"{agent_path.name}: no 'If validation fails' block found"
        block = match.group(0)
        assert "nx scratch search" in block, (
            f"{agent_path.name}: RECOVER block missing 'nx scratch search' step"
        )

    @pytest.mark.parametrize("agent_path", [
        pytest.param(p, id=p.name) for p in agent_files()
    ])
    def test_recover_uses_nx_memory_search(self, agent_path: Path) -> None:
        """RECOVER T2 step must use 'nx memory search', not stale 'nx memory get'."""
        text = agent_path.read_text()
        match = re.search(
            r"If validation fails.*?(?=\n###|\n##|\Z)",
            text,
            re.DOTALL,
        )
        if not match:
            pytest.skip(f"{agent_path.name}: no RECOVER block")
        block = match.group(0)
        # Step 2 must use search, not get --project --title {filename}
        assert "nx memory get --project" not in block or "nx memory search" in block, (
            f"{agent_path.name}: RECOVER block uses stale 'nx memory get' instead of "
            "'nx memory search'"
        )


# ── CLI syntax validation ─────────────────────────────────────────────────────


class TestCliSyntax:
    """No agent, skill, or command file should use known-bad CLI patterns."""

    ALL_MD_FILES = agent_files() + list(skill_skill_mds()) + command_files()

    @pytest.mark.parametrize("md_path", [
        pytest.param(p, id=str(p.relative_to(PLUGIN_DIR))) for p in ALL_MD_FILES
    ])
    def test_no_stale_pm_colon_notation(self, md_path: Path) -> None:
        """No file should use the stale 'pm::' key notation."""
        text = md_path.read_text()
        assert "pm::" not in text, (
            f"{md_path}: contains stale 'pm::' notation — "
            "use 'nx pm' commands or 'nx memory put --project {project}' (bare name, no _pm suffix) instead"
        )

    @pytest.mark.parametrize("md_path", [
        pytest.param(p, id=str(p.relative_to(PLUGIN_DIR))) for p in ALL_MD_FILES
    ])
    def test_no_stale_nx_health(self, md_path: Path) -> None:
        """'nx health' was renamed to 'nx doctor'."""
        text = md_path.read_text()
        # Allow 'nx doctor' mentions but not 'nx health' as a command
        assert not re.search(r"`nx health`|nx health\b", text), (
            f"{md_path}: contains stale 'nx health' command — use 'nx doctor'"
        )

    @pytest.mark.parametrize("agent_path", [
        pytest.param(p, id=p.name) for p in agent_files()
    ])
    def test_nx_store_put_has_pipe_source(self, agent_path: Path) -> None:
        """nx store put - must have a pipe source (printf/echo/...) before it."""
        text = agent_path.read_text()
        # Find all occurrences of nx store put -
        lines_with_put = [
            (i + 1, line)
            for i, line in enumerate(text.splitlines())
            if "nx store put -" in line
        ]
        for lineno, line in lines_with_put:
            stripped = line.strip()
            # Valid: starts with pipe or is continued from previous line
            # Pattern: something | nx store put -
            has_pipe = "|" in stripped and stripped.index("|") < stripped.index("nx store put -")
            # Or it's a comment/description (starts with #, -, or *)
            is_comment = re.match(r"^\s*[#\-*]", line)
            assert has_pipe or is_comment, (
                f"{agent_path.name}:{lineno}: 'nx store put -' missing pipe source. "
                f"Use 'printf \"...\" | nx store put -' or 'echo \"...\" | nx store put -'\n"
                f"  Line: {line.strip()}"
            )


# ── Skill structure ───────────────────────────────────────────────────────────


class TestSkillStructure:
    """All SKILL.md files must have required content."""

    # Standalone skills don't delegate to agents — exclude from agent-specific tests
    STANDALONE_SKILLS = {
        "cli-controller", "nexus",
        "brainstorming-gate", "using-nx-skills",
        "writing-nx-skills",
    }

    REQUIRED_SKILL_SECTIONS = [
        ("## Relay Template", "## Agent Invocation"),
        "## Success Criteria",
    ]

    @pytest.mark.parametrize("skill_path", [
        pytest.param(p, id=p.parent.name)
        for p in skill_skill_mds()
        if p.parent.name not in {
            "cli-controller", "nexus",
            "brainstorming-gate", "verification-before-completion",
            "receiving-code-review", "using-nx-skills",
            "dispatching-parallel-agents", "writing-nx-skills",
        }
    ])
    def test_skill_has_relay_template(self, skill_path: Path) -> None:
        text = skill_path.read_text()
        for section in self.REQUIRED_SKILL_SECTIONS:
            if isinstance(section, tuple):
                assert any(alt in text for alt in section), (
                    f"{skill_path.parent.name}/SKILL.md: missing one of {section}"
                )
            else:
                assert section in text, (
                    f"{skill_path.parent.name}/SKILL.md: missing '{section}'"
                )

    @pytest.mark.parametrize("skill_path", [
        pytest.param(p, id=p.parent.name)
        for p in skill_skill_mds()
        if p.parent.name not in {
            "cli-controller", "nexus",
            "brainstorming-gate", "verification-before-completion",
            "receiving-code-review", "using-nx-skills",
            "dispatching-parallel-agents", "writing-nx-skills",
        }
    ])
    def test_agent_skill_has_produce_section(self, skill_path: Path) -> None:
        """Agent-delegating skills must have an Agent-Specific PRODUCE section."""
        text = skill_path.read_text()
        assert "## Agent-Specific PRODUCE" in text or "Agent-Specific PRODUCE" in text, (
            f"{skill_path.parent.name}/SKILL.md: missing 'Agent-Specific PRODUCE' section"
        )

    @pytest.mark.parametrize("skill_path", [
        pytest.param(p, id=p.parent.name)
        for p in skill_skill_mds()
        if p.parent.name not in {
            "cli-controller", "nexus",
            "brainstorming-gate", "verification-before-completion",
            "receiving-code-review", "using-nx-skills",
            "dispatching-parallel-agents", "writing-nx-skills",
        }
    ])
    def test_skill_mentions_t1_scratch(self, skill_path: Path) -> None:
        """Agent-delegating skills should acknowledge T1 scratch usage."""
        text = skill_path.read_text()
        assert "nx scratch" in text, (
            f"{skill_path.parent.name}/SKILL.md: no mention of 'nx scratch' (T1 tier)"
        )

    @pytest.mark.parametrize("skill_path", [
        pytest.param(p, id=p.parent.name) for p in skill_skill_mds()
    ])
    def test_relay_template_has_required_rows(self, skill_path: Path) -> None:
        """Relay templates must include nx store, nx memory, and Files rows."""
        text = skill_path.read_text()
        if "## Relay Template" not in text:
            if "RELAY_TEMPLATE.md" in text:
                return  # Valid: uses hybrid cross-reference
            pytest.skip("No relay template in this skill")
        relay_section = text.split("## Relay Template")[1]
        for row in ("nx store:", "nx memory:", "Files:"):
            assert row in relay_section, (
                f"{skill_path.parent.name}/SKILL.md relay template: missing '{row}' row"
            )


# ── Command structure ─────────────────────────────────────────────────────────


class TestCommandStructure:
    """Command .md files must have syntactically valid bash blocks."""

    @pytest.mark.parametrize("cmd_path", [
        pytest.param(p, id=p.name) for p in command_files()
    ])
    def test_bash_block_syntax(self, cmd_path: Path) -> None:
        """Extract !{} bash blocks and check syntax with bash -n."""
        if shutil.which("bash") is None:
            pytest.skip("bash not available")

        text = cmd_path.read_text()
        # Extract content between !{ and }
        match = re.search(r"^!\{$(.*?)^}", text, re.MULTILINE | re.DOTALL)
        if not match:
            pytest.skip(f"{cmd_path.name}: no !{{}} bash block found")

        bash_content = match.group(1)
        result = subprocess.run(
            ["bash", "-n"],
            input=bash_content,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"{cmd_path.name}: bash syntax error in !{{}} block:\n{result.stderr}"
        )

    @pytest.mark.parametrize("cmd_path", [
        pytest.param(p, id=p.name) for p in command_files()
    ])
    def test_no_unescaped_glob_in_grep(self, cmd_path: Path) -> None:
        """grep patterns with * must use -F or escape the glob to avoid regex issues."""
        text = cmd_path.read_text()
        # Find grep calls with unescaped ** patterns in grep patterns (not in paths)
        bad = re.findall(r'grep\s+["\']?\*\*["\']?', text)
        assert not bad, (
            f"{cmd_path.name}: unescaped '**' in grep pattern — use grep -F or escape: {bad}"
        )

    @pytest.mark.parametrize("cmd_path", [
        pytest.param(p, id=p.name) for p in command_files()
    ])
    def test_nx_commands_guarded(self, cmd_path: Path) -> None:
        """nx commands in !{} blocks should be guarded with 'command -v nx' or '2>/dev/null'."""
        text = cmd_path.read_text()
        match = re.search(r"^!\{$(.*?)^}", text, re.MULTILINE | re.DOTALL)
        if not match:
            pytest.skip(f"{cmd_path.name}: no !{{}} bash block found")

        bash_content = match.group(1)
        # Find raw 'nx ' calls not guarded
        nx_calls = [
            line.strip()
            for line in bash_content.splitlines()
            if re.match(r"\s+nx\s+", line) and "2>/dev/null" not in line
            and "command -v nx" not in line
        ]
        # Allow some unguarded nx calls (e.g., in test commands that assert nx works)
        # but flag if MANY are unguarded — indicates the whole block lacks a guard
        assert len(nx_calls) < 5, (
            f"{cmd_path.name}: {len(nx_calls)} unguarded 'nx' calls in !{{}} block "
            "(add 2>/dev/null or wrap in 'if command -v nx &>/dev/null; then ... fi'):\n"
            + "\n".join(f"  {c}" for c in nx_calls[:5])
        )


# ── Cross-reference integrity ─────────────────────────────────────────────────


class TestCrossReferenceIntegrity:
    """Agent files must only reference agents that actually exist."""

    @pytest.mark.parametrize("agent_path", [
        pytest.param(p, id=p.name) for p in agent_files()
    ])
    def test_relay_to_references_exist(self, agent_path: Path) -> None:
        """Agent 'I Relay To' and Successor sections must reference existing agents."""
        text = agent_path.read_text()
        known_agents = {p.stem for p in agent_files()}

        # Find relay-to references: "relay to `agent-name`"
        relays = re.findall(r"relay to `([a-z][a-z0-9-]*)`", text, re.IGNORECASE)
        for ref in relays:
            assert ref in known_agents, (
                f"{agent_path.name}: references unknown agent '{ref}' in relay target"
            )

    @pytest.mark.parametrize("agent_path", [
        pytest.param(p, id=p.name) for p in agent_files()
    ])
    def test_shared_context_protocol_reference_valid(self, agent_path: Path) -> None:
        """CONTEXT_PROTOCOL.md reference must point to actual _shared file."""
        text = agent_path.read_text()
        if "CONTEXT_PROTOCOL.md" not in text:
            pytest.skip(f"{agent_path.name}: no CONTEXT_PROTOCOL.md reference")
        shared_file = SHARED_DIR / "CONTEXT_PROTOCOL.md"
        assert shared_file.exists(), (
            f"_shared/CONTEXT_PROTOCOL.md referenced by {agent_path.name} does not exist"
        )


# ── CSO Description Validation ───────────────────────────────────────────────


class TestSkillDescriptionCSO:
    """Skill descriptions must follow CSO 'Use when' pattern."""

    @pytest.mark.parametrize("skill_path", [
        pytest.param(p, id=p.parent.name) for p in skill_skill_mds()
    ])
    def test_description_starts_with_use_when(self, skill_path: Path) -> None:
        """All skill descriptions must start with 'Use when' per CSO methodology."""
        text = skill_path.read_text()
        match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        assert match, f"{skill_path.parent.name}/SKILL.md: no YAML frontmatter"
        fm = yaml.safe_load(match.group(1))
        desc = fm.get("description", "")
        assert desc.lower().startswith("use when"), (
            f"{skill_path.parent.name}/SKILL.md: description must start with "
            f"'Use when' (CSO pattern). Got: {desc[:80]!r}"
        )

    @pytest.mark.parametrize("skill_path", [
        pytest.param(p, id=p.parent.name) for p in skill_skill_mds()
    ])
    def test_description_no_workflow_keywords(self, skill_path: Path) -> None:
        """Descriptions must not summarize workflow — just triggering conditions."""
        text = skill_path.read_text()
        match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not match:
            pytest.skip("No frontmatter")
        fm = yaml.safe_load(match.group(1))
        desc = fm.get("description", "")
        bad_keywords = ["Triggers:", "user says", "workflow", "process:"]
        for kw in bad_keywords:
            assert kw not in desc, (
                f"{skill_path.parent.name}/SKILL.md: description contains "
                f"workflow keyword {kw!r}. Descriptions should state WHEN to "
                f"use the skill, not summarize what it does."
            )

    @pytest.mark.parametrize("skill_path", [
        pytest.param(p, id=p.parent.name) for p in skill_skill_mds()
    ])
    def test_frontmatter_only_standard_fields(self, skill_path: Path) -> None:
        """Skill frontmatter must contain only name and description."""
        text = skill_path.read_text()
        match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        assert match, f"{skill_path.parent.name}/SKILL.md: no YAML frontmatter"
        fm = yaml.safe_load(match.group(1))
        allowed = {"name", "description"}
        extra = set(fm.keys()) - allowed
        assert not extra, (
            f"{skill_path.parent.name}/SKILL.md: non-standard frontmatter fields "
            f"{extra}. Claude Code only reads 'name' and 'description'."
        )

    @pytest.mark.parametrize("skill_path", [
        pytest.param(p, id=p.parent.name) for p in skill_skill_mds()
    ])
    def test_no_yaml_comments_in_frontmatter(self, skill_path: Path) -> None:
        """No YAML comments inside frontmatter block."""
        text = skill_path.read_text()
        match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not match:
            pytest.skip("No frontmatter")
        frontmatter_raw = match.group(1)
        comment_lines = [
            line for line in frontmatter_raw.splitlines()
            if line.strip().startswith("#")
        ]
        assert not comment_lines, (
            f"{skill_path.parent.name}/SKILL.md: YAML comments in frontmatter: "
            f"{comment_lines}"
        )


# ── Hook Structure ───────────────────────────────────────────────────────────


class TestHookStructure:
    """Hook configuration must follow best practices."""

    def test_post_tool_use_has_matcher(self) -> None:
        """PostToolUse hooks should have a matcher to avoid firing on every tool use."""
        import json
        hooks_path = PLUGIN_DIR / "hooks" / "hooks.json"
        assert hooks_path.exists(), "hooks.json not found"
        hooks = json.loads(hooks_path.read_text())
        for entry in hooks.get("PostToolUse", []):
            has_matcher = "matcher" in entry
            has_filter = "grep" in entry.get("command", "") or "bd create" in entry.get("command", "")
            assert has_matcher or has_filter, (
                f"PostToolUse hook fires on every tool use without matcher: "
                f"{entry.get('command', '')[:80]}"
            )


# ── Standalone Skill Registry ────────────────────────────────────────────────


class TestStandaloneSkillRegistry:
    """standalone_skills entries must have matching directories."""

    def test_standalone_skill_directory_exists(self) -> None:
        """Every standalone_skills entry must have a matching skill directory."""
        registry = yaml.safe_load(REGISTRY_PATH.read_text())
        standalone = registry.get("standalone_skills", {})
        for skill_name in standalone:
            skill_dir = SKILLS_DIR / skill_name
            assert skill_dir.is_dir(), (
                f"standalone_skills entry '{skill_name}' has no matching "
                f"directory at {skill_dir}"
            )
            skill_md = skill_dir / "SKILL.md"
            assert skill_md.exists(), (
                f"standalone_skills entry '{skill_name}' has directory but "
                f"no SKILL.md at {skill_md}"
            )


# ── Shared Resources ────────────────────────────────────────────────────────


class TestSharedResources:
    """Shared resources referenced by skills must exist."""

    def test_relay_template_exists(self) -> None:
        """RELAY_TEMPLATE.md must exist — all hybrid relay cross-references point to it."""
        relay_template = SHARED_DIR / "RELAY_TEMPLATE.md"
        assert relay_template.exists(), (
            f"RELAY_TEMPLATE.md not found at {relay_template}. "
            f"All agent-delegating skills cross-reference this file."
        )
        content = relay_template.read_text()
        assert len(content) > 100, (
            "RELAY_TEMPLATE.md exists but appears empty or trivially short"
        )
