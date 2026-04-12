# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural tests for the RDR-067 Phase 3 incident template.

Verifies that nx/resources/rdr_process/INCIDENT-TEMPLATE.md has the exact
frontmatter fields and sections required by the canonical audit prompt
(T2 nexus_rdr/067-canonical-prompt-v1) and that the skill body references
the template correctly.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TEMPLATE = REPO_ROOT / "nx" / "resources" / "rdr_process" / "INCIDENT-TEMPLATE.md"
SKILL = REPO_ROOT / "nx" / "skills" / "rdr-audit" / "SKILL.md"


def _template_text() -> str:
    assert TEMPLATE.exists(), f"{TEMPLATE} does not exist"
    return TEMPLATE.read_text()


class TestIncidentTemplateExists:

    def test_file_exists(self) -> None:
        assert TEMPLATE.exists()

    def test_resources_directory_structure(self) -> None:
        """The template lives under nx/resources/rdr_process/ so it ships with the plugin."""
        assert TEMPLATE.parent.name == "rdr_process"
        assert TEMPLATE.parent.parent.name == "resources"


class TestFrontmatterSchema:
    """The frontmatter schema is the interface to the audit subagent —
    drift_class enum values in particular must match the canonical prompt's taxonomy."""

    REQUIRED_FIELDS = (
        "project", "rdr", "incident_date", "drift_class", "caught_by", "outcome",
    )

    DRIFT_CLASSES = ("unwiring", "dim-mismatch", "deferred-integration", "other")
    CAUGHT_BY = ("substantive-critic", "composition-probe", "dim-contracts", "user", "post-hoc")
    OUTCOMES = ("reopened", "partial", "shipped-silently")

    def test_template_has_frontmatter_example(self) -> None:
        """The template body must include a YAML frontmatter example block."""
        text = _template_text()
        assert re.search(r"\n---\n.*?\n---\n", text, re.DOTALL), (
            "Template must include a YAML frontmatter example block"
        )

    def test_all_required_fields_present(self) -> None:
        text = _template_text()
        fm_match = re.search(r"\n---\n(.*?)\n---\n", text, re.DOTALL)
        assert fm_match
        fm_block = fm_match.group(1)
        for field in self.REQUIRED_FIELDS:
            assert re.search(rf"^\s*{field}:", fm_block, re.MULTILINE), (
                f"Frontmatter missing required field: {field}"
            )

    def test_drift_class_enum_matches_canonical_taxonomy(self) -> None:
        """drift_class enum values must exactly match the sub-pattern taxonomy
        documented in the canonical audit prompt."""
        text = _template_text()
        # Search outside the frontmatter for the enum list (in the How-to-file or Mechanism section)
        for dc in self.DRIFT_CLASSES:
            assert dc in text, f"drift_class enum value `{dc}` missing from template documentation"

    def test_caught_by_enum_matches(self) -> None:
        text = _template_text()
        for cb in self.CAUGHT_BY:
            assert cb in text, f"caught_by enum value `{cb}` missing from template documentation"

    def test_outcome_enum_matches(self) -> None:
        text = _template_text()
        for outcome in self.OUTCOMES:
            assert outcome in text, f"outcome enum value `{outcome}` missing from template documentation"


class TestRequiredSections:
    """The 8 narrative sections from RDR-067 §Technical Design."""

    REQUIRED_SECTIONS = (
        "What was meant to be delivered",
        "What was actually delivered",
        "The gap",
        "Decision point",
        "Mechanism",
        "What caught it",
        "Cost",
        "Lessons",
    )

    def test_all_sections_present(self) -> None:
        text = _template_text()
        for section in self.REQUIRED_SECTIONS:
            assert f"## {section}" in text, f"Missing required section: ## {section}"

    def test_sections_in_canonical_order(self) -> None:
        text = _template_text()
        positions = []
        for section in self.REQUIRED_SECTIONS:
            idx = text.find(f"## {section}")
            assert idx >= 0
            positions.append(idx)
        assert positions == sorted(positions), (
            "Sections must appear in canonical order (what was meant → shipped → gap → "
            "decision point → mechanism → what caught → cost → lessons)"
        )


class TestHowToFileInstructions:

    def test_has_how_to_file_guidance(self) -> None:
        text = _template_text()
        assert "How to file" in text or "how to file" in text

    def test_documents_memory_put_invocation(self) -> None:
        text = _template_text()
        # Must show the exact memory_put call shape
        assert "memory_put" in text
        assert "rdr_process" in text

    def test_documents_title_convention(self) -> None:
        """Titles follow `<project>-incident-<slug>` convention."""
        text = _template_text()
        assert re.search(r"<project>-incident-<[^>]+>|project-incident-slug", text, re.IGNORECASE)

    def test_distinguishes_healthy_rescoping_from_silent_reduction(self) -> None:
        """The template must explicitly tell authors when NOT to file (healthy rescoping)."""
        text = _template_text().lower()
        assert "healthy rescoping" in text or "healthy re-scoping" in text, (
            "Template must distinguish healthy rescoping from silent scope reduction"
        )
        assert "does not" in text or "doesn't" in text or "does not belong" in text


class TestSkillReferencesTemplate:

    def test_skill_body_references_template(self) -> None:
        text = SKILL.read_text()
        assert "INCIDENT-TEMPLATE.md" in text, (
            "Skill body should reference the cross-project incident template file"
        )

    def test_skill_has_cross_project_filings_section(self) -> None:
        text = SKILL.read_text()
        assert "Cross-Project Incident" in text or "cross-project incident" in text.lower(), (
            "Skill body should have a section introducing the incident filing workflow"
        )

    def test_skill_documents_title_format(self) -> None:
        text = SKILL.read_text()
        assert "<project>-incident-<slug>" in text or "project-incident-" in text, (
            "Skill body should document the T2 title convention for filings"
        )
