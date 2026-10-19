#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail when a test run wrote more bytes, or ran more DDL, than its committed ceiling (nexus-sz651).

    ddl_ratchet.py check  --suite python_unit      --report <dir>/nx-ddl-report.json
    ddl_ratchet.py check  --suite java_main        --report service/target/ddl-counts [--run <id>]
    ddl_ratchet.py check  --suite java_integration --report service/target/ddl-counts [--run <id>]
    ddl_ratchet.py fan-in --suite python_unit      --report shard1.json shard2.json ... --shards N

THE TARGET IS BYTES WRITTEN (disk thrashing); the DDL count is the secondary diagnostic that
explains where the bytes come from. Both are ratcheted. Bytes are WAL bytes the test clusters
generated (``pg_current_wal_lsn`` between the probe's marks): on a tmpfs cluster that is churn,
not disk, which is what a disk-backed cluster would write. WAL varies a little from run to run
(autovacuum, hint-bit full-page images), so every bytes ceiling carries a tolerance band
(``bytes_tolerance``, a fraction); DDL is deterministic and carries ``ddl_tolerance`` (default 0).

A shard is not the run. CI splits the Python suite into shards of differing width and test count,
so ``check`` compares INTENSIVE numbers, which do not move with how the work was split: bytes per
test, DDL per test (both for the tests' own time), and the migration's bytes and DDL per worker.
Java totals are near width-invariant but the migration is paid once per fork, so its metrics
are per fork and the rest (``other_*``: every clone, every dedicated container, the tests' own
work) is a total. A full-run total exists only through ``fan-in``, which sums N shard reports and
fails unless exactly ``--shards`` of them are given; that total is width-dependent, so its ceiling
carries the worker and shard counts it was measured at and a different width is not compared.

A Java directory can hold files from several runs and from both suites (``service/target`` outlives
a run). Each fork file names its suite (``-Dnexus.ddl.suite``, set by the pom and CI) and its run
(``-Dnexus.ddl.run``, the Maven build timestamp): ``check`` reads only the requested suite and
refuses to guess between several runs of it (pass ``--run``).

Exit codes, distinct from argparse's 2 and from a crash's 1: 0 within every ceiling; 10 a metric above
its ceiling; 11 a ceiling is still ``"measure"`` (the measured numbers are printed, ready to
commit); 12 the run is not comparable or proves nothing (no report, a worker or fork that could not
read its log or marks, no substrate booted, several runs mixed, a shard count that is not the
expected one). An unreadable or empty measurement is a failed gate, never a pass (the nexus-moht0
vacuous-gate doctrine).
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SUITES = ("python_unit", "java_main", "java_integration")
JAVA_SUITE = {"java_main": "main", "java_integration": "integration"}
TOTAL_SECTION = "python_unit_total"
DEFAULT_CEILINGS = Path(__file__).resolve().parents[1] / "tests" / "ddl_ceiling.toml"
MEASURE = "measure"

#: Intensive metrics a ``check`` compares, per suite.
PYTHON_METRICS = (
    "wal_bytes_per_test", "ddl_per_test", "migration_wal_bytes_per_worker", "migration_ddl_per_worker",
)
JAVA_METRICS = ("migration_wal_bytes_per_fork", "migration_ddl_per_fork", "other_wal_bytes", "other_ddl")
#: ``fan-in``: totals with a tolerance, and the counts the totals were measured at (compared exactly).
TOTAL_METRICS = ("wal_bytes", "ddl")
TOTAL_BASIS = ("workers", "shards")

EXIT_OK, EXIT_OVER, EXIT_UNSET, EXIT_NOT_COMPARABLE = 0, 10, 11, 12


@dataclass
class Measurement:
    metrics: dict[str, float]
    #: What the metrics were measured at, for the message and (fan-in) the exact-match basis.
    basis: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    extra: str = ""


def _is_bytes(metric: str) -> bool:
    return "wal_bytes" in metric


# ── measuring ────────────────────────────────────────────────────────────────


def python_problems(data: dict) -> list[str]:
    """Why one merged python report does not prove anything, or nothing."""
    problems = []
    booted = int(data.get("workers_booted", 0))
    if not booted:
        problems.append("no worker booted the substrate, so nothing was counted")
    if not data.get("tests_run"):
        problems.append("the run recorded no tests")
    wal = data.get("wal") or {}
    if booted and (wal.get("migration_bytes") is None or wal.get("tests_bytes") is None):
        problems.append(
            f"WAL bytes are unmeasured ({wal.get('workers_complete', 0)} of {booted} workers carry both phases); "
            "bytes are the target, so a run without them proves nothing"
        )
    if booted and data.get("workers_ready_mark_seen", 0) != booted:
        problems.append(
            f"the ready mark reached the log of {data.get('workers_ready_mark_seen', 0)} of {booted} booted workers"
        )
    return problems


def measure_python(report: Path) -> Measurement:
    data = json.loads(report.read_text())
    problems = python_problems(data)
    booted, tests_run = int(data.get("workers_booted", 0)), int(data.get("tests_run", 0))
    buckets = data.get("buckets", {})
    wal = data.get("wal") or {}
    metrics: dict[str, float] = {}
    if not problems:
        metrics = {
            "wal_bytes_per_test": wal["tests_bytes"] / tests_run,
            "ddl_per_test": buckets["tests"]["total"] / tests_run,
            "migration_wal_bytes_per_worker": wal["migration_bytes"] / booted,
            "migration_ddl_per_worker": buckets["migration"]["total"] / booted,
        }
    extra = f"tests={tests_run} workers_booted={booted}"
    if data.get("recovered_workers"):
        extra += f" recovered_workers={data['recovered_workers']}"
    if data.get("uncounted_clusters") is not None:
        extra += f" uncounted_clusters={data['uncounted_clusters']}"
    io = data.get("cgroup_io")
    if io and io.get("wbytes_delta") is not None:
        extra += f" cgroup_io_wbytes={io['wbytes_delta']} (the job's block-device writes; informational)"
    return Measurement(metrics, {"workers": booted, "tests": tests_run}, problems, extra)


def measure_java(directory: Path, suite: str, run: str | None = None) -> Measurement:
    wanted = JAVA_SUITE[suite]
    everything = sorted(directory.glob("ddl-*.json")) if directory.is_dir() else []
    problems: list[str] = []
    loaded = [(path, json.loads(path.read_text())) for path in everything]
    of_suite = [(p, d) for p, d in loaded if d.get("suite") == wanted]
    other = sorted({str(d.get("suite")) for _, d in loaded if d.get("suite") != wanted})
    if not of_suite:
        problems.append(
            f"no ddl-*.json of suite {wanted!r} under {directory} ({len(everything)} file(s) of other suites: "
            f"{', '.join(other) or 'none'}); the pom sets -Dnexus.ddl.suite"
        )
    runs = sorted({str(d.get("run")) for _, d in of_suite})
    if run is not None:
        of_suite = [(p, d) for p, d in of_suite if str(d.get("run")) == run]
        if not of_suite and runs:
            problems.append(f"no file of suite {wanted!r} belongs to run {run!r} (runs present: {', '.join(runs)})")
    elif len(runs) > 1:
        problems.append(
            f"files of suite {wanted!r} come from {len(runs)} runs ({', '.join(runs)}); a total over several runs "
            "is nobody's measurement. Clear the directory or pass --run"
        )
        of_suite = []
    chosen = sorted({str(d.get("run")) for _, d in of_suite})
    forks = shared = 0
    totals = {"migration_bytes": 0, "migration_ddl": 0, "other_bytes": 0, "other_ddl": 0}
    for path, data in of_suite:
        containers = data.get("containers", {})
        s, dedicated = containers.get("shared", 0), containers.get("dedicated", 0)
        if data.get("containers_unread"):
            problems.append(f"{path.name}: {data['containers_unread']} container(s) started and never read (a killed fork?)")
        if s + dedicated == 0 and not data.get("containers_unread"):
            continue  # a fork that ran no database test writes nothing worth counting
        forks += 1
        shared += s
        if data.get("log_errors"):
            problems.append(f"{path.name}: {data['log_errors']} container log(s) could not be read")
        if s and not data.get("ready_mark_seen"):
            problems.append(f"{path.name}: the shared template's ready mark never reached the log")
        wal = data.get("wal") or {}
        if wal.get("incomplete_containers"):
            problems.append(f"{path.name}: {wal['incomplete_containers']} container(s) lack the WAL marks")
        b = data.get("buckets", {})
        totals["migration_bytes"] += wal.get("shared_migration_bytes", 0)
        totals["migration_ddl"] += b.get("shared_migration", {}).get("total", 0)
        totals["other_bytes"] += wal.get("shared_tests_bytes", 0) + wal.get("dedicated_bytes", 0)
        totals["other_ddl"] += sum(b.get(n, {}).get("total", 0) for n in ("shared_tests", "shared_empty", "dedicated"))
    if of_suite and not forks:
        problems.append("no fork started a container, so nothing was counted")
    if forks and not shared:
        problems.append("no fork booted the shared cluster, so the migration was not measured")
    metrics: dict[str, float] = {}
    if not problems:
        metrics = {
            "migration_wal_bytes_per_fork": totals["migration_bytes"] / shared,
            "migration_ddl_per_fork": totals["migration_ddl"] / shared,
            "other_wal_bytes": totals["other_bytes"],
            "other_ddl": totals["other_ddl"],
        }
    extra = f"run={','.join(chosen) or '-'} forks={forks} shared_clusters={shared}"
    return Measurement(metrics, {"forks": forks}, problems, extra)


def fan_in(reports: list[Path], shards: int) -> Measurement:
    problems: list[str] = []
    if len(reports) != shards:
        problems.append(f"{len(reports)} shard report(s) given, {shards} expected: a total over a missing shard is not the run's")
    wal_bytes = ddl = workers = 0
    for path in reports:
        data = json.loads(path.read_text())
        for problem in python_problems(data):
            problems.append(f"{path.name}: {problem}")
        wal = data.get("wal") or {}
        wal_bytes += (wal.get("migration_bytes") or 0) + (wal.get("tests_bytes") or 0)
        ddl += data.get("total", {}).get("total", 0)
        workers += int(data.get("workers_booted", 0))
    metrics: dict[str, float] = {} if problems else {"wal_bytes": wal_bytes, "ddl": ddl}
    return Measurement(metrics, {"workers": workers, "shards": len(reports)}, problems, f"shards={len(reports)} workers={workers}")


# ── comparing ────────────────────────────────────────────────────────────────


def uncounted_clusters(path: Path, prefix: str = "") -> int | None:
    """The sum of the ``[uncounted]`` table of the cluster-start-site allowlist (only the sites whose key
    starts with *prefix*), or None when the file is absent."""
    try:
        table = tomllib.loads(path.read_text()).get("uncounted", {})
        return sum(int(n) for k, n in table.items() if k.startswith(prefix))
    except (OSError, ValueError):
        return None


def load_ceilings(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fmt(value: float) -> str:
    return f"{value:.6g}" if isinstance(value, float) and not float(value).is_integer() else f"{int(value)}"


def compare(label: str, measured: Measurement, entry: dict, metric_names: tuple[str, ...], ceilings_name: str,
            section: str, basis_names: tuple[str, ...] = ()) -> tuple[int, str]:
    shown = " ".join(f"{k}={_fmt(v)}" for k, v in measured.metrics.items())
    head = f"ddl ratchet {label}: {shown} [{measured.extra}]"
    if measured.problems:
        return EXIT_NOT_COMPARABLE, head + "\n  not a valid measurement: " + "; ".join(measured.problems)
    bytes_tol, ddl_tol = entry.get("bytes_tolerance", 0.05), entry.get("ddl_tolerance", 0.0)
    if not _number(bytes_tol) or not _number(ddl_tol) or bytes_tol < 0 or ddl_tol < 0:
        return EXIT_NOT_COMPARABLE, head + f"\n  {section}: bytes_tolerance and ddl_tolerance must be non-negative numbers"
    unset: list[str] = []
    for name in (*basis_names, *metric_names):
        value = entry.get(name)
        if value is None:
            return EXIT_NOT_COMPARABLE, head + f"\n  {ceilings_name} [{section}] has no {name!r}"
        if value == MEASURE:
            unset.append(name)
        elif not _number(value) or value <= 0:
            return EXIT_NOT_COMPARABLE, head + f"\n  {section}.{name} must be a positive number or \"{MEASURE}\", got {value!r}"
    for name in basis_names:
        if entry[name] != MEASURE and measured.basis[name] != entry[name]:
            return EXIT_NOT_COMPARABLE, head + (
                f"\n  this run has {measured.basis[name]} {name}; the total ceiling was measured at {entry[name]}. "
                "A full-run total is width-dependent: rerun at that width, or re-measure and commit a new ceiling "
                "and its basis together."
            )
    over: list[str] = []
    notes: list[str] = []
    for name in metric_names:
        if entry[name] == MEASURE:
            continue
        tol = bytes_tol if _is_bytes(name) else ddl_tol
        limit = entry[name] * (1 + tol)
        value = measured.metrics[name]
        if value > limit:
            over.append(f"{name} {_fmt(value)} is above the ceiling {_fmt(entry[name])}"
                        + (f" (+{tol:.0%} band = {_fmt(limit)})" if tol else ""))
        elif value < entry[name] * (1 - tol):
            notes.append(f"{name} {_fmt(value)} is below {_fmt(entry[name])}: lower the ceiling in the commit that lowered it")
    if over:
        return EXIT_OVER, head + "\n  ABOVE: " + "; ".join(over) + (
            ". A change added writes or schema work to every run; remove it, or cut the same amount elsewhere."
        )
    if unset:
        snippet = "\n".join(
            f"  {n} = {_fmt(measured.metrics[n]) if n in measured.metrics else _fmt(measured.basis[n])}" for n in unset
        )
        return EXIT_UNSET, head + (
            f"\n  [{section}] in {ceilings_name} is still \"{MEASURE}\" for {', '.join(unset)}. "
            f"Set, once this is the full-run measurement:\n{snippet}"
        )
    return EXIT_OK, head + "\n  within every ceiling." + ("".join(f"\n  note: {n}" for n in notes))


def check(suite: str, report: Path, ceilings_path: Path = DEFAULT_CEILINGS, run: str | None = None) -> tuple[int, str]:
    if suite not in SUITES:
        return EXIT_NOT_COMPARABLE, f"unknown suite {suite!r}; expected one of {', '.join(SUITES)}"
    try:
        measured = measure_python(report) if suite == "python_unit" else measure_java(report, suite, run)
        entry = load_ceilings(ceilings_path)[suite]
    except (OSError, ValueError, KeyError) as exc:
        return EXIT_NOT_COMPARABLE, f"ddl ratchet {suite}: cannot read the measurement or ceiling ({exc!r})"
    if suite != "python_unit":
        count = uncounted_clusters(ceilings_path.parent / "ddl_uncounted_clusters.toml", "service/")
        if count is not None:
            measured.extra += f" | {count} uncounted clusters (the jOOQ code generator's build-time container)"
    names = PYTHON_METRICS if suite == "python_unit" else JAVA_METRICS
    return compare(suite, measured, entry, names, ceilings_path.name, suite)


def check_fan_in(suite: str, reports: list[Path], shards: int, ceilings_path: Path = DEFAULT_CEILINGS) -> tuple[int, str]:
    if suite != "python_unit":
        return EXIT_NOT_COMPARABLE, f"fan-in sums python shard reports; {suite!r} has none"
    try:
        measured = fan_in(reports, shards)
        entry = load_ceilings(ceilings_path)[TOTAL_SECTION]
    except (OSError, ValueError, KeyError) as exc:
        return EXIT_NOT_COMPARABLE, f"ddl ratchet {suite} fan-in: cannot read the measurement or ceiling ({exc!r})"
    return compare(f"{suite} fan-in", measured, entry, TOTAL_METRICS, ceilings_path.name, TOTAL_SECTION, TOTAL_BASIS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="compare one run's (or one shard's) intensive numbers with their ceilings")
    c.add_argument("--suite", required=True, choices=SUITES)
    c.add_argument("--report", required=True, type=Path)
    c.add_argument("--run", help="java: the nexus.ddl.run id to read (default: the only run present)")
    c.add_argument("--ceilings", type=Path, default=DEFAULT_CEILINGS)
    f = sub.add_parser("fan-in", help="sum N python shard reports and compare the full-run total")
    f.add_argument("--suite", required=True, choices=("python_unit",))
    f.add_argument("--report", required=True, type=Path, nargs="+")
    f.add_argument("--shards", required=True, type=int, help="how many shard reports the run has")
    f.add_argument("--ceilings", type=Path, default=DEFAULT_CEILINGS)
    args = parser.parse_args(argv)
    if args.command == "check":
        code, message = check(args.suite, args.report, args.ceilings, args.run)
    else:
        code, message = check_fan_in(args.suite, args.report, args.shards, args.ceilings)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
