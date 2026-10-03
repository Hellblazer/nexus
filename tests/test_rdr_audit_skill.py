# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural tests for the conexus:rdr-audit skill (RDR-067 Phase 2a).

The skill's prose (frontmatter fields, section headings, the transcript-mining
pre-step wording) is not pinned here: ``test_plugin_structure.py`` owns the
skill-structure invariants. What stays is the wiring a user can break by
renaming or unregistering something: the skill and command exist, the
registry and the routing table name the skill, and the command routes every
management subcommand.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
SKILL_PATH = REPO_ROOT / "conexus" / "skills" / "rdr-audit-checklist" / "SKILL.md"
COMMAND_PATH = REPO_ROOT / "conexus" / "commands" / "rdr-audit.md"
REGISTRY_PATH = REPO_ROOT / "conexus" / "registry.yaml"
USING_SKILLS_PATH = REPO_ROOT / "conexus" / "skills" / "using-nx-skills" / "SKILL.md"


def test_skill_and_command_files_exist() -> None:
    assert SKILL_PATH.exists(), f"missing skill file: {SKILL_PATH}"
    assert COMMAND_PATH.exists(), f"missing slash command file (needed for /conexus:rdr-audit): {COMMAND_PATH}"


def test_skill_is_registered_and_routed() -> None:
    registry = yaml.safe_load(REGISTRY_PATH.read_text())
    entry = registry.get("rdr_skills", {}).get("rdr-audit-checklist")
    assert entry is not None, "rdr-audit-checklist must be registered under rdr_skills: in conexus/registry.yaml"
    for field in ("slash_command", "command_file", "description", "triggers", "dispatches_to"):
        assert field in entry, f"registry entry missing field: {field}"
    assert entry["slash_command"] == "/rdr-audit"
    assert "deep-research-synthesizer" in entry["dispatches_to"]
    assert "rdr-audit" in USING_SKILLS_PATH.read_text(), (
        "rdr-audit must stay in the using-nx-skills routing table so it is discoverable each session"
    )


@pytest.mark.parametrize("subcommand", ["list", "status", "history"])
def test_command_routes_every_subcommand(subcommand: str) -> None:
    """The slash command routes each management subcommand's first token to the
    skill body instead of stubbing it out."""
    text = COMMAND_PATH.read_text().lower()
    assert "not yet implemented" not in text, (
        "conexus/commands/rdr-audit.md still stubs subcommands with 'not yet implemented'"
    )
    assert subcommand in text, f"command file does not reference subcommand `{subcommand}`"
