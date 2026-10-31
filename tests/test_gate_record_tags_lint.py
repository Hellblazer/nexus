# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gate-record writers carry one tag sentence, and the reviewers bound their memory.

RDR-209 Amendment A1 rolls gate records up at phase close by their
``kind:gate``, ``bead:`` and ``rdr:`` tags. The instruction lives as a copied
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
TAGS = 'tags="kind:gate,bead:<id>,rdr:<NNN>"'
DROP_CLAUSE = "drop `rdr:` when the bead serves no RDR"
SENTENCE = re.compile(r"A gate record \(a review, critique, confirmation or fix round\) also passes [^\n]*?\(RDR-209 Amendment A1\)\.")
MEMORY_PARAGRAPH = "never replaces the T2 gate record, which is mandatory"


def _frontmatter(text: str) -> str:
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return match.group(1) if match else ""


def test_the_tag_sentence_is_identical_in_every_writer() -> None:
    found = {}
    for name in WRITERS:
        hits = SENTENCE.findall((AGENTS_DIR / name).read_text(encoding="utf-8"))
        assert len(hits) == 1, f"{name}: expected the gate-record tag sentence once, found {len(hits)}"
        found[name] = hits[0]
    assert len(set(found.values())) == 1, f"the tag sentence differs between writers: {found}"
    assert TAGS in next(iter(found.values()))


@pytest.mark.parametrize("path", RESTATING, ids=lambda p: p.name)
def test_restating_files_carry_the_tags_and_the_drop_clause(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert TAGS in text, f"{path.relative_to(ROOT)} does not name the gate tags"
    assert DROP_CLAUSE in text, f"{path.relative_to(ROOT)} lacks the rdr: drop clause"


@pytest.mark.parametrize("name", REVIEWERS)
def test_a_reviewer_with_user_memory_carries_the_bounding_paragraph(name: str) -> None:
    text = (AGENTS_DIR / name).read_text(encoding="utf-8")
    assert re.search(r"^memory: user$", _frontmatter(text), re.M), f"{name}: no memory: user"
    assert MEMORY_PARAGRAPH in text, f"{name}: memory: user without the paragraph bounding it"
