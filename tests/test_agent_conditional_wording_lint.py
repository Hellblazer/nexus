# SPDX-License-Identifier: AGPL-3.0-or-later
"""The agent markdown does not assume the user tracks work in beads (nexus-sby49 Phase 1).

Agent, shared-protocol and orchestration-skill markdown has no hook point: it
reaches every user as written. A user with no ``.beads/`` directory and no ``bd``
on PATH would otherwise be told to run ``bd ready`` and ``bd create`` and have the
calls fail, and told to run nexus-tool steps that cannot run when the conexus MCP
tools are absent.

The rules, over ``conexus/agents/*.md``, ``conexus/resources/agent-shared/*.md`` and
``conexus/skills/orchestration/SKILL.md``:

1. Every line that runs a ``bd`` command sits under the conditional: the line, or an
   earlier line of the same section (since the last heading, code fences ignored),
   names the ``.beads/`` directory.
2. Every agent that names a ``mcp__plugin_conexus_nexus__`` tool carries the
   "tools are not available" skip clause for its pre-flight and post-flight.
3. The conditionals are not decoration: more than zero ``bd`` lines were examined.

The checker is exercised on fixture text, so a lint that stopped looking fails here.
"""
from __future__ import annotations

import pathlib
import re

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
CONEXUS = PROJECT_ROOT / "conexus"

GLOBS = (
    "agents/*.md",
    "resources/agent-shared/*.md",
    "skills/orchestration/SKILL.md",
)
#: A line that runs (or tells the reader to run) a ``bd`` command.
BD_COMMAND = re.compile(r"(?<![\w/.-])bd(?= (?:ready|list|show|create|update|close|dep|dolt|note|search|blocked|stats|sync)\b)")
CONDITIONAL = ".beads/"
SKIP_CLAUSE = "are not available in this session"


def covered_files() -> list[pathlib.Path]:
    return sorted(p for g in GLOBS for p in CONEXUS.glob(g))


def unguarded_bd_lines(text: str) -> tuple[int, list[tuple[int, str]]]:
    """``(examined, [(line number, line)])`` for the ``bd`` command lines not under the conditional."""
    examined = 0
    bad: list[tuple[int, str]] = []
    in_fence = False
    section_has_conditional = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(r"#{1,6} ", line):
            section_has_conditional = False
        if CONDITIONAL in line:
            section_has_conditional = True
        if BD_COMMAND.search(line):
            examined += 1
            if not section_has_conditional:
                bad.append((number, line.strip()[:140]))
    return examined, bad


# ---------------------------------------------------------------------------
# The checker, on fixtures
# ---------------------------------------------------------------------------


def test_the_checker_accepts_a_guarded_line_and_a_guarded_section():
    ok = "## A\n\nWhere the project has a `.beads/` directory, run `bd ready`.\n\n## B\n\nApplies where `.beads/` exists.\n\n- bd create x\n"
    assert unguarded_bd_lines(ok) == (2, [])


def test_the_checker_flags_an_unguarded_line_and_resets_at_a_heading():
    text = "## A\n\nUse `.beads/` here.\n\n## B\n\nRun bd ready first.\n"
    examined, bad = unguarded_bd_lines(text)
    assert examined == 1 and [n for n, _ in bad] == [7]


def test_the_checker_ignores_headings_inside_a_code_fence_and_prose_uses_of_bd():
    text = "## A\n\n`.beads/`\n\n```\n# not a heading\nbd ready\n```\n\nThe word bd alone, or bdx ready, or a/bd list is not a command.\n"
    examined, bad = unguarded_bd_lines(text)
    assert examined == 1 and bad == []


# ---------------------------------------------------------------------------
# The real files
# ---------------------------------------------------------------------------


def test_every_bd_command_line_sits_under_the_beads_conditional():
    files = covered_files()
    assert len(files) >= 13, f"only {len(files)} files matched the globs"
    examined = 0
    problems: list[str] = []
    for path in files:
        n, bad = unguarded_bd_lines(path.read_text(encoding="utf-8"))
        examined += n
        problems += [f"{path.relative_to(PROJECT_ROOT)}:{ln}: {text}" for ln, text in bad]
    assert examined > 0, "no bd command line was examined: the lint is blind"
    assert problems == [], "\n".join(problems)


def test_every_agent_that_names_the_nexus_tools_says_to_skip_them_when_absent():
    agents = sorted((CONEXUS / "agents").glob("*.md"))
    named = [p for p in agents if "mcp__plugin_conexus_nexus__" in p.read_text(encoding="utf-8")]
    assert len(named) == len(agents) == 10, (len(named), len(agents))
    missing = [p.name for p in named if SKIP_CLAUSE not in p.read_text(encoding="utf-8")]
    assert missing == [], missing


def test_a_hard_gate_names_the_way_out_when_the_tools_are_absent():
    gated = 0
    for p in sorted((CONEXUS / "agents").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        for block in re.findall(r"<HARD-GATE>.*?</HARD-GATE>", text, re.S):
            gated += 1
            assert SKIP_CLAUSE in block, f"{p.name}: a HARD-GATE demands a nexus tool call with no way out"
    assert gated >= 5, f"only {gated} HARD-GATE blocks found"


def test_the_developer_keeps_the_stacked_review_sentence_and_adds_only_the_two_clauses():
    text = (CONEXUS / "agents" / "developer.md").read_text(encoding="utf-8")
    assert "Do NOT self-close the bead or self-commit; that bypasses the stacked-review gate." in text
    assert text.count("unless your brief tells you to commit") + text.count("unless your brief tells you to") >= 4
    assert text.count("in a linked worktree your brief assigned you, commit as the brief says") >= 4
