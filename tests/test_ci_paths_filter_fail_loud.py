# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hak6p: shape pins for nexus-cqquo's paths-filter fail-loud guards.

nexus-cqquo made five workflow sites refuse to treat "paths-filter said
nothing usable" as an affirmatively-false predicate: each reads the filter
output through a ``case`` statement whose ``*)`` arm exits 1, so an
empty/absent/malformed output FAILS the step instead of silently skipping
a required build. The fix shipped as inline bash embedded in workflow YAML
with zero regression coverage — a future edit dropping the ``*)`` arm
would reopen the exact "reports green without checking" bug with nothing
to catch it (the asymmetry nexus-hak6p was filed for: the sibling fixes
f9z84/jvhsw both carry tests).

Shape pins (the ``test_release_workflow_ci_evidence.py`` precedent): the
workflows are not executable in CI-of-CI, so assertions on the parsed YAML
hold the load-bearing properties — each guard step still exists, its
``case`` accepts only ``true``/``false``, and its wildcard arm still
exits 1 loudly.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"

#: (workflow file, step-name substring, expected number of occurrences).
#: Counts are exact: a guard silently deleted from ONE of the two CA-3
#: jobs must fail here, not average out against the survivor.
GUARDS = [
    ("ci.yml", "Decide whether the CA-3 build runs", 2),
    ("ci.yml", "Verify the svc predicate is usable", 1),
    ("ci.yml", "Verify the seam predicate is usable", 1),
    ("service-ci.yml", "Report the decision (fail loud, never default-skip)", 1),
]


def _guard_steps(workflow: str, name_fragment: str) -> list[dict]:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text())
    return [
        step
        for job in doc.get("jobs", {}).values()
        for step in job.get("steps", [])
        if name_fragment in str(step.get("name", ""))
    ]


@pytest.mark.parametrize(("workflow", "fragment", "count"), GUARDS)
def test_guard_step_exists_at_expected_count(
    workflow: str, fragment: str, count: int
) -> None:
    steps = _guard_steps(workflow, fragment)
    assert len(steps) == count, (
        f"{workflow}: expected {count} step(s) named like {fragment!r}, "
        f"found {len(steps)} — a nexus-cqquo fail-loud guard was removed "
        "or renamed without updating this pin"
    )


@pytest.mark.parametrize(("workflow", "fragment", "count"), GUARDS)
def test_guard_case_statement_fails_loud_on_unusable_output(
    workflow: str, fragment: str, count: int
) -> None:
    """Each guard's ``run`` block must (a) read the predicate through a
    ``case`` statement, (b) recognize only ``true``/``false`` as usable,
    and (c) ``exit 1`` in the ``*)`` wildcard arm — the arm whose silent
    removal reopens the fail-open-to-skip bug nexus-cqquo closed."""
    for step in _guard_steps(workflow, fragment):
        run = step.get("run", "")
        assert "set -euo pipefail" in run, (
            f"{workflow} {fragment!r}: guard shell must be strict-mode"
        )
        assert re.search(r'case\s+"\$', run), (
            f"{workflow} {fragment!r}: predicate no longer read through a "
            "case statement"
        )
        # Both accepted-value spellings used across the five sites:
        # `true|false) ;;` (ci.yml) and separate `true)` / `false)` arms
        # (service-ci.yml).
        assert re.search(r"true\|false\)", run) or (
            re.search(r"^\s*true\)", run, re.MULTILINE)
            and re.search(r"^\s*false\)", run, re.MULTILINE)
        ), f"{workflow} {fragment!r}: case no longer limits usable values to true/false"
        wildcard = re.search(r"\*\)(.*?)(?:;;|esac)", run, re.DOTALL)
        assert wildcard, (
            f"{workflow} {fragment!r}: the `*)` wildcard arm is GONE — an "
            "unusable paths-filter output now falls through silently"
        )
        assert "exit 1" in wildcard.group(1), (
            f"{workflow} {fragment!r}: the `*)` arm no longer exits 1 — "
            "malformed output degrades to a silent skip again"
        )
        assert "::error::" in wildcard.group(1), (
            f"{workflow} {fragment!r}: the `*)` arm should surface a "
            "::error:: annotation naming the unusable output"
        )
