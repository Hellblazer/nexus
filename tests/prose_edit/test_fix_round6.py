# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fix round 6 for the RDR-221 Phase 1 exit (nexus-ger02.7): the dispatch prompt is printed, never composed.

In 2 of 10 headless sessions the orchestrating model dispatched the line-editor with the literal prompt
`Read WORK/brief.md` instead of the work directory's path. `build` now prints the exact prompt, with the absolute
path already in it, and the skill tells the model to pass that line as printed.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.prose_edit.conftest import Prose
from tests.prose_edit.test_brief import AGENT, SKILL, brief_ok
from tests.prose_edit.test_fix_round1 import _step

SKILL_TEXT = SKILL.read_text(encoding="utf-8")
AGENT_TEXT = AGENT.read_text(encoding="utf-8")
DISPATCH = re.compile(r"^DISPATCH=(?P<prompt>.+)$", re.MULTILINE)
PROMPT = re.compile(r'Read (?P<path>/\S+/brief\.md) in full with Read and follow it\. '
                    r'Its last line is "Brief id: <id>": put that id in your reply as brief_sha\.')


def _header(out: str) -> tuple[list[str], str]:
    head, brief = out.split("\n\n", 1)
    return head.splitlines(), brief


def test_a_path_run_build_prints_the_dispatch_prompt_with_the_absolute_brief_path(prose: Prose) -> None:
    out = brief_ok(prose, "build", "docs/x.md", "--work")
    lines, brief = _header(out)
    work = Path(lines[0].removeprefix("WORK="))
    try:
        assert lines[0].startswith("WORK=") and len(lines) == 2 and lines[1].startswith("DISPATCH="), lines
        assert brief.startswith("# Editing brief")
        m = PROMPT.fullmatch(DISPATCH.search(out).group("prompt"))  # type: ignore[union-attr]
        assert m, lines[1]
        path = Path(m.group("path"))
        assert path.is_absolute() and path == work / "brief.md" and path.is_file()
        assert "WORK" not in lines[1] and not re.search(r"Brief id: [0-9a-f]{12}", out)
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_a_stdin_run_build_inside_a_work_directory_prints_the_dispatch_prompt_too(prose: Prose) -> None:
    work = Path(brief_ok(prose, "tmpdir").strip())
    try:
        (work / "input.txt").write_text("fix: a thing\n", encoding="utf-8")
        out = brief_ok(prose, "build", "-", "--genre", "commit-message", "--file", str(work / "input.txt"))
        lines, brief = _header(out)
        assert len(lines) == 1 and lines[0].startswith("DISPATCH="), lines
        m = PROMPT.fullmatch(lines[0].removeprefix("DISPATCH="))
        assert m and Path(m.group("path")) == work / "brief.md" and Path(m.group("path")).is_file()
        assert brief.startswith("# Editing brief") and "fix: a thing" in brief
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_a_stdin_build_outside_a_work_directory_writes_no_brief_and_prints_no_prompt(
    prose: Prose, tmp_path: Path
) -> None:
    loose = tmp_path / "m.txt"
    loose.write_text("fix: a thing\n", encoding="utf-8")
    out = brief_ok(prose, "build", "-", "--genre", "commit-message", "--file", str(loose))
    assert out.startswith("# Editing brief") and "DISPATCH=" not in out


def test_skill_step_7_passes_the_printed_prompt_and_composes_no_path() -> None:
    step = _step(SKILL_TEXT, 7)
    assert "`DISPATCH=`" in step and "exactly as printed" in step
    assert "never type a path" in step.lower()
    assert "WORK/brief.md" not in step and "with WORK as its full path" not in step
    assert "Read WORK" not in SKILL_TEXT and "WORK/brief.md" not in SKILL_TEXT.split("\n7. ", 1)[1].split("\n8. ", 1)[0]
    # step 4 says where the line is printed, for both runs
    step4 = _step(SKILL_TEXT, 4)
    assert "DISPATCH=" in step4


def test_skill_step_7_stops_and_shows_the_author_the_error_when_the_editor_cannot_read_the_brief() -> None:
    step = _step(SKILL_TEXT, 7)
    sentence = next(s for s in re.split(r"(?<=\.)\s+", step) if "cannot read the brief" in s)
    assert "stop" in sentence and "error" in sentence and "author" in sentence
    assert "create a directory" in sentence and "link" in sentence and "list the work directory" in sentence


def test_the_line_editor_reads_the_path_in_its_prompt_and_stops_when_it_cannot() -> None:
    procedure = AGENT_TEXT[AGENT_TEXT.index("## Procedure"):AGENT_TEXT.index("## Voice card")]
    first = procedure.split("\n2. ", 1)[0]
    assert "absolute path" in first and "WORK/brief.md" not in first
    assert "cannot read" in first and "stop" in first.lower()
