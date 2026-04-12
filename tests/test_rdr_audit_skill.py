# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural tests for the nx:rdr-audit skill (RDR-067 Phase 2a).

These tests pin the invariants that Phase 1b's spike surfaced:
- Transcript mining must be explicit and marked non-delegatable
- The canonical prompt must be referenced by stable T2 title
- Current-project derivation precedence chain must be documented
- The skill must satisfy the agent-delegating CI checks in test_plugin_structure.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
SKILL_PATH = REPO_ROOT / "nx" / "skills" / "rdr-audit" / "SKILL.md"
COMMAND_PATH = REPO_ROOT / "nx" / "commands" / "rdr-audit.md"
REGISTRY_PATH = REPO_ROOT / "nx" / "registry.yaml"
USING_SKILLS_PATH = REPO_ROOT / "nx" / "skills" / "using-nx-skills" / "SKILL.md"


def _load_skill_text() -> str:
    assert SKILL_PATH.exists(), f"Skill file missing: {SKILL_PATH}"
    return SKILL_PATH.read_text()


def _load_frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    assert m, "Skill file has no YAML frontmatter"
    return yaml.safe_load(m.group(1))


class TestSkillFileExists:

    def test_skill_file_present(self) -> None:
        assert SKILL_PATH.exists(), (
            f"nx/skills/rdr-audit/SKILL.md does not exist. "
            f"Phase 2a (nexus-dqp.3) creates this file."
        )

    def test_command_file_present(self) -> None:
        assert COMMAND_PATH.exists(), (
            f"nx/commands/rdr-audit.md does not exist. "
            f"Slash command file is required for /nx:rdr-audit invocation."
        )


class TestFrontmatter:

    def test_frontmatter_fields(self) -> None:
        fm = _load_frontmatter(_load_skill_text())
        assert set(fm.keys()) == {"name", "description", "effort"}, (
            f"Expected exactly name/description/effort; got {set(fm.keys())}"
        )

    def test_name_matches_directory(self) -> None:
        fm = _load_frontmatter(_load_skill_text())
        assert fm["name"] == "rdr-audit"

    def test_description_starts_with_use_when(self) -> None:
        fm = _load_frontmatter(_load_skill_text())
        assert fm["description"].lower().startswith("use when")

    def test_description_mentions_audit_and_scheduling(self) -> None:
        """The skill covers both audit dispatch and the management surface;
        description should cover both so the skill fires on either triggering context."""
        desc = _load_frontmatter(_load_skill_text())["description"].lower()
        assert "audit" in desc
        # One of these terms anchors the management surface (Phase 2b)
        assert any(
            term in desc
            for term in ("schedul", "periodic", "recurring", "manag", "inspect")
        ), f"description should hint at the management/scheduling surface: {desc}"


class TestAgentDelegatingStructure:
    """Enforces the CI-required sections from test_plugin_structure.py."""

    def test_has_agent_invocation_heading(self) -> None:
        text = _load_skill_text()
        assert "## Agent Invocation" in text or "## Relay Template" in text

    def test_has_success_criteria(self) -> None:
        assert "## Success Criteria" in _load_skill_text()

    def test_has_agent_specific_produce(self) -> None:
        assert "Agent-Specific PRODUCE" in _load_skill_text()

    def test_mentions_scratch(self) -> None:
        assert "scratch" in _load_skill_text().lower()

    def test_has_when_this_skill_activates(self) -> None:
        assert "## When This Skill Activates" in _load_skill_text()


class TestTranscriptMiningInvariant:
    """Phase 1b spike invariant: transcript mining is NOT delegatable.
    Main session must mine ~/.claude/projects/* before any Agent dispatch."""

    def test_explicit_pre_step_section(self) -> None:
        text = _load_skill_text()
        assert re.search(
            r"(?i)(PRE-?STEP|pre-?dispatch|main session|main-session).*transcript",
            text,
            re.DOTALL,
        ), "Skill must document the main-session transcript-mining pre-step"

    def test_non_delegatable_language(self) -> None:
        text = _load_skill_text().lower()
        assert "not delegatable" in text or "not delegateable" in text or \
               "non-delegatable" in text or "must not be delegated" in text, \
               "Skill must explicitly mark transcript mining as non-delegatable"

    def test_references_claude_projects_path(self) -> None:
        """The transcript-mining pre-step must name the actual path it reads from."""
        text = _load_skill_text()
        assert "~/.claude/projects" in text or ".claude/projects" in text

    def test_pre_step_precedes_agent_invocation(self) -> None:
        """Ordering matters: main session gathers BEFORE Agent tool dispatch."""
        text = _load_skill_text()
        pre_step_idx = min(
            (text.lower().find(marker) for marker in ("pre-step", "pre step", "pre-dispatch"))
            if any(m in text.lower() for m in ("pre-step", "pre step", "pre-dispatch"))
            else [-1],
            default=-1,
        )
        if pre_step_idx == -1:
            pre_step_idx = text.lower().find("transcript mining")
        agent_idx = text.find("## Agent Invocation")
        if agent_idx == -1:
            agent_idx = text.find("## Relay Template")
        assert 0 < pre_step_idx < agent_idx, (
            "Transcript-mining pre-step section must appear BEFORE the Agent Invocation section"
        )


class TestCanonicalPromptReference:

    def test_references_canonical_prompt_title(self) -> None:
        """Skill must reference the pinned canonical prompt by its T2 title."""
        text = _load_skill_text()
        assert "067-canonical-prompt-v1" in text, (
            "Skill must reference the pinned canonical prompt "
            "(T2 title nexus_rdr/067-canonical-prompt-v1)"
        )

    def test_references_substitution_parameters(self) -> None:
        """Skill must document the two substitution points the prompt expects."""
        text = _load_skill_text()
        assert "{project}" in text
        assert "{transcript_excerpts}" in text


class TestCurrentProjectDerivation:
    """Bead MINOR-1: precedence chain git remote → pwd basename → prompt user."""

    def test_git_remote_step_documented(self) -> None:
        text = _load_skill_text().lower()
        assert "git remote" in text, "Step 1 of precedence chain: git remote get-url origin"

    def test_pwd_fallback_documented(self) -> None:
        text = _load_skill_text().lower()
        assert "pwd" in text or "basename" in text or "cwd" in text

    def test_prompt_user_fallback_documented(self) -> None:
        text = _load_skill_text().lower()
        # Final fallback: ask the user
        assert re.search(r"(prompt|ask).*user|user.*(prompt|confirm)", text), (
            "Final fallback in precedence chain: prompt the user"
        )


class TestPersistenceOwnership:
    """Phase 1b finding: subagents do NOT reliably do memory_put.
    The skill body must own persistence, not the subagent."""

    def test_skill_body_owns_memory_put(self) -> None:
        text = _load_skill_text()
        # Must say the skill (not the subagent) calls memory_put after the dispatch
        assert "memory_put" in text
        # Persistence step must be in the skill body flow, not just "the subagent will persist"
        assert re.search(
            r"(?i)(skill body|main session|after.*subagent|after.*dispatch).*memory_put|"
            r"memory_put.*(skill body|main session|after)",
            text,
        ), "Skill must document that the skill body owns the memory_put step, not the subagent"


class TestManagementSubcommands:
    """Phase 2b: skill must document all 5 management subcommands.
    Structural test — verifies the skill file documents the behavior; runtime
    safety is enforced by the skill body being followed and validated at Phase 5a."""

    SUBCOMMANDS = ("list", "status", "history", "schedule", "unschedule")

    def test_management_section_present(self) -> None:
        text = _load_skill_text()
        assert "## Management Subcommands" in text, (
            "Skill must have a ## Management Subcommands section (Phase 2b)"
        )

    @pytest.mark.parametrize("subcommand", SUBCOMMANDS)
    def test_subcommand_documented(self, subcommand: str) -> None:
        text = _load_skill_text()
        mgmt_idx = text.find("## Management Subcommands")
        assert mgmt_idx >= 0
        mgmt_section = text[mgmt_idx:]
        next_section = re.search(r"\n## [^#]", mgmt_section[1:])
        if next_section:
            mgmt_section = mgmt_section[: next_section.start() + 1]
        assert f"`{subcommand}`" in mgmt_section or f"### {subcommand}" in mgmt_section.lower(), (
            f"Subcommand `{subcommand}` not documented in Management Subcommands section"
        )

    def test_list_format_documented(self) -> None:
        """The `list` subcommand must document the launchctl + crontab shell-out pattern."""
        text = _load_skill_text()
        assert "launchctl list" in text
        assert "crontab -l" in text

    def test_status_queries_rdr_process(self) -> None:
        """The `status` subcommand must document T2 lookup against rdr_process."""
        text = _load_skill_text()
        # Finding the last-run outcome comes from rdr_process/audit-<project>-*
        assert re.search(
            r"status.*?rdr_process|rdr_process.*?status|audit-.{0,10}project.{0,10}-",
            text,
            re.DOTALL | re.IGNORECASE,
        )

    @staticmethod
    def _subsection(text: str, subcommand: str) -> str:
        """Extract a ### subsection for a subcommand, handling optional backtick wrapping
        and trailing qualifiers like '(read-only)'. Stops at the next ### or next ## heading."""
        pattern = rf"### `?{re.escape(subcommand)}`?[^\n]*\n.*?(?=\n### |\n## (?!#)|\Z)"
        match = re.search(pattern, text, re.DOTALL)
        return match.group(0) if match else ""

    def test_history_documents_count_default(self) -> None:
        """The `history` subcommand must document a default count (5) and accept override."""
        section = self._subsection(_load_skill_text(), "history")
        assert section, "history subsection not found"
        assert "5" in section or "default" in section.lower(), (
            "history subcommand should document default N"
        )

    def test_schedule_documents_project_substitution(self) -> None:
        """The `schedule` subcommand must have a {project} or <PROJECT> substitution point
        in the template text it prints."""
        section = self._subsection(_load_skill_text(), "schedule")
        assert section, "schedule subcommand must have its own ### section"
        assert re.search(r"<PROJECT>|{project}|\$PROJECT", section), (
            "schedule subcommand must document a project-name substitution point"
        )

    def test_schedule_documents_both_platforms(self) -> None:
        """schedule must document both macOS (launchd/plist) and Linux (cron) templates."""
        section = self._subsection(_load_skill_text(), "schedule").lower()
        assert section
        assert "launchd" in section or "plist" in section
        assert "cron" in section

    def test_unschedule_documents_both_platforms(self) -> None:
        section = self._subsection(_load_skill_text(), "unschedule").lower()
        assert section
        assert "launchctl unload" in section or "plist" in section
        assert "crontab" in section


class TestManagementSafetySplit:
    """The read-only vs print-only safety split is the core user-protection invariant.
    Tests enforce that the skill body documents both halves of the split clearly enough
    that a following agent will implement them correctly."""

    READONLY_SUBCOMMANDS = ("list", "status", "history")
    PRINTONLY_SUBCOMMANDS = ("schedule", "unschedule")

    def test_readonly_invariant_explicit(self) -> None:
        """The skill body must explicitly name the read-only subcommands and forbid mutation."""
        text = _load_skill_text().lower()
        # The names must appear together somewhere (as a group)
        for sc in self.READONLY_SUBCOMMANDS:
            assert sc in text
        # And there must be "read-only" language near them
        assert "read-only" in text or "read only" in text

    def test_readonly_forbids_os_mutation(self) -> None:
        """Read-only subcommands must be documented as not modifying OS state."""
        text = _load_skill_text()
        assert re.search(
            r"(?i)(must not|do(es)? not).{0,80}(modify|change|mutate|write).{0,40}(os|file|system|state)",
            text,
        ) or re.search(
            r"(?i)(no|zero).{0,30}(file writes|os state|os mutation|process spawn)",
            text,
        ), "Read-only subcommands must explicitly document no OS state mutation"

    def test_readonly_forbids_t2_mutation(self) -> None:
        """Read-only subcommands must be documented as not modifying T2 state."""
        text = _load_skill_text()
        # Accept either explicit "MUST NOT memory_put" or "no T2 writes" style language
        assert re.search(
            r"(?i)(must not|do(es)? not|no).{0,60}(memory_put|memory_delete|t2 (write|mutation|state)|modify t2)",
            text,
        ), "Read-only subcommands must explicitly document no T2 state mutation"

    def test_printonly_invariant_explicit(self) -> None:
        """The skill body must explicitly name the print-only subcommands and forbid execution."""
        text = _load_skill_text().lower()
        for sc in self.PRINTONLY_SUBCOMMANDS:
            assert sc in text
        assert "print-only" in text or "print only" in text

    def test_printonly_forbids_launchctl_execution(self) -> None:
        """Print-only subcommands must be documented as NOT running launchctl load/unload."""
        text = _load_skill_text()
        assert re.search(
            r"(?i)(must not|do(es)? not|never).{0,80}(execute|run|invoke).{0,40}launchctl",
            text,
        ), "Print-only subcommands must explicitly forbid launchctl execution"

    def test_printonly_forbids_crontab_write(self) -> None:
        """Print-only subcommands must be documented as NOT editing crontab."""
        text = _load_skill_text()
        assert re.search(
            r"(?i)(must not|do(es)? not|never).{0,80}(execute|run|invoke|edit|write).{0,40}crontab",
            text,
        ), "Print-only subcommands must explicitly forbid crontab execution/edit"

    def test_printonly_forbids_plist_write(self) -> None:
        """Print-only subcommands must be documented as NOT writing plist files."""
        text = _load_skill_text()
        assert re.search(
            r"(?i)(must not|do(es)? not|never).{0,80}(write|create|install).{0,40}\.?plist",
            text,
        ), "Print-only subcommands must explicitly forbid plist file write"

    def test_user_retains_explicit_install_authority(self) -> None:
        """The skill must explicitly document that system-level installs are the user's step."""
        text = _load_skill_text().lower()
        assert re.search(
            r"(user|human).{0,100}(explicit|manual|authoriz|review).{0,100}(install|run|execute)|"
            r"(install|run|execute).{0,60}(user|human).{0,60}(explicit|manual|authoriz|review)",
            text,
        ), "Skill must document that install execution is explicitly the user's step"


class TestCommandFileSubcommandRouting:
    """The slash command preamble must route subcommand first-tokens to the skill body,
    not stub them out with 'not yet implemented' notices."""

    def test_command_file_does_not_stub_subcommands(self) -> None:
        text = COMMAND_PATH.read_text()
        assert "not yet implemented" not in text.lower(), (
            "nx/commands/rdr-audit.md still has 'not yet implemented' stub for subcommands — "
            "Phase 2b should remove that and route subcommands to the skill body"
        )

    def test_command_file_lists_all_subcommands(self) -> None:
        text = COMMAND_PATH.read_text().lower()
        for sc in ("list", "status", "history", "schedule", "unschedule"):
            assert sc in text, f"Command file does not reference subcommand `{sc}`"


class TestRegistryIntegration:

    def test_registered_in_yaml(self) -> None:
        registry = yaml.safe_load(REGISTRY_PATH.read_text())
        rdr_skills = registry.get("rdr_skills", {})
        assert "rdr-audit" in rdr_skills, (
            "rdr-audit must be registered under rdr_skills: in nx/registry.yaml"
        )

    def test_registry_entry_has_required_fields(self) -> None:
        registry = yaml.safe_load(REGISTRY_PATH.read_text())
        entry = registry.get("rdr_skills", {}).get("rdr-audit", {})
        for field in ("slash_command", "command_file", "description", "triggers"):
            assert field in entry, f"Registry entry missing field: {field}"
        assert entry["slash_command"] == "/rdr-audit"
        assert "dispatches_to" in entry
        assert "deep-research-synthesizer" in entry["dispatches_to"]

    def test_listed_in_using_nx_skills_table(self) -> None:
        text = USING_SKILLS_PATH.read_text()
        assert "rdr-audit" in text, (
            "rdr-audit must be added to the routing table in "
            "nx/skills/using-nx-skills/SKILL.md so it is discoverable each session"
        )
