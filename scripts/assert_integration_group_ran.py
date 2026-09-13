#!/usr/bin/env python3
"""Fail when a Java @Tag("integration") class did not actually execute tests.

nexus-yuogq. ``service/pom.xml`` sets ``test.excluded.groups=integration``, so
that group is excluded from the ordinary ``mvn test``. Until 2026-09-12 it ran
in no default suite, no release battery and no schedule, and three fixtures
rotted there undetected: two carried the RDR-204 unregistered-collection defect
and were found only because a bead named them by hand, and a third still errored
at SETUP afterwards with 12 tests dark. They are the SQL-function-versus-app-
stitch parity tests -- the cross-check between the combined-query functions and
the repository path, which is precisely the surface an engine release changes.

THE FAILURE THIS GUARDS IS A GREEN RUN THAT EXECUTED NOTHING, not a red test.
A class erroring at setup reports zero tests; a selector that stops matching (a
dropped tag, a renamed group, a filter change) exits 0 having run nothing. Both
read as success.

It deliberately does NOT assert a total count. The integration run writes into
the same surefire directory as the ordinary suite, so a total would be
satisfied by the ~2950 tests the ordinary suite already wrote and would pass
while the integration group ran zero -- vacuous in exactly the way this bead is
about. Instead it derives the expected class list from the SOURCE (every class
carrying the tag) and requires each one to have a report with tests > 0. That
is self-maintaining: adding or removing a tagged class needs no edit here.

SCOPE, stated so the guard is not trusted past it. It catches the case where the
group is SELECTED BUT EMPTY -- maven exits 0 having run nothing, because a tag
was dropped or a filter stopped matching -- which is silent. It does NOT
independently catch a class ERRORING at setup: surefire records that as one test
with an error, so this guard would pass it. That case is caught by the maven
step itself exiting non-zero, before this ever runs. It also assumes a CLEAN
target/: on a developer box carrying stale reports from an earlier run, a report
can satisfy this guard without the current run having produced it. CI checks out
fresh, and the ordinary suite writes no reports for these classes because they
are excluded from it, so both assumptions hold there.

Usage:  assert_integration_group_ran.py <service-dir>
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET

TAG_RE = re.compile(r'@Tag\(\s*"integration"\s*\)')


def tagged_classes(test_root: str) -> list[str]:
    """Fully-qualified class names carrying @Tag("integration")."""
    found = []
    for dirpath, _dirs, files in os.walk(test_root):
        for name in files:
            if not name.endswith(".java"):
                continue
            path = os.path.join(dirpath, name)
            try:
                src = open(path, encoding="utf-8").read()
            except OSError:
                continue
            if not TAG_RE.search(src):
                continue
            m = re.search(r"^\s*package\s+([\w.]+)\s*;", src, re.M)
            if not m:
                continue
            found.append(f"{m.group(1)}.{name[:-5]}")
    return sorted(found)


def tests_in_report(reports_dir: str, fqcn: str) -> int | None:
    """Tests executed for *fqcn*, or None when no report exists."""
    path = os.path.join(reports_dir, f"TEST-{fqcn}.xml")
    if not os.path.isfile(path):
        return None
    try:
        return int(ET.parse(path).getroot().get("tests") or 0)
    except ET.ParseError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("service_dir", help="the service/ directory")
    args = ap.parse_args()

    test_root = os.path.join(args.service_dir, "src", "test", "java")
    reports = os.path.join(args.service_dir, "target", "surefire-reports")

    classes = tagged_classes(test_root)
    if not classes:
        print(
            'FAIL: no class carries @Tag("integration"). Either the tag was renamed or '
            "the group was retired -- in both cases this guard and the CI step that "
            "runs the group are now theatre and must be updated or deleted "
            "(nexus-yuogq).",
            file=sys.stderr,
        )
        return 1

    bad: list[str] = []
    for fqcn in classes:
        n = tests_in_report(reports, fqcn)
        if n is None:
            bad.append(f"{fqcn}: NO REPORT (never selected, or the run never reached it)")
        elif n == 0:
            bad.append(f"{fqcn}: 0 tests (errored at SETUP, or every test was filtered out)")
        else:
            print(f"ok {fqcn}: {n} test(s)")

    if bad:
        print(
            f"\nFAIL: {len(bad)} of {len(classes)} integration-tagged class(es) executed "
            "nothing. This is the nexus-yuogq failure -- a suite that runs nothing while "
            "exiting green, not a test that failed:",
            file=sys.stderr,
        )
        for line in bad:
            print(f"  {line}", file=sys.stderr)
        return 1

    print(f"\nall {len(classes)} integration-tagged class(es) executed tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
