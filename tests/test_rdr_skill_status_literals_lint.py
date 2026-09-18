# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``status:`` literal an RDR lifecycle skill writes is a value of
the packaged lifecycle table's ``status`` domain, lower-cased.

The skills are the T2 status writers the census reads back.
``conexus/skills/rdr-close/SKILL.md`` wrote ``status: Implemented`` (outside
the six-value domain, armed but never fired in the live census) and
``rdr-create`` wrote ``status: Draft`` (in domain, wrong case: the live
census counted ``Draft=2`` beside ``draft=9``). Intrastate review [26115],
plan N2 cause (c), bead nexus-nc08w.5.

File-only: reads the skill files and the packaged table, touches no store,
so it runs in the ``-m lint`` job. A planted ``status: Implemented`` fixture
proves the check fires (nexus-moht0: a check that cannot fail on bad input
proves nothing), and the live check prints the files scanned and literals
examined so a vacuous pass is visible.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.tables.load import load_packaged_table

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "conexus" / "skills"

#: A ``status:`` key followed by a bare or quoted word. A placeholder
#: (``status: [status]``) or a code span (``the `status:` line``) has no
#: word directly after the colon and is not a literal this lint judges.
#: The key may follow a literal ``\n`` escape inside a quoted memory_put
#: payload (rdr-create writes one); a word-boundary rule alone would see
#: the ``n`` and skip it, so that escape is an explicit second boundary.
_STATUS_LITERAL = re.compile(r"(?:(?<![A-Za-z_])|(?<=\\n))status:\s*\"?([A-Za-z][A-Za-z_-]*)\"?")


def _domain() -> frozenset[str]:
    return frozenset(load_packaged_table("rdr-lifecycle.toml").dimensions["status"].domain)


def status_literal_violations(files: list[Path], domain: frozenset[str]) -> tuple[list[str], int]:
    """``(violations, literals_examined)`` over *files*. A violation names
    ``file:line: status: <value>``; a value is a violation when it is not
    exactly (case included) a member of *domain*."""
    violations: list[str] = []
    examined = 0
    for path in files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in _STATUS_LITERAL.finditer(line):
                examined += 1
                if m.group(1) not in domain:
                    rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
                    violations.append(f"{rel}:{lineno}: status: {m.group(1)}")
    return violations, examined


def _rdr_skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.glob("rdr-*/SKILL.md"))


def test_rdr_skills_write_in_domain_lowercase_statuses() -> None:
    files = _rdr_skill_files()
    assert len(files) >= 5, f"expected the rdr-* skills under {SKILLS_DIR}, found {files}"
    violations, examined = status_literal_violations(files, _domain())
    print(f"rdr skill status-literal lint: {len(files)} files scanned, {examined} literals examined, {len(violations)} violations")  # noqa: T201 — the examined counts are the non-vacuity evidence a reader of the lint log needs
    assert examined >= 3, "the scan examined almost nothing; the literal pattern or the skill files moved"
    assert not violations, "\n".join(violations)


def test_planted_out_of_domain_literal_is_caught(tmp_path: Path) -> None:
    planted = tmp_path / "SKILL.md"
    planted.write_text(
        "1. memory_put(content=\"status: Implemented, closed: X\")\n"
        "2. set `status: \"accepted\"`\n"
        "3. memory_put(content=\"title: T\\nstatus: Draft\\ntype: F\")\n"
        "4. the `status:` line and status: [status]\n",
        encoding="utf-8",
    )
    violations, examined = status_literal_violations([planted], _domain())
    assert examined == 3
    assert [v.split(": ", 1)[1] for v in violations] == ["status: Implemented", "status: Draft"]
