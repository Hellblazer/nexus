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
#: The recipe lives in a file the skill loads on demand, keeping SKILL.md inside its context budget
#: (tests/scripts/test_context_budgets.py); SKILL.md keeps a pointer section under the same heading.
PROMOTION_PATH = SKILL_PATH.with_name("promotion.md")

SECTION_HEADING = "## Promoting a Memory File"

#: part name -> (subsection heading, patterns its body must match). The dict
#: order is the order the section's intro promises ("in this order").
PARTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "criterion": (
        "### The criterion",
        (
            r"How to apply",
            r"ordered steps",
            r"8\s*KB",
            r"bright line",
        ),
    ),
    "target": (
        "### The target",
        (
            r"A procedure library is",
            r"\.nexus/procedures/",
            # The shipped library is named by what it ships with, not by its
            # repo path: a plugin file may not name a repo-only path
            # (test_plugin_markdown_paths_lint), and src/nexus/procedures/ is
            # a package path in any case.
            r"ships with `nx`",
            # The script target is not available until the runner ships
            # (critic B1, 2026-11-08): the skill must say so itself.
            r"`procedure_run`",
            r"released package",
            # R1: a repo-library script also needs the repo root enabled.
            r"for a repo library, the repo root enabled",
            r"source stays whole",
            # A file holding both a fixed and a judgment part (critic B2).
            r"split by part",
            r"its own target",
            # The worked example baton's qo7ie.36 follows: fixed part to a
            # script, judgment part to a skill, and what to do until the
            # runner ships.
            r"`preflight`",
            r"judgment part becomes a skill",
            r"stays in the source or goes into the skill",
            r"hook-development",
            r"test first",
        ),
    ),
    "citation": (
        "### Citing the sources",
        (
            r"cites? its source files",
            r"file path or T2 title",
            r"pointer",
            r"call that runs it",
            r"rule and its why",
            r"Bidirectional Memory",
        ),
    ),
    "loss check": (
        "### The loss check",
        (
            r"`substantive-critic`",
            r"every rule",
            r"lacks",
            r"before the source shrinks",
            r"Ship when the list is empty",
            r"one source file at a time",
            r"union of the targets",
            r"whole source file",
            r"enumerates",
            r"0 ship-blockers",
            r"on the bead",
        ),
    ),
    "empty-list rule": (
        "### The empty-list rule",
        (
            r"only with an empty list",
            r"does not shrink",
            # When the source shrinks (critic B3, 2026-11-08): only once the
            # target is LIVE, after the empty list is recorded. Live differs
            # per target: a merged skill or hook is inert until the plugin
            # release that carries it is installed, a script until
            # procedure_run is released (and the repo root enabled for a repo
            # library). Shrinking at merge leaves a pointer to an inert target.
            r"only once the target is live",
            r"empty list is recorded on the bead",
            r"stays whole",
            r"script target is live once `procedure_run` is released",
            r"repo root is enabled",
            r"skill or hook target is live once the plugin release that carries it is installed",
            r"script or (?:a )?hook",
        ),
    ),
}

#: (cell of the "The memory holds" column, prefix of its Target cell). The
#: pairing is the point of the table: swapping two targets must fail.
TARGET_PAIRS: tuple[tuple[str, str], ...] = (
    ("judgment", "Skill"),
    ("fixed sequence", "Lua script"),
    ("bright line", "Hook"),
)


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


def _table_rows(body: str) -> list[list[str]]:
    """Cells of each table row (separator rows dropped). A row with fewer than
    two cells is kept short: callers must not index past what is there."""
    rows: list[list[str]] = []
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and all(set(c) <= set("-: ") for c in cells):
            continue
        rows.append(cells)
    return rows


def _pairs_ok(body: str) -> bool:
    rows = _table_rows(body)
    for holds, target in TARGET_PAIRS:
        if not any(
            len(r) >= 2 and holds in r[0].lower() and r[1].startswith(target)
            for r in rows
        ):
            return False
    return True


def missing_parts(text: str) -> list[str]:
    """Names of the required parts that *text* lacks, in PARTS order, plus
    ``order`` when every part is present but out of the promised order."""
    section = _section(text)
    if section is None:
        return list(PARTS)
    subs = _subsections(section)
    missing: list[str] = []
    for name, (heading, patterns) in PARTS.items():
        body = subs.get(heading)
        if body is None or not all(re.search(p, body, re.I) for p in patterns):
            missing.append(name)
        elif name == "target" and not _pairs_ok(body):
            missing.append(name)
    if not missing:
        at = [section.index(h) for h, _ in PARTS.values()]
        if at != sorted(at):
            missing.append("order")
    return missing


def _without(text: str, heading: str) -> str:
    """*text* with the subsection under *heading* cut out."""
    pattern = rf"(?ms)^{re.escape(heading)}\n.*?(?=^### |^## |\Z)"
    cut, n = re.subn(pattern, "", text, count=1)
    assert n == 1, f"fixture error: {heading!r} not found to remove"
    return cut


def test_skill_carries_all_five_promotion_parts() -> None:
    text = PROMOTION_PATH.read_text(encoding="utf-8")
    assert text.count(f"\n{SECTION_HEADING}\n") == 1, (
        f"{SKILL_PATH.relative_to(REPO_ROOT)} needs exactly one {SECTION_HEADING!r} section"
    )
    assert missing_parts(text) == []


@pytest.mark.parametrize("part", list(PARTS))
def test_checker_flags_a_draft_without_each_part(part: str) -> None:
    text = PROMOTION_PATH.read_text(encoding="utf-8")
    draft = _without(text, PARTS[part][0])
    assert missing_parts(draft) == [part]


def test_checker_flags_a_part_with_an_empty_body() -> None:
    """A heading alone is not the part."""
    text = PROMOTION_PATH.read_text(encoding="utf-8")
    heading = PARTS["loss check"][0]
    draft = re.sub(
        rf"(?ms)^{re.escape(heading)}\n.*?(?=^### |^## |\Z)", f"{heading}\n\n", text, count=1
    )
    assert missing_parts(draft) == ["loss check"]


def test_checker_flags_a_target_table_without_the_hook_row() -> None:
    text = PROMOTION_PATH.read_text(encoding="utf-8")
    draft = "\n".join(
        line for line in text.splitlines()
        if not (line.startswith("|") and "bright line" in line.lower())
    )
    assert missing_parts(draft) == ["target"]


def test_checker_flags_swapped_targets_in_the_table() -> None:
    """Skill and Hook trade places: every name is still in the column."""
    text = PROMOTION_PATH.read_text(encoding="utf-8")
    draft = (
        text.replace("| Skill |", "| @@ |")
        .replace("| Hook |", "| Skill |")
        .replace("| @@ |", "| Hook |")
    )
    assert draft != text
    assert missing_parts(draft) == ["target"]


def test_checker_survives_a_short_table_row() -> None:
    text = PROMOTION_PATH.read_text(encoding="utf-8")
    draft = text.replace("| --- | --- | --- | --- |", "| --- | --- | --- | --- |\n| x |", 1)
    assert draft != text
    assert missing_parts(draft) == []


def test_checker_flags_parts_out_of_order() -> None:
    text = PROMOTION_PATH.read_text(encoding="utf-8")
    section = _section(text)
    assert section is not None
    subs = _subsections(section)
    first, second = PARTS["criterion"][0], PARTS["target"][0]
    # A body starts with its own newline, so heading + body is the exact slice.
    swapped = section.replace(f"{first}{subs[first]}", "@@1@@").replace(
        f"{second}{subs[second]}", "@@2@@"
    )
    swapped = swapped.replace("@@1@@", f"{second}{subs[second]}").replace(
        "@@2@@", f"{first}{subs[first]}"
    )
    draft = text.replace(section, swapped)
    assert draft != text
    assert missing_parts(draft) == ["order"]


def _pattern_cases() -> list[tuple[str, str]]:
    return [(part, pat) for part, (_, pats) in PARTS.items() for pat in pats]


@pytest.mark.parametrize(("part", "pattern"), _pattern_cases())
def test_every_pinned_phrase_is_load_bearing(part: str, pattern: str) -> None:
    """Delete one pinned phrase from its part: the checker names that part.
    A phrase the checker would not miss is not pinned."""
    text = PROMOTION_PATH.read_text(encoding="utf-8")
    section = _section(text)
    assert section is not None
    heading = PARTS[part][0]
    body = _subsections(section)[heading]
    cut = re.sub(pattern, "", body, flags=re.I)
    assert cut != body, f"{pattern!r} matched nothing in {part!r}"
    draft = text.replace(body, cut)
    assert part in missing_parts(draft)


def test_checker_flags_a_skill_without_the_section() -> None:
    text = PROMOTION_PATH.read_text(encoding="utf-8")
    assert missing_parts(text.replace(SECTION_HEADING, "## Something Else")) == list(PARTS)


def _frontmatter_description(text: str) -> str:
    m = re.search(r"^description:\s*(.*)$", text, re.M)
    assert m, "no description line"
    return m.group(1)


def test_description_names_promotion_and_skill_points_at_the_recipe() -> None:
    """An agent promoting a memory file must be able to find this skill (the description is what
    the Skill tool lists, critic N4) and, from it, the recipe file it loads on demand."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert re.search(r"memory file", _frontmatter_description(text), re.I)
    section = _section(text)
    assert section is not None, "SKILL.md lost its Promoting a Memory File pointer section"
    assert "promotion.md" in section
    assert PROMOTION_PATH.is_file()
    assert missing_parts(text) == list(PARTS), "the recipe belongs in promotion.md, not SKILL.md"
