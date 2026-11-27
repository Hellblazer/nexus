# SPDX-License-Identifier: AGPL-3.0-or-later
"""The where-it-goes guidance for ``identity/<id>`` notes (RDR-253 Step 6 part 3, nexus-hczdo) stays in the nexus skill.

A pin, not a style check: the skill is the one place sessions are told where a note goes, and the 30-day retire rule
removes this text on purpose, in the same change that drops this test.
"""
from __future__ import annotations

from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "conexus" / "skills" / "nexus" / "SKILL.md"


def test_the_nexus_skill_says_where_each_kind_of_note_goes() -> None:
    text = SKILL.read_text(encoding="utf-8")
    section = text.split("## Where a note goes", 1)[1].split("\n## ", 1)[0]
    for needle in ("`identity/<id>`", "standing context", "fact other sessions need", "memory files", "the user's own standing rules",
                   "threads board", 'memory_put(project="identity/<id>"', "nx identity show", "Seats only"):
        assert needle in section, needle
