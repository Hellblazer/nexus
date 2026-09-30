#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print the slowest Java test classes from Surefire XML reports.

nexus-rjk2a. The Java CI job's duration ranged 21 to 34 minutes over 14 runs
and no per-class timing was captured, so nothing could say WHICH classes grew.
This reads ``TEST-*.xml`` under a reports directory, takes each
``<testsuite time=...>``, and prints the slowest classes plus the summed time.

It is diagnostic only and NEVER fails a run: a missing or empty directory, or a
report that will not parse, prints a line and exits 0. The service-ci step that
calls it runs ``if: always()``, so it must not turn a timeout into a second
failure or hide the first.

The summed time is the sum of per-class times, not wall clock (setup between
classes and JVM start are outside it), so it reads a little under the step time.

The report directory holds both mvn invocations of the job: the integration
group writes different classes (they are excluded from the main run), and
Surefire does not clear the directory between invocations, so one call after
both steps sees every class.

Python stdlib only; runs on the runner's system python3 with no ``uv sync``.

Usage:  surefire_slowest.py [reports_dir] [--top N]
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_DIR = "service/target/surefire-reports"


def _seconds(raw: str | None) -> float:
    # Surefire formats with the JVM locale; "1,234.5" occurs on some runners.
    return float((raw or "0").replace(",", ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("reports_dir", nargs="?", default=DEFAULT_DIR)
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args(argv)

    d = Path(args.reports_dir)
    files = sorted(d.glob("TEST-*.xml")) if d.is_dir() else []
    if not files:
        print(f"surefire_slowest: no surefire reports under {d}; nothing to rank")
        return 0

    rows: list[tuple[float, str, int]] = []
    unreadable = 0
    for f in files:
        try:
            root = ET.parse(f).getroot()
            rows.append(
                (
                    _seconds(root.get("time")),
                    root.get("name") or f.stem.removeprefix("TEST-"),
                    int(root.get("tests") or 0),
                )
            )
        except (ET.ParseError, ValueError, OSError):
            unreadable += 1
            print(f"surefire_slowest: unreadable report skipped: {f.name}")

    if not rows:
        print(f"surefire_slowest: no readable surefire reports under {d}")
        return 0

    rows.sort(key=lambda r: (-r[0], r[1]))
    total = sum(r[0] for r in rows)
    print(f"Slowest {min(args.top, len(rows))} of {len(rows)} test classes (seconds, tests):")
    for secs, name, tests in rows[: args.top]:
        print(f"{secs:10.2f}  {tests:5d}  {name}")
    print(
        f"Total: {len(rows)} classes, {sum(r[2] for r in rows)} tests, "
        f"{total:.2f} s summed class time ({total / 60:.1f} min)"
    )
    if unreadable:
        print(f"surefire_slowest: {unreadable} unreadable report(s) not counted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
