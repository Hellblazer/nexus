# SPDX-License-Identifier: AGPL-3.0-or-later
"""No test reads a named changelog fragment by path.

The release folds every ``changelog/unreleased/*.md`` fragment into CHANGELOG.md and
deletes it, so a test that opens one by name fails on every release tree. It happened
on 8.1.0 (test_memory_curate_cold) and 8.1.2 (test_nx_backup). Use
``tests._changelog_entry.changelog_entry``, which reads the fragment or the folded entry.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
# A path to a real bead fragment: "unreleased" then a nexus-<id> file name, as a
# literal path string or a / join. Synthetic ids (nexus-x, nexus-abc) build temp fixtures.
PATTERN = re.compile(r"""unreleased["']?\s*[/,]\s*["']?nexus-(?!x["'.]|abc["'.])[a-z0-9]{4,}""")
ALLOWED = {"_changelog_entry.py", "test_changelog_fragment_reads_lint.py", "test_memory_curate_cold.py"}


@pytest.mark.lint
def test_no_test_opens_a_named_changelog_fragment() -> None:
    hits = []
    examined = 0
    for path in sorted(TESTS.rglob("*.py")):
        if path.name in ALLOWED:
            continue
        examined += 1
        text = path.read_text(encoding="utf-8")
        for m in PATTERN.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            hits.append(f"{path.relative_to(TESTS.parent)}:{line}")
    assert examined > 100, f"vacuous scan: only {examined} test files examined"
    assert not hits, "tests open a changelog fragment by name; use changelog_entry(): " + ", ".join(hits)
