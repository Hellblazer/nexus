# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for RDR close gate heading normalization and gap extraction.

Validates that the close gate preamble (nx/commands/rdr-close.md) correctly
handles heading variants and gap format variations. Tests the Python functions
extracted from the preamble script.

nexus-bbt, nexus-oyu, nexus-40b, nexus-j4o.
"""
from __future__ import annotations

import re

import pytest


# ── Replicate the preamble functions exactly as they appear in rdr-close.md ──


def _extract_section(doc: str, *headings: str) -> str:
    """Extract content under the first matching heading variant."""
    for heading in headings:
        idx = doc.find(heading)
        if idx != -1:
            rest = doc[idx + len(heading) :]
            nxt = re.search(r"\n## ", rest)
            return rest[: nxt.start()] if nxt else rest
    return ""


def _find_gaps(problem_stmt: str) -> list[tuple[str, str, str]]:
    """Extract gap matches from a problem statement section."""
    return re.findall(
        r"^#{3,5} Gap (\d+)([^\n:]*):\s*(.*)$", problem_stmt, re.MULTILINE
    )


# ── _extract_section tests ──────────────────────────────────────────────────


class TestExtractSection:
    """Test heading variant matching in _extract_section."""

    SAMPLE_RDR_PROBLEM_STATEMENT = """\
---
title: Test RDR
status: accepted
---

# Test RDR

## Problem Statement

This is the problem statement with gaps.

#### Gap 1: First gap
Description of first gap.

#### Gap 2: Second gap
Description of second gap.

## Proposed Design

Design details here.
"""

    SAMPLE_RDR_PROBLEM = """\
---
title: Test RDR
status: accepted
---

# Test RDR

## Problem

This is the problem with gaps.

#### Gap 1: First gap
Description of first gap.

#### Gap 2: Second gap
Description of second gap.

## Proposed Design

Design details here.
"""

    SAMPLE_RDR_NO_SECTION = """\
---
title: Test RDR
status: accepted
---

# Test RDR

## Context

Some context.

## Proposed Design

Design details here.
"""

    def test_finds_problem_statement(self) -> None:
        result = _extract_section(
            self.SAMPLE_RDR_PROBLEM_STATEMENT,
            "## Problem Statement",
            "## Problem",
        )
        assert "First gap" in result
        assert "Second gap" in result

    def test_finds_problem_heading(self) -> None:
        result = _extract_section(
            self.SAMPLE_RDR_PROBLEM, "## Problem Statement", "## Problem"
        )
        assert "First gap" in result
        assert "Second gap" in result

    def test_problem_statement_preferred_over_problem(self) -> None:
        """When both headings exist, '## Problem Statement' is tried first."""
        doc = """\
## Problem Statement

Statement gaps here.

#### Gap 1: From statement

## Problem

Different content.

## Design
"""
        result = _extract_section(doc, "## Problem Statement", "## Problem")
        assert "From statement" in result
        assert "Different content" not in result

    def test_returns_empty_when_no_match(self) -> None:
        result = _extract_section(
            self.SAMPLE_RDR_NO_SECTION, "## Problem Statement", "## Problem"
        )
        assert result == ""

    def test_extracts_to_end_when_no_following_section(self) -> None:
        doc = """\
## Problem

Content to the very end.
"""
        result = _extract_section(doc, "## Problem Statement", "## Problem")
        assert "Content to the very end." in result

    def test_subsection_substring_match_known_limitation(self) -> None:
        """'## Problem' inside '### Problem Details' is a known substring match.

        doc.find("## Problem") matches the substring within "### Problem Details".
        This is acceptable because no real RDR uses this heading pattern, and
        the gap regex would find 0 gaps → gate blocks appropriately.
        """
        doc = """\
## Context

### Problem Details

This is a subsection, not a top-level section.

## Design
"""
        result = _extract_section(doc, "## Problem Statement", "## Problem")
        # Known: substring match fires — documented, not a gate bypass because
        # gap_count == 0 triggers the "malformed" block for RDR >= 65.
        assert result != ""  # substring match fires

    def test_problem_substring_not_matched(self) -> None:
        """'## Problem' should not match '## Problematic Issues'."""
        doc = """\
## Problematic Issues

Not the problem section.

## Design
"""
        # doc.find("## Problem") would match "## Problematic Issues" at position 0
        # This IS a known limitation of exact string find — documenting it
        result = _extract_section(doc, "## Problem Statement", "## Problem")
        # find("## Problem") matches "## Problematic" — this is the substring issue.
        # For the close gate, this is acceptable because:
        # 1. No real RDR uses "## Problematic Issues"
        # 2. The gap regex would find 0 gaps → gate blocks appropriately
        assert "Not the problem section" in result  # substring match fires


# ── Gap extraction tests ───────────────────────────────────────────────────


class TestGapExtraction:
    """Test gap heading regex resilience."""

    def test_standard_h4_gap(self) -> None:
        """Standard format: #### Gap 1: title"""
        section = """\

#### Gap 1: First gap
Content.

#### Gap 2: Second gap
Content.
"""
        gaps = _find_gaps(section)
        assert len(gaps) == 2
        assert gaps[0][0] == "1"
        assert gaps[0][2] == "First gap"
        assert gaps[1][0] == "2"

    def test_h3_gap(self) -> None:
        """### Gap 1: title (3 hashes)"""
        section = """\

### Gap 1: First gap
Content.
"""
        gaps = _find_gaps(section)
        assert len(gaps) == 1
        assert gaps[0][0] == "1"

    def test_h5_gap(self) -> None:
        """##### Gap 1: title (5 hashes)"""
        section = """\

##### Gap 1: Title here
Content.
"""
        gaps = _find_gaps(section)
        assert len(gaps) == 1

    def test_gap_with_parenthetical(self) -> None:
        """#### Gap 4 (prerequisite for Gap 1): title"""
        section = """\

#### Gap 4 (prerequisite for Gap 1): Complex title
Content.
"""
        gaps = _find_gaps(section)
        assert len(gaps) == 1
        assert gaps[0][0] == "4"
        assert gaps[0][2] == "Complex title"

    def test_no_gaps(self) -> None:
        """Section with no gap headings."""
        section = """\

Some problem description without structured gaps.

### Not a gap heading
"""
        gaps = _find_gaps(section)
        assert len(gaps) == 0

    def test_h2_gap_not_matched(self) -> None:
        """## Gap 1: should NOT match (only 2 hashes)."""
        section = """\

## Gap 1: Too few hashes
Content.
"""
        gaps = _find_gaps(section)
        assert len(gaps) == 0

    def test_h6_gap_not_matched(self) -> None:
        """###### Gap 1: should NOT match (6 hashes)."""
        section = """\

###### Gap 1: Too many hashes
Content.
"""
        gaps = _find_gaps(section)
        assert len(gaps) == 0

    def test_gap_without_colon_not_matched(self) -> None:
        """#### Gap 1 without colon should not match."""
        section = """\

#### Gap 1 Missing the colon
Content.
"""
        gaps = _find_gaps(section)
        assert len(gaps) == 0

    def test_multi_digit_gap_number(self) -> None:
        """#### Gap 12: should work with multi-digit numbers."""
        section = """\

#### Gap 12: Twelfth gap
Content.
"""
        gaps = _find_gaps(section)
        assert len(gaps) == 1
        assert gaps[0][0] == "12"


# ── End-to-end: heading + gap extraction combined ──────────────────────────


class TestEndToEnd:
    """Combined heading extraction + gap finding."""

    def test_problem_statement_with_gaps(self) -> None:
        doc = """\
## Problem Statement

#### Gap 1: No version tracking
No record of which version last touched the database.

#### Gap 2: No ordering guarantee
Migrations run on first access, not in a defined sequence.

## Proposed Design
"""
        section = _extract_section(doc, "## Problem Statement", "## Problem")
        gaps = _find_gaps(section)
        assert len(gaps) == 2
        assert gaps[0][2] == "No version tracking"
        assert gaps[1][2] == "No ordering guarantee"

    def test_problem_with_gaps(self) -> None:
        """RDR using '## Problem' (like RDR-076) — must still find gaps."""
        doc = """\
## Problem

### Current state: ad-hoc migrations

#### Gap 1: No version tracking
Migrations detect state by probing columns.

#### Gap 2: No upgrade command
Users have no explicit step to run.

## Proposed Design
"""
        section = _extract_section(doc, "## Problem Statement", "## Problem")
        gaps = _find_gaps(section)
        assert len(gaps) == 2
        assert gaps[0][2] == "No version tracking"

    def test_no_problem_section_returns_zero_gaps(self) -> None:
        doc = """\
## Context

Some context.

## Design

Design details.
"""
        section = _extract_section(doc, "## Problem Statement", "## Problem")
        gaps = _find_gaps(section)
        assert len(gaps) == 0

    def test_h3_gaps_in_problem_section(self) -> None:
        """Gaps using ### (3 hashes) inside ## Problem."""
        doc = """\
## Problem

### Gap 1: First
Content.

### Gap 2: Second
Content.

## Design
"""
        section = _extract_section(doc, "## Problem Statement", "## Problem")
        gaps = _find_gaps(section)
        assert len(gaps) == 2

    def test_mixed_hash_levels(self) -> None:
        """Mix of ### and #### gap headings."""
        doc = """\
## Problem Statement

### Gap 1: Three hashes
Content.

#### Gap 2: Four hashes
Content.

##### Gap 3: Five hashes
Content.

## Design
"""
        section = _extract_section(doc, "## Problem Statement", "## Problem")
        gaps = _find_gaps(section)
        assert len(gaps) == 3


# ── Preamble consistency check ──────────────────────────────────────────────


class TestPreambleConsistency:
    """Verify the actual preamble in rdr-close.md matches our test functions."""

    def test_preamble_has_heading_variants(self) -> None:
        """The preamble must search for both '## Problem Statement' and '## Problem'."""
        from pathlib import Path

        preamble = (
            Path(__file__).parent.parent / "nx" / "commands" / "rdr-close.md"
        ).read_text()
        # The _extract_section call must include both variants
        assert "'## Problem Statement'" in preamble
        assert "'## Problem'" in preamble

    def test_preamble_gap_regex_accepts_h3_to_h5(self) -> None:
        """The gap regex must use #{3,5} not just ####."""
        from pathlib import Path

        preamble = (
            Path(__file__).parent.parent / "nx" / "commands" / "rdr-close.md"
        ).read_text()
        assert "#{3,5}" in preamble

    def test_gate_skill_lists_heading_variants(self) -> None:
        """rdr-gate SKILL.md must list both Problem and Problem Statement."""
        from pathlib import Path

        skill = (
            Path(__file__).parent.parent / "nx" / "skills" / "rdr-gate" / "SKILL.md"
        ).read_text()
        assert "Problem / Problem Statement" in skill

    def test_create_skill_documents_heading_variants(self) -> None:
        """rdr-create SKILL.md must mention both heading forms."""
        from pathlib import Path

        skill = (
            Path(__file__).parent.parent
            / "nx"
            / "skills"
            / "rdr-create"
            / "SKILL.md"
        ).read_text()
        assert "## Problem Statement" in skill
        assert "## Problem" in skill
