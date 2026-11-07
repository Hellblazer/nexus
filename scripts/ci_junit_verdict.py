# SPDX-License-Identifier: AGPL-3.0-or-later
"""Judge a pytest junit report by its counts, never by the text pytest prints (nexus-odkc0).

``ci_junit_verdict.py REPORT --expect N`` exits 0 when the report records exactly N tests, all passed: no
skip, failure or error. Anything else exits 1 with the counts. Reading the printed summary is what made
develop red at cb1f5587d: the run's output also carries the storage-touch census line ("skipped=0"), so a
substring test for "skipped" refused a clean "2 passed, 2 warnings" run.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET

COUNTS: tuple[str, ...] = ("tests", "skipped", "failures", "errors")


def counts(report: str) -> dict[str, int]:
    """The totals over every testsuite in *report* (pytest writes one, under a testsuites root)."""
    root = ET.parse(report).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        raise ValueError(f"{report}: no testsuite element")
    return {key: sum(int(s.get(key, "0")) for s in suites) for key in COUNTS}


def verdict(totals: dict[str, int], expect: int) -> str | None:
    """None when *totals* is exactly *expect* tests all passed, else why not."""
    if totals["tests"] == expect and totals["skipped"] == totals["failures"] == totals["errors"] == 0:
        return None
    shown = ", ".join(f"{key}={totals[key]}" for key in COUNTS)
    return f"expected {expect} passed with no skip, failure or error; the report has {shown}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Judge a pytest junit report by its counts.")
    parser.add_argument("report")
    parser.add_argument("--expect", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        totals = counts(args.report)
    except (OSError, ET.ParseError, ValueError) as exc:
        sys.stderr.write(f"unreadable junit report: {exc}\n")
        return 1
    why = verdict(totals, args.expect)
    if why is not None:
        sys.stderr.write(why + "\n")
        return 1
    sys.stdout.write(f"{totals['tests']} passed, none skipped\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
