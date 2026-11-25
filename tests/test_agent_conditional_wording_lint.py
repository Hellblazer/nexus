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

Phase 3 (nexus-sby49.4) adds three rules over the wider set (agents, the shared protocol, the
resources, every skill and every command), because Phase 1 shipped "elsewhere put them in the plan
document" and no check read where an else-branch sends the work (T2 nexus/rca-undefined-plan-document-fallback-2026-11-25):

4. Every else-branch of a portability condition (``.beads/``, "tools are not available", "plan
   document") names a destination: a backticked ``mcp__plugin_`` tool, a backticked path, the
   hand-back, a skip, or a term in ``DEFINED_TERMS`` that the test resolves to a heading.
5. A line that names the plan-library tools (``answer_plan_*``, the deprecated ``plan_*``, ``nx
   answer-plan``) together with implementation, pipeline, phase, epic, dispatch or "starting structure" carries
   a negation: the library holds retrieval plans, never those.
6. One ``.beads/`` condition has one else-branch per file.
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


#: The wider set the else-branch and plan-conflation rules read: every file that reaches a user as written.
WIDE_GLOBS = (
    "agents/*.md",
    "resources/**/*.md",
    "skills/**/*.md",
    "commands/*.md",
)


def covered_files() -> list[pathlib.Path]:
    return sorted(p for g in GLOBS for p in CONEXUS.glob(g))


def wide_files() -> list[pathlib.Path]:
    return sorted({p for g in WIDE_GLOBS for p in CONEXUS.glob(g)})


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


# ---------------------------------------------------------------------------
# Phase 3: where an else-branch sends the work, and what the plan-library tools hold
# ---------------------------------------------------------------------------

#: Terms an else-branch may name that are defined somewhere a reader can find: ``term -> repo-relative
#: file whose heading line holds the term``. The test resolves each entry, so a term cannot be added
#: without a place that defines it. Empty today: every shipped else-branch names a tool, a path, the
#: hand-back or a skip.
DEFINED_TERMS: dict[str, str] = {}

#: The portability conditions: a line with one of these is a guard, and what follows its else marker is
#: the else-branch. A guard's else-branch must go somewhere; "otherwise" elsewhere in prose is not read.
CONDITION = re.compile(r"\.beads/|are not available in this session|plan document|there are no beads", re.I)
ELSE_BRANCH = re.compile(r"(?:^|[;:.,(]\s*|\s-\s)(?:elsewhere|otherwise)\b[,:]?\s+(?P<clause>(?:[^.;)]|\.(?=\S))+)", re.I)
DESTINATION = re.compile(
    r"`mcp__plugin_\w"  # a backticked MCP tool
    r"|`[\w.${}-]+(?:/[\w.${}-]*)+`"  # a backticked path
    r"|`[\w.-]+\.(?:md|yaml|yml|json|toml|py|txt)`"  # a backticked file
    r"|hand[- ]?back|hand it back|hand (?:it|them) back|\bskip\b|\bskipped\b",
    re.I,
)


def resolve_defined_terms(terms: dict[str, str], root: pathlib.Path = PROJECT_ROOT) -> list[str]:
    """The entries of ``terms`` whose file is missing or has no heading holding the term."""
    unresolved = []
    for term, rel in terms.items():
        path = root / rel
        headings = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("#")] if path.is_file() else []
        if not any(term.lower() in h.lower() for h in headings):
            unresolved.append(f"{term!r} -> {rel}")
    return unresolved


def else_branches(text: str) -> list[tuple[int, str]]:
    """``(line number, clause)`` for each else-branch on a line that carries a portability condition."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if not CONDITION.search(line):
            continue
        for match in ELSE_BRANCH.finditer(line):
            found.append((number, match.group("clause").strip()))
    return found


def aimless_else_branches(text: str, defined_terms: dict[str, str] | None = None) -> tuple[int, list[tuple[int, str]]]:
    """``(examined, [(line number, clause)])`` for the else-branches that name no destination."""
    terms = DEFINED_TERMS if defined_terms is None else defined_terms
    branches = else_branches(text)
    bad = [
        (n, clause)
        for n, clause in branches
        if not DESTINATION.search(clause) and not any(t.lower() in clause.lower() for t in terms)
    ]
    return len(branches), bad


#: A line that names the plan-library tools or the CLI group over them.
PLAN_LIBRARY = re.compile(r"(?<![A-Za-z0-9])(?:answer_)?plan_(?:save|search|delete)\b|\bnx (?:answer-)?plan\b(?!_)")
#: What the library does not hold.
NOT_THE_LIBRARY = re.compile(r"implementation|pipeline|phase[sd]?\b|epic|starting structure|dispatch|multiple agents", re.I)
NEGATION = re.compile(r"\b(?:not|never|no|without|instead of)\b|n't", re.I)


def plan_library_conflations(text: str) -> tuple[int, list[tuple[int, str]]]:
    """``(examined, [(line number, line)])``: plan-tool lines, and those that offer the library as implementation work."""
    examined = 0
    bad = []
    for number, line in enumerate(text.splitlines(), 1):
        if not PLAN_LIBRARY.search(line):
            continue
        examined += 1
        if NOT_THE_LIBRARY.search(line) and not NEGATION.search(line):
            bad.append((number, line.strip()[:160]))
    return examined, bad


def beads_else_branches(text: str) -> set[str]:
    """The distinct else-branches (normalised) of the ``.beads/`` condition in one file."""
    return {
        re.sub(r"\s+", " ", clause).strip(" .").lower()
        for line in text.splitlines()
        if ".beads/" in line
        for m in ELSE_BRANCH.finditer(line)
        for clause in [m.group("clause")]
    }


# -- the checkers, on fixtures: one per failure class ------------------------------------------------


def test_an_else_branch_that_sends_the_work_to_the_plan_document_is_flagged():
    text = "Bead steps here apply where the project has a `.beads/` directory; elsewhere put them in the plan document.\n"
    examined, bad = aimless_else_branches(text)
    assert examined == 1 and bad == [(1, "put them in the plan document")]


def test_an_else_branch_into_nothing_nameable_is_flagged_whatever_the_wording():
    for line in (
        "Use `.beads/` for tasks; otherwise keep a list somewhere.",
        "Where the project has a `.beads/` directory, create beads; elsewhere track tasks in the plan document",
        "The nexus tools are not available in this session; otherwise carry on as you see fit.",
    ):
        examined, bad = aimless_else_branches(line + "\n")
        assert examined == 1 and len(bad) == 1, line


def test_an_else_branch_with_a_destination_passes():
    for line in (
        "Applies where the project has a `.beads/` directory; elsewhere skip the `bd` steps.",
        "Where `.beads/` exists, file a bead; elsewhere put the list in your hand-back.",
        "Where `.beads/` exists, file a bead; elsewhere hand it back to the caller.",
        "Where `.beads/` exists, note it there; elsewhere write it with `mcp__plugin_conexus_nexus__memory_put`.",
        "Where `.beads/` exists, note it there; elsewhere write it to `docs/notes/plan.md`.",
    ):
        examined, bad = aimless_else_branches(line + "\n")
        assert examined == 1 and bad == [], line


def test_a_defined_term_is_a_destination_only_when_it_resolves_to_a_heading(tmp_path):
    (tmp_path / "defs.md").write_text("# Terms\n\n## The task log\n\nA file.\n", encoding="utf-8")
    line = "Where `.beads/` exists, file a bead; elsewhere keep it in the task log.\n"
    assert aimless_else_branches(line)[1], "undefined: flagged"
    assert aimless_else_branches(line, {"the task log": "defs.md"}) == (1, [])
    assert resolve_defined_terms({"the task log": "defs.md"}, tmp_path) == []
    assert resolve_defined_terms({"the task log": "missing.md", "no such heading": "defs.md"}, tmp_path) == [
        "'the task log' -> missing.md",
        "'no such heading' -> defs.md",
    ]


def test_an_else_branch_on_a_line_with_no_portability_condition_is_not_read():
    assert aimless_else_branches("Write the file; otherwise ask the user.\n") == (0, [])


def test_offering_the_plan_library_as_implementation_work_is_flagged():
    for line in (
        "Run `mcp__plugin_conexus_nexus__answer_plan_search` and reuse a match as a starting structure.",
        "Save the phase list with `answer_plan_save` for the next epic.",
        "`plan_search` returns the pipeline template for this task.",
        "Reuse a matching plan via `plan_search` before dispatching multiple agents.",
        "Search the library with `nx answer-plan list` for an implementation plan.",
    ):
        examined, bad = plan_library_conflations(line + "\n")
        assert examined == 1 and len(bad) == 1, line


def test_naming_the_plan_library_with_a_negation_or_without_a_trigger_word_passes():
    for line in (
        "`answer_plan_search` finds saved retrieval plans, never implementation plans.",
        "Implementation, pipeline, and phased-execution plans do not go in `answer_plan_save`.",
        "Do not use `plan_search` as a starting structure.",
        "`answer_plan_save` a reusable retrieval plan with a required `verb`.",
        "The `nx_plan_audit` tool checks an implementation plan.",  # a different tool: not read
    ):
        _, bad = plan_library_conflations(line + "\n")
        assert bad == [], line


def test_a_file_with_two_different_beads_else_branches_is_flagged_and_a_repeated_one_is_not():
    two = (
        "Bead steps apply where the project has a `.beads/` directory; elsewhere skip the `bd` steps.\n"
        "Bead steps apply where the project has a `.beads/` directory; elsewhere put them in your hand-back.\n"
    )
    same = (
        "Bead steps apply where the project has a `.beads/` directory; elsewhere skip the `bd` steps.\n"
        "Applies where the project has a `.beads/` directory; Elsewhere skip the `bd` steps.\n"
    )
    assert len(beads_else_branches(two)) == 2
    assert len(beads_else_branches(same)) == 1


# -- the real files ------------------------------------------------------------------------------------


def test_every_else_branch_of_a_portability_condition_names_a_destination():
    files = wide_files()
    assert len(files) >= 60, f"only {len(files)} files matched the wide globs"
    examined = 0
    problems: list[str] = []
    for path in files:
        n, bad = aimless_else_branches(path.read_text(encoding="utf-8"))
        examined += n
        problems += [f"{path.relative_to(PROJECT_ROOT)}:{ln}: else-branch names no destination: {clause}" for ln, clause in bad]
    assert examined >= 12, f"only {examined} else-branches were examined: the lint is blind"
    assert problems == [], "\n".join(problems)


def test_the_defined_terms_resolve():
    assert resolve_defined_terms(DEFINED_TERMS) == []


def test_the_plan_library_tools_are_never_offered_as_implementation_work():
    examined = 0
    problems: list[str] = []
    for path in wide_files():
        n, bad = plan_library_conflations(path.read_text(encoding="utf-8"))
        examined += n
        problems += [f"{path.relative_to(PROJECT_ROOT)}:{ln}: {text}" for ln, text in bad]
    assert examined >= 20, f"only {examined} plan-library lines were examined: the lint is blind"
    assert problems == [], "\n".join(problems)


def test_each_file_has_one_beads_else_branch():
    with_a_branch = 0
    problems: list[str] = []
    for path in wide_files():
        branches = beads_else_branches(path.read_text(encoding="utf-8"))
        with_a_branch += bool(branches)
        if len(branches) > 1:
            problems.append(f"{path.relative_to(PROJECT_ROOT)}: {sorted(branches)}")
    assert with_a_branch >= 10, f"only {with_a_branch} files carry a .beads/ else-branch: the lint is blind"
    assert problems == [], "\n".join(problems)
