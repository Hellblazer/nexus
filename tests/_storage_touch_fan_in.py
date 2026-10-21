# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fan-in half of the tenant-saving ratchet (nexus-zv8i7): sum the sweep reports of a
whole run and check the total against ``tests/storage_touch_ceiling.toml``.

    python -m tests._storage_touch_fan_in <dir> --suite python_unit --shards 6

STANDARD LIBRARY ONLY, and it must stay so: CI runs it on a runner with no pytest installed,
which is why it is not part of ``tests/_storage_touch_guard.py`` (that imports pytest at module
top). ``tests/test_storage_touch_guard_report.py`` imports this module with pytest made
unavailable and checks its imports against ``sys.stdlib_module_names``. The guard imports
``REPORT_NAME`` and ``SWEEP_ENV`` from here, so the file name and the sweep switch have one
definition.
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

REPORT_NAME = "nx-storage-touch-report.json"
#: ``1`` asks the sweep-shape checks of a WHOLE run (some test provisioned, some
#: test clean, some touch event): a scoped run of three provisioned tests has no
#: clean one and is not vacuous. The CI shards, the census and the full-suite proof set
#: it (nexus-ey246: on CI it gates); a run that pytest-split is splitting outside CI or that
#: collects at least ``SWEEP_AUTO_FLOOR`` tests (``tests/_storage_touch_guard.py``) turns
#: it on by itself, as advice only (its report says ``sweep: true`` and nothing gates). A report written
#: without it does not count the whole run, and the fan-in refuses it.
SWEEP_ENV = "NX_STORAGE_TOUCH_SWEEP"
#: The committed ceiling on the NUMBER of tests that mint a tenant, by suite section.
DEFAULT_CEILING_FILE = Path(__file__).resolve().parent / "storage_touch_ceiling.toml"
CEILING_UNSET = "measure"
#: The most tests a ceiling may be exceeded by. Absolute and small: the count is
#: deterministic for a given selection, so the slack only absorbs host-dependent
#: skips (a Linux-only test that skips on macOS still counts as a test).
MAX_TOLERANCE_TESTS = 50


def _ceiling_values(path: Path, suite: str) -> tuple[object, int]:
    """(ceiling, tolerance) of ``[suite]`` in ``path``: the ceiling is ``"measure"``
    or a count >= 1, the tolerance a count in [0, ``MAX_TOLERANCE_TESTS``]. Raises
    ``ValueError`` (or ``OSError``) on anything else: bool, float (so no nan or inf),
    negative, over the cap, absent section or key."""
    try:
        section = tomllib.loads(path.read_text(encoding="utf-8"))[suite]
        ceiling, tolerance = section["provisioned_tests"], section["tolerance_tests"]
    except KeyError as exc:
        raise ValueError(f"missing section or key {exc}") from exc
    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or not 0 <= tolerance <= MAX_TOLERANCE_TESTS:
        raise ValueError(f"tolerance_tests must be an integer in [0, {MAX_TOLERANCE_TESTS}], got {tolerance!r}")
    if ceiling != CEILING_UNSET and (isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 1):
        raise ValueError(f'provisioned_tests must be "{CEILING_UNSET}" or an integer >= 1, got {ceiling!r}')
    return ceiling, tolerance


def _read_fan_in_report(path: Path) -> tuple[dict | None, str | None]:
    """(the numbers fan-in needs from one session report, None) or (None, why not)."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        tests, provisioned = report["counts"]["tests"], report["counts"]["provisioned"]
        run_id, sweep = report["run_id"], report["sweep"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"{path}: cannot read the report ({type(exc).__name__}: {exc})"
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (tests, provisioned)) or provisioned > tests:
        return None, f"{path}: counts are not 0 <= provisioned <= tests integers (tests={tests!r}, provisioned={provisioned!r})"
    if sweep is not True or not isinstance(run_id, str) or not run_id:
        return None, (
            f"{path}: not a whole-run report (sweep={sweep!r}, run_id={run_id!r}); only a run with "
            f"{SWEEP_ENV}=1 (or a pytest-split slice, or one large enough to ask for it) counts every test it selected, and a scoped run would undercount"
        )
    return {"file": str(path), "run_id": run_id, "tests": tests, "provisioned": provisioned}, None


def fan_in(directory: Path, *, suite: str, shards: int, path: Path = DEFAULT_CEILING_FILE) -> tuple[dict, list[str]]:
    """Ratchet the NUMBER of tests that minted a tenant, summed over every session
    report under ``directory``, against the committed ceiling (nexus-zv8i7).
    Returns the entry (also the JSON the CLI prints) and the problem lines (empty
    when the total is within ceiling + tolerance).

    Gating the total, not a per-shard share: CI splits the suite by duration into
    contiguous chunks, so a shard's share is its file neighbourhood's, moves by
    more than any tolerance when a file crosses a chunk boundary, and a ceiling at
    the worst shard lets every lighter shard regress freely (the per-shard design
    nexus-2rhif recorded as refuted for the DDL ratchet). Provisioned is counted
    per test, not per worker, so the total has no dependence on how the run was
    split. The per-report share stays in the entry as advice and gates nothing.
    The tolerance is an absolute number of tests, capped at ``MAX_TOLERANCE_TESTS``.

    ``shards`` is how many reports the run must have left: a missing shard would under-count and
    pass, and an extra one (a second download, a stray file) would over-count, so any other number
    of reports is a failure before a single count is read.

    A ceiling nobody set (``"measure"``) fails and prints the number to commit
    (the nexus-moht0 doctrine); so does a directory with no report, a report that
    is not a whole-run (sweep) report, a repeated run id (the same artifact
    downloaded twice would double the total), and every unreadable file, section
    or value. A ratchet that checked nothing must not read as a pass."""
    entry: dict = {
        "suite": suite, "file": str(path), "directory": str(directory), "shards": shards, "reports": [],
        "tests": 0, "provisioned": 0, "provisioned_per_test": 0.0,
        "ceiling": None, "tolerance": None, "limit": None, "lower_to": None, "status": "ok",
    }
    head = f"provisioned ratchet [{suite}] in {path.name}"
    found = sorted(directory.rglob(REPORT_NAME)) if directory.is_dir() else []
    if not found:
        entry["status"] = "no_reports"
        return entry, [f"{head}: no {REPORT_NAME} under {directory}; a ratchet over nothing passes nothing"]
    if isinstance(shards, bool) or not isinstance(shards, int) or shards < 1:
        entry["status"] = "bad_shards"
        return entry, [f"{head}: shards must be an integer >= 1, got {shards!r}"]
    if len(found) != shards:
        entry["status"] = "shard_count"
        return entry, [
            f"{head}: expected {shards} report(s) under {directory}, found {len(found)}"
            + (f" ({', '.join(str(p) for p in found[:8])})" if found else "")
            + "; a missing shard under-counts and passes, an extra one over-counts"
        ]
    problems: list[str] = []
    reports: list[dict] = []
    for report_path in found:
        numbers, why = _read_fan_in_report(report_path)
        if numbers is None:
            problems.append(f"{head}: {why}")
        elif any(r["run_id"] == numbers["run_id"] for r in reports):
            problems.append(f"{head}: {report_path} repeats run id {numbers['run_id']}; the same report counted twice would double the total")
        else:
            reports.append(numbers)
    if problems:
        entry["status"] = "bad_reports"
        return entry, problems
    tests, provisioned = sum(r["tests"] for r in reports), sum(r["provisioned"] for r in reports)
    entry.update({
        "tests": tests, "provisioned": provisioned,
        "provisioned_per_test": round(provisioned / tests, 6) if tests else 0.0,
        "reports": [{**r, "provisioned_per_test": round(r["provisioned"] / r["tests"], 6) if r["tests"] else 0.0} for r in reports],
    })
    if not tests:
        entry["status"] = "empty"
        return entry, [f"{head}: the {len(reports)} report(s) recorded no tests; a ratchet over nothing passes nothing"]
    try:
        ceiling, tolerance = _ceiling_values(path, suite)
    except (OSError, ValueError) as exc:  # tomllib.TOMLDecodeError is a ValueError
        entry["status"] = "unreadable"
        return entry, [f"{head}: cannot read the ceiling ({type(exc).__name__}: {exc}); a ratchet that cannot be read passes nothing"]
    entry["ceiling"], entry["tolerance"] = ceiling, tolerance
    measured = f"{provisioned} of {tests} tests minted a tenant across {len(reports)} report(s)"
    if ceiling == CEILING_UNSET:
        entry["status"] = "unset"
        return entry, [
            f"{head}: no ceiling set; measured provisioned_tests={provisioned} ({measured}). Commit "
            f"provisioned_tests = {provisioned} for [{suite}]; a ceiling nobody set does not pass a run."
        ]
    entry["limit"] = limit = ceiling + tolerance
    if provisioned > limit:
        entry["status"] = "over"
        return entry, [
            f"{head}: provisioned_tests={provisioned} ({measured}) is above the ceiling {ceiling} "
            f"(+{tolerance} tolerance = {limit}). The tenant saving is eroding: drop the t2_tenant marks the tests "
            "no longer need, or raise the ceiling in a reviewed commit that says why."
        ]
    if provisioned < ceiling:
        entry["lower_to"] = provisioned  # reduce-only: the saving held, so the ceiling can follow it down
    return entry, []


def main(argv: list[str] | None = None) -> int:
    """``python -m tests._storage_touch_fan_in <dir> --suite <name> --shards <N>``: sum the
    N sweep reports under ``<dir>`` and check them against the committed ceiling.
    Exit 0 within the ceiling, 1 on any problem. Per-report shares are printed as
    advice only."""
    parser = argparse.ArgumentParser(prog="python -m tests._storage_touch_fan_in", description=main.__doc__)
    parser.add_argument("directory", type=Path, help="holds the nx-storage-touch-report.json of every shard (any depth)")
    parser.add_argument("--suite", required=True, help="section of the ceiling file: python_unit or docs_readers")
    parser.add_argument("--shards", required=True, type=int, help="how many reports the run must have left (1 for an unsharded run)")
    parser.add_argument("--ceiling-file", type=Path, default=DEFAULT_CEILING_FILE)
    args = parser.parse_args(argv)
    entry, problems = fan_in(args.directory, suite=args.suite, shards=args.shards, path=args.ceiling_file)
    out = sys.stdout
    out.write(
        f"storage-touch fan-in [{entry['suite']}]: shards={entry['shards']} reports={len(entry['reports'])} tests={entry['tests']} "
        f"provisioned={entry['provisioned']} ceiling={entry['ceiling']} tolerance={entry['tolerance']} "
        f"limit={entry['limit']} status={entry['status']}\n"
    )
    for r in entry["reports"]:
        out.write(f"  {r['file']}: tests={r['tests']} provisioned={r['provisioned']} share={r['provisioned_per_test']:.6f} (advisory)\n")
    if entry["lower_to"] is not None:
        out.write(f"  the ceiling can be lowered to {entry['lower_to']} (ceilings are reduce-only)\n")
    for problem in problems:
        out.write(problem + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
