# SPDX-License-Identifier: AGPL-3.0-or-later
"""Both review agents mandate one terminal verdict block and one vocabulary.

``substantive-critic`` has carried a mandated ``- **outcome**:`` block with
three literal values since RDR-069; ``code-review-expert`` mandated nothing,
and its records closed in fifteen different spellings, so a census could
not classify them (intrastate survey [26111] Q4, plan N8, bead
nexus-4hoc0). File-only, so it runs in the ``-m lint`` job.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

AGENTS_DIR = Path(__file__).resolve().parent.parent / "conexus" / "agents"
REVIEW_AGENTS = ("substantive-critic.md", "code-review-expert.md")
OUTCOMES = ("justified", "partial", "not-justified")


def verdict_mandate_gaps(text: str) -> list[str]:
    """What a review agent's definition lacks of the verdict mandate."""
    gaps: list[str] = []
    if "- **outcome**:" not in text:
        gaps.append("no literal `- **outcome**:` line")
    if not re.search(r"MUST be one of exactly three literal strings", text):
        gaps.append("no 'exactly three literal strings' mandate")
    for value in OUTCOMES:
        if f"`{value}`" not in text:
            gaps.append(f"outcome value `{value}` not named")
    return gaps


@pytest.mark.parametrize("name", REVIEW_AGENTS)
def test_review_agent_mandates_the_verdict_block(name: str) -> None:
    path = AGENTS_DIR / name
    assert path.is_file(), f"review agent definition moved: {path}"
    assert verdict_mandate_gaps(path.read_text(encoding="utf-8")) == []


def test_both_agents_name_the_same_example_field_set() -> None:
    fields = []
    for name in REVIEW_AGENTS:
        text = (AGENTS_DIR / name).read_text(encoding="utf-8")
        fields.append(set(re.findall(r"^- \*\*([a-z_]+)\*\*:", text, re.MULTILINE)))
    assert fields[0] == fields[1], f"verdict fields differ: {fields[0] ^ fields[1]}"
    assert {"outcome", "confidence", "critical_count", "significant_count", "ship_blockers", "summary"} <= fields[0]


def test_a_definition_without_the_mandate_is_caught() -> None:
    planted = "## Output\n\nEnd with PASS, APPROVE or NOT READY as fits.\n"
    assert verdict_mandate_gaps(planted)[0] == "no literal `- **outcome**:` line"
    assert len(verdict_mandate_gaps(planted)) == 5
