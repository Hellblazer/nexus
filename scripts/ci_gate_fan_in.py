#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""pytest-gate's whole-run checks, every one evaluated: one step, one verdict.

    python3 scripts/ci_gate_fan_in.py --reports-dir ddl-reports --suite python_unit --shards 6 \
        [--advisory] [--bytes-advisory]

Runs, in this order, from the repo root:

1. the DDL/bytes ratchet fan-in          (scripts/ddl_ratchet.py fan-in)
2. the tenant-saving ratchet fan-in      (python -m tests._storage_touch_fan_in)
3. the shard-collection partition check  (python -m tests._shard_collection)

GitHub skips the steps after a failed one, so as three steps a WAL overage in (1) hid a storage-touch
overage in (2) and cost a CI cycle. Here each check runs whatever the others returned, prints its own
verdict (stdout and stderr are inherited, never reworded), and the script ends with one summary line
naming every check that failed, exiting 1 once if any did. A check that cannot even start counts as failed.

``--advisory`` and ``--bytes-advisory`` belong to the DDL check alone, as they did in the step. The two
ceiling flags point checks 1 and 2 at other ceiling files (the tests use them); CI leaves them off.

STANDARD LIBRARY ONLY, python3 >= 3.11 like the checks it runs: pytest-gate has no pytest installed.
"""
from __future__ import annotations

import argparse
import glob
import subprocess
import sys
from dataclasses import dataclass

DDL_REPORT = "nx-ddl-report.json"


@dataclass(frozen=True)
class Check:
    name: str
    argv: list[str]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--reports-dir", required=True, help="where the shard reports were downloaded (one subdirectory a shard)")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--shards", required=True, type=int)
    parser.add_argument("--advisory", action="store_true", help="forwarded to the DDL fan-in")
    parser.add_argument("--bytes-advisory", action="store_true", help="forwarded to the DDL fan-in")
    parser.add_argument("--ddl-ceilings", help="the DDL fan-in's --ceilings (default: its own)")
    parser.add_argument("--storage-touch-ceilings", help="the storage-touch fan-in's --ceiling-file (default: its own)")
    return parser.parse_args(argv)


def build_checks(args: argparse.Namespace) -> list[Check]:
    """The three commands the three workflow steps used to run, in the same order and spelling."""
    pattern = f"{args.reports_dir}/*/{DDL_REPORT}"
    # What the shell did with the old step's unquoted glob: the sorted matches, or the pattern itself when
    # nothing matches, so the ratchet reports the missing reports in its own words (exit 12).
    reports = sorted(glob.glob(pattern)) or [pattern]
    shards = str(args.shards)
    py = sys.executable
    ddl = [py, "scripts/ddl_ratchet.py", "fan-in", "--suite", args.suite, "--shards", shards, "--report", *reports]
    if args.ddl_ceilings:
        ddl += ["--ceilings", args.ddl_ceilings]
    if args.advisory:
        ddl.append("--advisory")
    if args.bytes_advisory:
        ddl.append("--bytes-advisory")
    touch = [py, "-m", "tests._storage_touch_fan_in", args.reports_dir, "--suite", args.suite, "--shards", shards]
    if args.storage_touch_ceilings:
        touch += ["--ceiling-file", args.storage_touch_ceilings]
    collection = [py, "-m", "tests._shard_collection", args.reports_dir, "--shards", shards]
    return [
        Check("DDL/bytes ratchet over the whole run (fan-in)", ddl),
        Check("Tenant-saving ratchet over the whole run (storage-touch fan-in)", touch),
        Check("Every shard split the same collection", collection),
    ]


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def run_check(check: Check) -> int | None:
    """The check's exit code; None when it could not start."""
    _say(f"== {check.name}")
    try:
        return subprocess.run(check.argv, check=False).returncode
    except OSError as exc:
        _say(f"ci_gate_fan_in: {check.name} could not start ({type(exc).__name__}: {exc})")
        return None


def main(argv: list[str] | None = None) -> int:
    checks = build_checks(parse_args(argv))
    failed: list[str] = []
    for check in checks:
        code = run_check(check)
        if code != 0:
            failed.append(f"{check.name} ({'could not start' if code is None else f'exit {code}'})")
    if failed:
        _say(f"ci_gate_fan_in: FAILED {len(failed)} of {len(checks)} checks: " + "; ".join(failed))
        return 1
    _say(f"ci_gate_fan_in: all {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
