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
#: The procedure, by its full MCP name and table name; the call form is nexus-qo7ie.31's.
PROCEDURE = re.compile(r'mcp__plugin_conexus_nexus__procedure_run\(name="gate_record"')
UNCHANGED = "passing the returned `project`, `title`, `tags` and `content` unchanged"
WRITE = "written with `mcp__plugin_conexus_nexus__memory_put`"
ROUND = '"round": <integer>'

NO_COMPOSE = "never compose the tag string yourself"
DROP_CLAUSE = "leave out `rdr` when the bead serves no RDR"
#: A writer composing its own gate tags, the form gate_record replaced. Not a list of
#: spellings: a line that names kind:gate (any spacing around the colon) composes the tags
#: when it also assigns tags (``tags=``, ``tags:``, ``--tag``) or gives a sibling tag a
#: value (``bead:<id>``, ``rdr:240``). Prose naming the tags (``the `kind:gate`, `bead:` and
#: `rdr:` tags``) gives no value and assigns nothing. The RDR gate flow's own records in
#: <repo>_rdr (tags="rdr,gate,...") name no kind:gate and are outside gate_record (TD6).
KIND_GATE = re.compile(r"\bkind\s*:\s*gate\b", re.IGNORECASE)
ASSIGNS_TAGS = re.compile(r"\btags\s*[=:]|--tags?\b", re.IGNORECASE)
VALUED_SIBLING = re.compile(r"\b(?:bead|rdr)\s*:\s*[^\s`'\",)\]]", re.IGNORECASE)
#: Every plugin file that can instruct a writer, less the two release ledgers.
SCANNED = sorted(
    p for d in ("agents", "skills", "commands") for p in (ROOT / "conexus" / d).rglob("*.md")
)
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
    assert PROCEDURE.search(sentence), "the writers' sentence does not name procedure_run(name=\"gate_record\" ...)"
    for clause in (UNCHANGED, NO_COMPOSE, DROP_CLAUSE, WRITE, ROUND):
        assert clause in sentence, f"the writers' sentence lacks {clause!r}"


def composed_tag_lines(text: str) -> list[str]:
    """Lines of an agent or skill file that compose a gate tag string themselves."""
    return [
        line for line in text.splitlines()
        if KIND_GATE.search(line) and (ASSIGNS_TAGS.search(line) or VALUED_SIBLING.search(line))
    ]


def test_the_scan_covers_the_plugins_instruction_files() -> None:
    names = {p.relative_to(ROOT / "conexus").as_posix() for p in SCANNED}
    assert {f"agents/{n}" for n in WRITERS} <= names
    assert {p.relative_to(ROOT / "conexus").as_posix() for p in RESTATING} <= names
    assert len(names) > 50, "the scan examined almost nothing"


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: p.relative_to(ROOT / "conexus").as_posix())
def test_no_plugin_file_composes_its_own_gate_tags(path: Path) -> None:
    lines = composed_tag_lines(path.read_text(encoding="utf-8"))
    assert not lines, f"{path.relative_to(ROOT)} composes gate tags instead of using gate_record: {lines}"


@pytest.mark.parametrize("line", [
    'also passes `tags="kind:gate,bead:<id>,rdr:<NNN>"` (drop `rdr:` when the bead serves no RDR).',
    "memory_put(..., tags='kind:gate,bead:nexus-1')",
    'memory_put(..., tags = "kind:gate,bead:nexus-1")',
    "memory_put(..., tags=`kind:gate,bead:nexus-1`)",
    'memory_put(..., tags="bead:x,kind:gate")',
    "tags: kind:gate,bead:<id>",
    'tags=\\"kind:gate,bead:x\\"',
    "with the tags kind:gate, bead:<id>, rdr:<NNN>",
    'memory_put(..., tags="rdr:240, kind:gate")',
    'memory_put(..., tags="bead:x, rdr:240, kind:gate")',
    'memory_put(..., tags=["kind:gate", "bead:x"])',
    'tags: ["kind:gate", "bead:x"]',
    'memory_put(..., tags=("kind:gate", "bead:x"))',
    "--tag kind:gate --tag bead:x",
    "kind: gate, bead: x",
    "bead:x kind:gate",
    "KIND:GATE,BEAD:X",
])
def test_the_composed_tag_check_sees_every_spelling(line: str) -> None:
    assert composed_tag_lines(line) == [line]


@pytest.mark.parametrize("line", [
    "the rollup reads its `kind:gate`, `bead:` and `rdr:` tags",
    "the phase-close rollup finds the record by its `kind:gate`, `bead:` and `rdr:` tags (RDR-240 TD6)",
    'tags="critique,review"',
    'tags="rdr,gate,fix-check"',
    'memory_put(project="{repo}_rdr", title="{id}-gate-latest", tags="rdr,gate")',
    "a gatekeeper checks it",
    "bead:x,kind:gateway",
    "the rollup reads `kind:gate`, then `bead:` and `rdr:`",
])
def test_the_composed_tag_check_leaves_prose_and_other_tags_alone(line: str) -> None:
    assert composed_tag_lines(line) == []


@pytest.mark.parametrize("path", RESTATING, ids=lambda p: p.name)
def test_restating_files_name_the_procedure_and_the_unchanged_write(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert PROCEDURE.search(text), f"{path.relative_to(ROOT)} does not name procedure_run(name=\"gate_record\" ...)"
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
