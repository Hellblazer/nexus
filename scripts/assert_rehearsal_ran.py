# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assert the schema-upgrade rehearsal actually ran (not vacuously skipped).

nexus-c3rer (critique of nexus-4m6i0.6): ``SchemaUpgradeRehearsalIntegrationTest``
skips loudly via JUnit ``Assumptions.abort`` when the old engine tag is
unavailable — correct behavior locally, but a JUnit skip does not fail the
Maven build, and Surefire's skip count is invisible behind a green Actions
checkmark. If ``fetch-tags: true`` ever regressed in ``service-ci.yml`` (a
workflow edit, checkout-action semantics change, tag deletion), the rehearsal
would silently and permanently stop running while CI stayed green — the exact
gates-scripted-not-ambient failure mode (CLAUDE.md: "a gate that skip-passes
when its dependency is absent must carry a max-skip / non-vacuity assert").
Mirrors ``scripts/assert_ca3_ran.py``, the established precedent.

Usage: ``python scripts/assert_rehearsal_ran.py <surefire TEST-*.xml>``

Exits non-zero when the report is missing (the test class never executed at
all — e.g. renamed, or excluded by a pattern change), records zero testcases,
or records any skip. Unlike CA-3 there are no legitimate platform-conditional
skips here: in CI the tag is guaranteed by fetch-tags, so ANY skip is vacuous.
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET


def main(junit_path: str) -> None:
    if not os.path.exists(junit_path):
        raise SystemExit(
            f"rehearsal Surefire report not found at '{junit_path}' — the "
            "SchemaUpgradeRehearsalIntegrationTest class never executed "
            "(renamed? excluded? mvn crashed before the test phase?). The "
            "old-to-HEAD upgrade rehearsal (nexus-4m6i0.6) is NOT covered by "
            "this run."
        )
    root = ET.parse(junit_path).getroot()
    cases = list(root.iter("testcase"))
    if not cases:
        raise SystemExit(
            f"rehearsal Surefire report '{junit_path}' contains zero "
            "testcases — vacuous run, failing loud (nexus-c3rer)."
        )
    skipped = [c for c in cases if c.find("skipped") is not None]
    if skipped:
        reasons = "; ".join(
            (c.find("skipped").get("message") or "<no message>")[:200]
            for c in skipped
        )
        raise SystemExit(
            f"rehearsal test skipped ({len(skipped)}/{len(cases)} cases) — "
            "in CI the old tag is guaranteed by fetch-tags, so a skip means "
            "the tag-availability contract regressed and the upgrade "
            f"rehearsal silently stopped running. Reasons: {reasons}"
        )
    print(
        f"rehearsal non-vacuity OK: {len(cases)} testcase(s) ran, 0 skipped "
        f"({junit_path})"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: assert_rehearsal_ran.py <surefire TEST-*.xml>")
    main(sys.argv[1])
