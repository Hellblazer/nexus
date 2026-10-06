#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail unless a pytest JUnit report shows a run that actually ran its tests.

A gate that skips itself on a platform passes there having proved nothing
(the nexus-moht0 vacuous-gate doctrine). The Windows conformance job
(``windows-pg-bundle-rehearsal.yml``, RDR-224 nexus-f9bgu.19) runs this over
its report: at least ``--min-passed`` tests passed, no more than
``--max-skipped`` were skipped, none failed or errored. pytest writes an
``xfail`` into the report as a skip, so the ceiling counts the suite's
documented GAP cells as well as its platform skips.

A count floor has slack (a tenth of the suite can vanish unnoticed) and names
nothing, so a skipped real-kernel test stays inside the skip ceiling. Each
``--require-passed PATTERN`` closes that for the tests that only a real Windows
run can exercise: PATTERN is a substring of the dotted ``classname.name`` the
report records for a test; at least one test must match, and every test that
matches must have passed (a skip, a failure or an error is a miss).

Exit 0 on a run that meets the floor, 1 on one that does not, 2 on a report
that cannot be read (an unreadable report is a failed gate, never a pass).
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def counts(report: Path) -> dict[str, int]:
    """``tests`` / ``passed`` / ``failed`` / ``skipped`` over every testcase."""
    root = ET.parse(report).getroot()  # noqa: S314 — a report this job's own pytest just wrote
    tally = {"tests": 0, "passed": 0, "failed": 0, "skipped": 0}
    for case in root.iter("testcase"):
        tally["tests"] += 1
        if case.find("skipped") is not None:
            tally["skipped"] += 1
        elif case.find("failure") is not None or case.find("error") is not None:
            tally["failed"] += 1
        else:
            tally["passed"] += 1
    return tally


def outcomes(report: Path) -> list[tuple[str, str]]:
    """``(classname.name, "passed" | "failed" | "skipped")`` for every testcase."""
    root = ET.parse(report).getroot()  # noqa: S314 — a report this job's own pytest just wrote
    out: list[tuple[str, str]] = []
    for case in root.iter("testcase"):
        ident = f"{case.get('classname', '')}.{case.get('name', '')}"
        if case.find("skipped") is not None:
            out.append((ident, "skipped"))
        elif case.find("failure") is not None or case.find("error") is not None:
            out.append((ident, "failed"))
        else:
            out.append((ident, "passed"))
    return out


def missing_required(report: Path, patterns: list[str]) -> list[str]:
    """The ``--require-passed`` patterns *report* does not satisfy, each with why."""
    seen = outcomes(report)
    problems = []
    for pattern in patterns:
        matched = [(ident, status) for ident, status in seen if pattern in ident]
        if not matched:
            problems.append(f"required test {pattern!r} is not in the report")
            continue
        bad = [f"{ident} ({status})" for ident, status in matched if status != "passed"]
        if bad:
            problems.append(f"required test {pattern!r} did not pass: {', '.join(bad)}")
    return problems


def check(
    report: Path, *, min_passed: int, max_skipped: int, require_passed: list[str] | None = None,
) -> list[str]:
    """The reasons *report* misses the floor; empty when it meets it."""
    tally = counts(report)
    problems = []
    if tally["failed"]:
        problems.append(f"{tally['failed']} test(s) failed or errored")
    if tally["passed"] < min_passed:
        problems.append(f"only {tally['passed']} passed, the floor is {min_passed}")
    if tally["skipped"] > max_skipped:
        problems.append(f"{tally['skipped']} skipped (xfail counts), the ceiling is {max_skipped}")
    problems.extend(missing_required(report, require_passed or []))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path)
    parser.add_argument("--min-passed", type=int, required=True)
    parser.add_argument("--max-skipped", type=int, required=True)
    parser.add_argument(
        "--require-passed", action="append", default=[], metavar="PATTERN",
        help="a substring of 'classname.name'; at least one test must match and every match must pass (repeatable)",
    )
    args = parser.parse_args(argv)
    try:
        problems = check(
            args.report, min_passed=args.min_passed, max_skipped=args.max_skipped,
            require_passed=args.require_passed,
        )
        tally = counts(args.report)
    except (OSError, ET.ParseError) as exc:
        sys.stderr.write(f"check_junit_floor: cannot read {args.report}: {exc}\n")
        return 2
    sys.stdout.write(
        f"check_junit_floor: {tally['passed']} passed, {tally['skipped']} skipped, "
        f"{tally['failed']} failed of {tally['tests']}\n"
    )
    for problem in problems:
        sys.stderr.write(f"check_junit_floor: {problem}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
