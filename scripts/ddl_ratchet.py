#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail when a suite run executed more DDL statements than its committed ceiling (nexus-sz651).

    ddl_ratchet.py check --suite python_unit      --report <dir>/nx-ddl-report.json
    ddl_ratchet.py check --suite java_main        --report service/target/ddl-counts
    ddl_ratchet.py check --suite java_integration --report service/target/ddl-counts

The python report is the merged file ``tests/_ddl_counter.py`` writes (path printed in the
pytest terminal summary, or under ``NX_DDL_REPORT_DIR``); the java report is the directory of
per-fork files ``DdlCounts.java`` writes, which this script sums. Ceilings live in
``tests/ddl_ceiling.toml``.

Exit codes: 0 within the ceiling; 1 above it; 2 the ceiling is still ``"measure"`` (the count
measured is printed, ready to commit); 3 the run is not comparable or proves nothing (no report,
a worker or fork that could not read its log, no substrate booted, a different parallelism than
the ceiling's basis). An unreadable or empty measurement is a failed gate, never a pass (the
nexus-moht0 vacuous-gate doctrine).
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SUITES = ("python_unit", "java_main", "java_integration")
DEFAULT_CEILINGS = Path(__file__).resolve().parents[1] / "tests" / "ddl_ceiling.toml"
MEASURE = "measure"

EXIT_OK, EXIT_OVER, EXIT_UNSET, EXIT_NOT_COMPARABLE = 0, 1, 2, 3


@dataclass
class Measurement:
    total: int
    basis: int
    buckets: dict[str, int]
    problems: list[str] = field(default_factory=list)


def measure_python(report: Path) -> Measurement:
    data = json.loads(report.read_text())
    problems = []
    if not data.get("workers_booted"):
        problems.append("no worker booted the substrate, so nothing was counted")
    if not data.get("tests_run"):
        problems.append("the run recorded no tests")
    buckets = {name: t["total"] for name, t in data.get("buckets", {}).items()}
    return Measurement(sum(buckets.values()), int(data.get("workers_booted", 0)), buckets, problems)


def measure_java(directory: Path) -> Measurement:
    files = sorted(directory.glob("ddl-*.json")) if directory.is_dir() else []
    problems: list[str] = []
    if not files:
        problems.append(f"no ddl-*.json under {directory}: no fork wrote a count")
    buckets: dict[str, int] = {}
    containers = 0
    forks = 0
    for path in files:
        data = json.loads(path.read_text())
        shared, dedicated = data.get("containers", {}).get("shared", 0), data.get("containers", {}).get("dedicated", 0)
        if shared + dedicated == 0:
            continue  # a fork that ran no database test writes nothing worth counting
        forks += 1
        containers += shared + dedicated
        if data.get("log_errors"):
            problems.append(f"{path.name}: {data['log_errors']} container log(s) could not be read")
        if shared and not data.get("ready_mark_seen"):
            problems.append(f"{path.name}: the shared template's ready mark never reached the log")
        for name, t in data.get("buckets", {}).items():
            buckets[name] = buckets.get(name, 0) + t["total"]
    if files and not containers:
        problems.append("no fork started a container, so nothing was counted")
    return Measurement(sum(buckets.values()), forks, buckets, problems)


def load_ceilings(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def check(suite: str, report: Path, ceilings_path: Path = DEFAULT_CEILINGS) -> tuple[int, str]:
    if suite not in SUITES:
        return EXIT_NOT_COMPARABLE, f"unknown suite {suite!r}; expected one of {', '.join(SUITES)}"
    try:
        measured = measure_python(report) if suite == "python_unit" else measure_java(report)
        entry = load_ceilings(ceilings_path)[suite]
    except (OSError, ValueError, KeyError) as exc:
        return EXIT_NOT_COMPARABLE, f"ddl ratchet {suite}: cannot read the measurement or ceiling ({exc!r})"
    parts = ", ".join(f"{k}={v}" for k, v in sorted(measured.buckets.items()))
    line = f"ddl ratchet {suite}: total={measured.total} basis={measured.basis} ({parts})"
    if measured.problems:
        return EXIT_NOT_COMPARABLE, line + "\n  not a valid measurement: " + "; ".join(measured.problems)
    ceiling, basis = entry.get("ceiling"), entry.get("basis")
    if ceiling == MEASURE or basis == MEASURE:
        return EXIT_UNSET, (
            line + f"\n  the {suite} ceiling in {ceilings_path.name} is still \"{MEASURE}\". "
            f"Set ceiling = {measured.total} and basis = {measured.basis} once this is the full-run number."
        )
    if not isinstance(ceiling, int) or not isinstance(basis, int):
        return EXIT_NOT_COMPARABLE, line + f"\n  {suite}: ceiling and basis must be integers or \"{MEASURE}\""
    if measured.basis != basis:
        return EXIT_NOT_COMPARABLE, (
            line + f"\n  this run used {measured.basis} {'workers' if suite == 'python_unit' else 'forks'}; "
            f"the ceiling was measured at {basis}. The counts are not comparable; rerun at that width "
            "or re-measure and commit a new ceiling and basis together."
        )
    if measured.total > ceiling:
        return EXIT_OVER, (
            line + f"\n  ABOVE the ceiling of {ceiling} by {measured.total - ceiling}. "
            "A change added schema work to every run; remove it, or cut the same amount elsewhere."
        )
    headroom = ceiling - measured.total
    note = f"\n  below the ceiling by {headroom}: lower ceiling to {measured.total}" if headroom else ""
    return EXIT_OK, line + f"\n  within the ceiling of {ceiling}." + note


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="compare a run's DDL count with its ceiling")
    c.add_argument("--suite", required=True, choices=SUITES)
    c.add_argument("--report", required=True, type=Path)
    c.add_argument("--ceilings", type=Path, default=DEFAULT_CEILINGS)
    args = parser.parse_args(argv)
    code, message = check(args.suite, args.report, args.ceilings)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
