# SPDX-License-Identifier: AGPL-3.0-or-later
"""A test that reads the plugin's pending-release ledger also reads the plugin changelog.

The release empties ``conexus/PENDING_RELEASE.md`` and moves its lines into
``conexus/CHANGELOG.md``, so a test that pins a line of the ledger alone goes red on every
release tree. It happened on 8.5.2 and again on 8.5.3
(test_worktree_skills_identity_work, fixed on release/v8.5.3 in e8f735bed). A test that
names the ledger as a path literal must also name the changelog, so it can find the line
in either. The ledger's own tooling reads the file for what it is, not for a line that
ships, and is listed in ``ALLOWED`` with its reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
LEDGER = re.compile(r"""["'](?:conexus/)?PENDING_RELEASE\.md["']""")
CHANGELOG = re.compile(r"""["'](?:conexus/)?CHANGELOG\.md["']""")
ALLOWED = {
    # this file names both in its own patterns
    "test_pending_release_reads_lint.py",
    # the ledger's drift gate: it compares committed trees against the pinned tag and must read the ledger itself
    "test_plugin_release_drift_ledger.py",
    # keyed on the ledger still carrying its entries, and skips once a release has emptied it
    "test_plan_audit_guidance_lint.py",
}


@pytest.mark.lint
def test_a_test_that_reads_the_ledger_also_reads_the_changelog() -> None:
    hits = []
    examined = 0
    for path in sorted(TESTS.rglob("*.py")):
        if path.name in ALLOWED:
            continue
        examined += 1
        text = path.read_text(encoding="utf-8")
        if LEDGER.search(text) and not CHANGELOG.search(text):
            hits.append(str(path.relative_to(TESTS.parent)))
    assert examined > 100, f"vacuous scan: only {examined} test files examined"
    assert not hits, (
        "these tests read conexus/PENDING_RELEASE.md, which the release empties, without also reading "
        "conexus/CHANGELOG.md, where its lines go: " + ", ".join(hits))
