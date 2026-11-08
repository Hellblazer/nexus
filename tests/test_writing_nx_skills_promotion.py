# SPDX-License-Identifier: AGPL-3.0-or-later
"""The promotion section of the writing-nx-skills skill carries its five parts.

RDR-240 Technical Design 9 (nexus-qo7ie.35): a recipe for moving a memory file
into a skill, a script or a hook. Its five parts are the criterion, the target
choice, the citation of sources, the loss check and the empty-list rule. A
recipe missing the loss check or the empty-list rule is the one that lets a
promotion shrink its source and drop a rule (RDR-240 Risks), so each part is
pinned here by the words that carry its meaning, not by its heading alone: a
heading with an empty body would otherwise pass.

``test_checker_flags_a_draft_without_each_part`` is the non-vacuity half. It
removes one part at a time from the real section and requires the checker to
name exactly that part, so a checker that cannot fail fails this file.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SKILL_PATH = REPO_ROOT / "conexus" / "skills" / "writing-nx-skills" / "SKILL.md"

SECTION_HEADING = "## Promoting a Memory File"

#: part name -> (subsection heading, patterns its body must match).
PARTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "criterion": (
        "### The criterion",
        (
            r"How to apply",
            r"ordered steps",
            r"8 KB",
            r"bright line",
        ),
    ),
    "target": (
        "### The target",
        (
            r"procedure library",
            r"\.nexus/procedures/",
            # The shipped library is named by what it ships with, not by its
            # repo path: a plugin file may not name a repo-only path
            # (test_plugin_markdown_paths_lint), and src/nexus/procedures/ is
            # a package path in any case.
            r"ships with `nx`",
        ),
    ),
    "citation": (
        "### Citing the sources",
        (
            r"cites? its source files",
            r"pointer",
            r"rule and its why",
        ),
    ),
    "loss check": (
        "### The loss check",
        (
            r"`substantive-critic`",
            r"every rule",
            r"lacks",
            r"before the source shrinks",
        ),
    ),
    "empty-list rule": (
        "### The empty-list rule",
        (
            r"only with an empty list",
            r"script or (?:a )?hook",
        ),
    ),
}

#: The target table must name all three targets in its Target column.
TARGET_ROWS: tuple[str, ...] = ("Skill", "Lua script", "Hook")


def _section(text: str) -> str | None:
    start = text.find(f"\n{SECTION_HEADING}\n")
    if start < 0:
        return None
    start += 1
    nxt = text.find("\n## ", start + len(SECTION_HEADING))
    return text[start:] if nxt < 0 else text[start:nxt]


def _subsections(section: str) -> dict[str, str]:
    """Subsection heading line -> body, split on ``### `` lines."""
    pieces = re.split(r"(?m)^(### .+)$", section)
    return {pieces[i].strip(): pieces[i + 1] for i in range(1, len(pieces) - 1, 2)}


def _target_column(body: str) -> list[str]:
    cells: list[str] = []
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        row = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(row) >= 2 and not set(row[1]) <= set("-: "):
            cells.append(row[1])
    return cells


def missing_parts(text: str) -> list[str]:
    """Names of the required parts that *text* lacks, in PARTS order."""
    section = _section(text)
    if section is None:
        return list(PARTS)
    subs = _subsections(section)
    missing: list[str] = []
    for name, (heading, patterns) in PARTS.items():
        body = subs.get(heading)
        if body is None or not all(re.search(p, body, re.I) for p in patterns):
            missing.append(name)
            continue
        if name == "target":
            column = _target_column(body)
            if not all(any(c.startswith(row) for c in column) for row in TARGET_ROWS):
                missing.append(name)
    return missing


def _without(text: str, heading: str) -> str:
    """*text* with the subsection under *heading* cut out."""
    pattern = rf"(?ms)^{re.escape(heading)}\n.*?(?=^### |^## |\Z)"
    cut, n = re.subn(pattern, "", text, count=1)
    assert n == 1, f"fixture error: {heading!r} not found to remove"
    return cut


def test_skill_carries_all_five_promotion_parts() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert text.count(f"\n{SECTION_HEADING}\n") == 1, (
        f"{SKILL_PATH.relative_to(REPO_ROOT)} needs exactly one {SECTION_HEADING!r} section"
    )
    assert missing_parts(text) == []


@pytest.mark.parametrize("part", list(PARTS))
def test_checker_flags_a_draft_without_each_part(part: str) -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    draft = _without(text, PARTS[part][0])
    assert missing_parts(draft) == [part]


def test_checker_flags_a_part_with_an_empty_body() -> None:
    """A heading alone is not the part."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    heading = PARTS["loss check"][0]
    draft = re.sub(
        rf"(?ms)^{re.escape(heading)}\n.*?(?=^### |^## |\Z)", f"{heading}\n\n", text, count=1
    )
    assert missing_parts(draft) == ["loss check"]


def test_checker_flags_a_target_table_without_the_hook_row() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    draft = "\n".join(
        line for line in text.splitlines()
        if not (line.startswith("|") and line.split("|")[2].strip().startswith("Hook"))
    )
    assert missing_parts(draft) == ["target"]


def test_checker_flags_a_skill_without_the_section() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert missing_parts(text.replace(SECTION_HEADING, "## Something Else")) == list(PARTS)
