# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jndz0: policy citations in workflows/e2e scripts must resolve.

Four CI workflows cited "CI Cost Discipline (CLAUDE.md)" as project policy
while AGENTS.md (the symlink target) had zero hits — a cited-but-absent
policy READS AS ENFORCED: reviewers treat the constraint as settled, and an
edit that violates it has nothing to violate. The directive now lives in
AGENTS.md § CI Cost Discipline; this lint keeps every such citation
resolving, so the next dangling citation surfaces without anyone filing a
new bead (the nexus-moht0 epic's exit criterion).

Two citation shapes are checked:

1. **Explicit section citations** — ``AGENTS.md § <heading>`` /
   ``CLAUDE.md § <heading>``: the quoted heading must match a real
   ``##``-level heading in AGENTS.md (case-insensitive prefix match, since
   prose often shortens "## CI Cost Discipline" to "§ CI Cost Discipline").
2. **Named-policy citations** — a line naming a policy from the table below
   alongside a CLAUDE.md/AGENTS.md reference requires that policy's heading
   to exist. Table-driven so new named policies get coverage by adding one
   row.

Non-vacuity: zero citations found across the scanned tree is a FAILURE —
scanning nothing is not finding nothing wrong (the expectations.sh rc=3
doctrine).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent
AGENTS_MD = REPO_ROOT / "AGENTS.md"

_SCAN_DIRS = (".github", "tests/e2e")
_SCAN_GLOBS = ("*.yml", "*.yaml", "*.sh")

#: Explicit `<file>.md § <heading>` citation. The heading capture stops at
#: punctuation that ends the citation in prose.
#: Path-anchored (wave-3.6 review): a path-prefixed citation like
#: ``tests/AGENTS.md § X`` names a DIFFERENT file's headings and must not be
#: checked against root AGENTS.md — the lookbehind skips it (out of scope
#: rather than mis-checked). KNOWN LIMITATION (disclosed, same review): a
#: citation whose ``§ heading`` text wraps across comment lines is invisible
#: to this line-oriented scan — e.g. warm-reindex-skip-gate.sh's wrapped
#: "Engine-service\nrelease" cite gets zero protection; a future multi-line
#: joiner is the fix if that class ever rots in practice.
_SECTION_CITE_RE = re.compile(
    r"(?<![\w/])(?:AGENTS|CLAUDE)\.md\s*§\s*([A-Za-z][A-Za-z0-9 \-]{2,60})"
)

#: Named policies: (line-match regex, required AGENTS.md heading).
_NAMED_POLICIES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"cost discipline", re.IGNORECASE), "## CI Cost Discipline"),
)


def _headings() -> list[str]:
    return [
        line.strip()
        for line in AGENTS_MD.read_text().splitlines()
        if line.startswith("##")
    ]


def _scan() -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """Return (section_cites, named_cites) as (relpath, lineno, payload)."""
    section: list[tuple[str, int, str]] = []
    named: list[tuple[str, int, str]] = []
    for d in _SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.exists():
            continue
        for glob in _SCAN_GLOBS:
            for path in sorted(base.rglob(glob)):
                rel = str(path.relative_to(REPO_ROOT))
                for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                    for m in _SECTION_CITE_RE.finditer(line):
                        section.append((rel, i, m.group(1).strip()))
                    if re.search(r"(?:AGENTS|CLAUDE)\.md", line):
                        for rx, heading in _NAMED_POLICIES:
                            if rx.search(line):
                                named.append((rel, i, heading))
    return section, named


def heading_resolves(cited: str, headings: list[str]) -> bool:
    """A citation resolves when some ##-heading's text starts with (or
    equals) the cited text, case-insensitive — prose shortens headings."""
    cited_l = cited.lower().rstrip(" .")
    return any(
        h.lstrip("#").strip().lower().startswith(cited_l)
        or cited_l.startswith(h.lstrip("#").strip().lower())
        for h in headings
    )


def test_every_policy_citation_resolves() -> None:
    headings = _headings()
    section, named = _scan()
    total = len(section) + len(named)
    assert total > 0, (
        "the citation sweep found ZERO AGENTS.md/CLAUDE.md policy citations "
        "under .github/ and tests/e2e/ — either every citation was removed "
        "or this scanner broke; scanning nothing is not finding nothing "
        "wrong (nexus-jndz0 non-vacuity)"
    )
    failures: list[str] = []
    for rel, lineno, cited in section:
        if not heading_resolves(cited, headings):
            failures.append(f"{rel}:{lineno}: '§ {cited}' matches no AGENTS.md heading")
    for rel, lineno, heading in named:
        if not any(h.startswith(heading) for h in headings):
            failures.append(
                f"{rel}:{lineno}: cites the {heading!r} policy but AGENTS.md "
                "has no such heading — the citation dangles (cited-but-absent "
                "policy reads as enforced)"
            )
    assert failures == [], (
        "dangling policy citation(s) (nexus-jndz0):\n  " + "\n  ".join(failures)
    )


def test_detector_reds_on_a_planted_bogus_citation() -> None:
    """The mandatory measure-the-detector arm: a bogus `§ Nonexistent
    Heading` citation must NOT resolve against the real AGENTS.md."""
    headings = _headings()
    assert not heading_resolves("Nonexistent Heading Xyzzy", headings)
    m = _SECTION_CITE_RE.search("#   per AGENTS.md § Nonexistent Heading Xyzzy, we skip")
    assert m is not None and m.group(1).strip().startswith("Nonexistent Heading")


def test_named_policy_line_detection() -> None:
    """A workflow comment naming the cost policy next to a CLAUDE.md ref is
    picked up; the same words without the file reference are not."""
    rx, heading = _NAMED_POLICIES[0]
    hit = "        # Deliberately no matrix (CI cost discipline, CLAUDE.md)"
    miss = "        # keep costs low"
    assert rx.search(hit) and re.search(r"CLAUDE\.md", hit)
    assert not (rx.search(miss) and re.search(r"(?:AGENTS|CLAUDE)\.md", miss))
