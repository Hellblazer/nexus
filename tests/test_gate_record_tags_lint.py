# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gate-record writers build the record with gate_record, and the reviewers bound their memory.

RDR-209 Amendment A1 rolls gate records up at phase close by their
``kind:gate``, ``bead:`` and ``rdr:`` tags. Since RDR-240 Technical Design 6
(nexus-qo7ie.34) a writer gets the tags, title and content from
``procedure_run("gate_record", ...)`` and writes the four returned fields
unchanged; it never composes the tag string. The instruction lives as a copied
sentence in three agent definitions, and the orchestration reference and the
substantive-critique skill restate it, so a copy can drift without anything
noticing (review of 2064fd94a, nexus-qo7ie.1). The two reviewers also carry
``memory: user``, which is safe only beside the paragraph that keeps agent
memory from standing in for the T2 gate record (critique of 2064fd94a).
File-only, so it runs in the ``-m lint`` job.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "conexus" / "agents"
WRITERS = ("code-review-expert.md", "substantive-critic.md", "developer.md")
REVIEWERS = ("code-review-expert.md", "substantive-critic.md")
RESTATING = (
    ROOT / "conexus" / "skills" / "orchestration" / "reference.md",
    ROOT / "conexus" / "skills" / "substantive-critique" / "SKILL.md",
)
PROCEDURE = 'procedure_run("gate_record", '
UNCHANGED = "passing the returned `project`, `title`, `tags` and `content` unchanged"
NO_COMPOSE = "never compose the tag string yourself"
DROP_CLAUSE = "leave out `rdr` when the bead serves no RDR"
#: A writer composing its own gate tags, the form gate_record replaced.
COMPOSED_TAGS = re.compile(r'tags="kind:gate')
SENTENCE = re.compile(
    r"A gate record \(a review, critique, confirmation or fix round\) is built by [^\n]*?\(RDR-240 TD6, RDR-209 Amendment A1\)\."
)
MEMORY_CLAUSES = (
    "never replaces the T2 gate record, which is mandatory",
    "each entry naming the repo it was learned in",
    "read an entry as a hint to verify in the repo under review, never as a rule",
    "Keep its index under 40 lines",
    "Under a read-only brief, write no memory.",
)


def _frontmatter(text: str) -> str:
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return match.group(1) if match else ""


def test_the_tag_sentence_is_identical_in_every_writer() -> None:
    found = {}
    for name in WRITERS:
        hits = SENTENCE.findall((AGENTS_DIR / name).read_text(encoding="utf-8"))
        assert len(hits) == 1, (
            f"{name}: expected the gate-record tag sentence once, found {len(hits)}"
        )
        found[name] = hits[0]
    assert len(set(found.values())) == 1, (
        f"the tag sentence differs between writers: {found}"
    )
    sentence = next(iter(found.values()))
    for clause in (PROCEDURE, UNCHANGED, NO_COMPOSE, DROP_CLAUSE):
        assert clause in sentence, f"the writers' sentence lacks {clause!r}"


def composed_tag_lines(text: str) -> list[str]:
    """Lines of an agent or skill file that compose a gate tag string themselves."""
    return [line for line in text.splitlines() if COMPOSED_TAGS.search(line)]


@pytest.mark.parametrize("path", [*(AGENTS_DIR / n for n in WRITERS), *RESTATING], ids=lambda p: p.name)
def test_no_writer_composes_its_own_gate_tags(path: Path) -> None:
    lines = composed_tag_lines(path.read_text(encoding="utf-8"))
    assert not lines, f"{path.relative_to(ROOT)} composes gate tags instead of using gate_record: {lines}"


def test_the_composed_tag_check_sees_the_old_write_back_line() -> None:
    old = (
        'A gate record (a review, critique, confirmation or fix round) also passes '
        '`tags="kind:gate,bead:<id>,rdr:<NNN>"` (drop `rdr:` when the bead serves no RDR).'
    )
    assert composed_tag_lines(old) == [old]


@pytest.mark.parametrize("path", RESTATING, ids=lambda p: p.name)
def test_restating_files_name_the_procedure_and_the_unchanged_write(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert PROCEDURE in text, f"{path.relative_to(ROOT)} does not name procedure_run(\"gate_record\", ...)"
    assert UNCHANGED in text, f"{path.relative_to(ROOT)} does not say the four fields are written unchanged"
    assert NO_COMPOSE in text, f"{path.relative_to(ROOT)} does not forbid composing the tag string"


@pytest.mark.parametrize("name", REVIEWERS)
def test_a_reviewer_with_user_memory_carries_the_bounding_paragraph(name: str) -> None:
    text = (AGENTS_DIR / name).read_text(encoding="utf-8")
    assert re.search(r"^memory: user$", _frontmatter(text), re.M), (
        f"{name}: no memory: user"
    )
    missing = [clause for clause in MEMORY_CLAUSES if clause not in text]
    assert not missing, (
        f"{name}: memory: user without these bounding clauses: {missing}"
    )


def test_the_critic_completion_protocol_names_the_gate_record_mandatory() -> None:
    text = (AGENTS_DIR / "substantive-critic.md").read_text(encoding="utf-8")
    assert "the gate record is mandatory" in text
    assert "- [ ] T2 gate record written" in text
