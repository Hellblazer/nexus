#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail when a test run wrote more bytes, or ran more DDL, than its committed ceiling (nexus-sz651).

    ddl_ratchet.py check  --suite python_unit      --report <dir>/nx-ddl-report.json --advisory
    ddl_ratchet.py check  --suite java_main        --report service/target/ddl-counts [--run <id>] [--advisory]
    ddl_ratchet.py check  --suite java_integration --report service/target/ddl-counts [--run <id>] [--advisory]
    ddl_ratchet.py fan-in --suite python_unit      --report shard1.json shard2.json ... --shards N [--advisory] [--bytes-advisory]
                                                   (the Python gate; enforced only once ``measured_on`` is set)

THE PRIMARY GATED SIGNAL IS DDL PER TEST: an exact count (31881 DDL statements on macOS and on Linux
for the same tests), the same on every host, and the lever that explains the writes. The ``*wal_bytes*``
metrics are WAL bytes the test clusters generated (``pg_current_wal_insert_lsn`` between the probe's
marks). WAL is a CHURN PROXY, not a disk meter: the clusters sit on tmpfs on Linux with headroom, so
that WAL goes to memory and does NOT reach the NVMe. It is also relative and a lower bound (``fsync =
off``, ``full_page_writes = off``, the Python substrate ``wal_level = minimal``), host-dependent (the same
tests wrote 1.8x more WAL on Linux than on macOS) and not comparable between Python and Java. NOT
MEASURED: the job workspace and checkout, ``target/``, ``.venv``, jar builds, journald, anything before
pytest starts, and the docker-run and e2e shell clusters. The labrun NVMe sampler is the source of
truth for bytes written to disk, with the device delta (``cgroup_io`` ``wbytes``, summed and printed by
``fan-in``, informational and never gated). WAL varies a little from run to run (autovacuum, hint bits), so every bytes ceiling
carries a tolerance band (``bytes_tolerance``, a fraction); DDL carries ``ddl_tolerance`` (default 0). A
metric may override its band with its own
``<metric>_tolerance`` (the migration's DDL per worker is exactly the changelog's, so it keeps 0 beside
a DDL-per-test ratio that moves a little with the test set; a commit that adds a Liquibase changeset
bumps it in the same commit, once the ratchet is armed).

A shard is not the run. CI splits the Python suite into shards of differing width and test count, so
a single shard's bytes per test and DDL per test flap with its composition (a 15% heavier mix moved one
past its ceiling with no code change), and one ceiling per shard lets a light shard regress freely. The
gate is therefore on the WHOLE run: ``fan-in`` sums the N shard reports and judges four metrics over the
sum, bytes per test and DDL per test (both for the tests' own time, over every test of every shard) and
the migration's bytes and DDL per booted worker (each worker boots and migrates its own cluster). None of
the four moves with how the work was split, how many xdist workers each shard ran (a pool shard runs
``-n 4``, a hosted one runs serially) or which workers never booted a substrate (it boots lazily), and a
test added at the suite's average cost leaves them where they were. The raw totals are printed beside
them. ``check`` on one shard report judges the same four against the same ceilings but is ADVISORY
(``--advisory``: it prints what it found and exits 0), because a shard's mix is not the run's.

``fan-in`` is ADVISORY too until the python ceilings have been measured on the platform that enforces them
(CI, Linux): ``[python_unit] measured_on`` in the ceiling file is the marker, and the wiring lint
(tests/scripts/test_ddl_ratchet_wiring.py) keeps ``--advisory`` on the CI fan-in exactly while the marker is
absent. To arm it: paste the first Linux fan-in line into nexus-ekp7w, set the four ceilings from it, add
``measured_on = "<ci run url or sha>"``, drop ``--advisory`` and the three fan-in ``continue-on-error`` lines
from ci.yml, and the ``continue-on-error`` on the two per-shard ``test`` steps (advisory check, report
upload), all in one commit. Java is not
sharded: its totals are near width-invariant but the migration is paid once per fork, so its metrics are
per fork and the rest (``other_*``: every clone, every dedicated container, the tests' own work) is a
total.

A Java directory can hold files from several runs and from both suites (``service/target`` outlives
a run). Each fork file names its suite (``-Dnexus.ddl.suite``, set by the pom and CI) and its run
(``-Dnexus.ddl.run``, the Maven build timestamp unless CI names it): ``check`` reads only the requested
suite and refuses to guess between several runs of it (pass ``--run``; CI names the run and passes the
same id to both, so a persistent workspace cannot mix runs).

Exit codes, distinct from argparse's 2 and from a crash's 1: 0 within every ceiling; 10 a metric above
its ceiling; 11 a ceiling is still ``"measure"`` (the measured numbers are printed, ready to
commit); 12 the run is not comparable or proves nothing (no report, a worker or fork that could not
read its log or marks, a worker that died before it reported, no substrate booted, a metric that
measured 0 on a booted cluster, several runs mixed, a shard count that is not the expected one). An
unreadable or empty measurement is a failed gate, never a pass (the nexus-moht0 vacuous-gate doctrine).
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
DEFAULT_CEILINGS = Path(__file__).resolve().parents[1] / "tests" / "ddl_ceiling.toml"
MEASURE = "measure"

#: Intensive metrics a ``check`` compares, per suite. The python four are judged on one shard by
#: ``check`` (advisory) and on the whole run by ``fan-in`` (the gate).
PYTHON_METRICS = (
    "wal_bytes_per_test", "ddl_per_test", "migration_wal_bytes_per_worker", "migration_ddl_per_worker",
)
JAVA_METRICS = ("migration_wal_bytes_per_fork", "migration_ddl_per_fork", "other_wal_bytes", "other_ddl")

EXIT_OK, EXIT_OVER, EXIT_UNSET, EXIT_NOT_COMPARABLE = 0, 10, 11, 12


@dataclass
class Measurement:
    metrics: dict[str, float]
    problems: list[str] = field(default_factory=list)
    extra: str = ""


def _is_bytes(metric: str) -> bool:
    return "wal_bytes" in metric


def _proves_nothing(metrics: dict[str, float], what: str) -> list[str]:
    """A measured 0 on a cluster that booted. A booted cluster migrated a schema, so a zero in
    DDL or WAL is a counter that saw nothing (a log that was never read, marks that never moved), and
    comparing it with a ceiling would read as 'below' and pass (nexus-mvr3t)."""
    zero = sorted(name for name, value in metrics.items() if value == 0)
    if not zero:
        return []
    return [f"{', '.join(zero)} measured 0 on {what}: a cluster that booted cannot have written nothing, so the counter saw nothing"]


# ── measuring ────────────────────────────────────────────────────────────────


def python_problems(data: dict, *, part_of_run: bool = False) -> list[str]:
    """Why one merged python report does not prove anything, or nothing.

    ``part_of_run``: the report is one shard of a ``fan-in``. A shard none of whose workers booted the
    substrate (it boots lazily, so a shard of tests that never touch it is legitimate) contributes no
    cluster; ``fan_in`` asks the run as a whole for one."""
    problems = []
    booted = int(data.get("workers_booted", 0))
    if not booted and not part_of_run:
        problems.append("no worker booted the substrate, so nothing was counted")
    if not data.get("tests_run"):
        problems.append("the run recorded no tests")
    if data.get("workers_lost"):
        problems.append(
            f"{data['workers_lost']} xdist worker(s) ended without handing over a report (killed before "
            "sessionfinish), so their DDL and WAL are not in these figures"
        )
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


def _python_metrics(data: dict) -> dict[str, float]:
    booted, tests_run = int(data["workers_booted"]), int(data["tests_run"])
    wal = data["wal"]
    buckets = data["buckets"]
    return {
        "wal_bytes_per_test": wal["tests_bytes"] / tests_run,
        "ddl_per_test": buckets["tests"]["total"] / tests_run,
        "migration_wal_bytes_per_worker": wal["migration_bytes"] / booted,
        "migration_ddl_per_worker": buckets["migration"]["total"] / booted,
    }


def _python_extra(data: dict) -> str:
    extra = f"tests={int(data.get('tests_run', 0))} workers_booted={int(data.get('workers_booted', 0))}"
    wal = data.get("wal") or {}
    if wal.get("migration_bytes") is not None and wal.get("tests_bytes") is not None:
        total_ddl = data.get("buckets", {}).get("migration", {}).get("total", 0) + data.get("buckets", {}).get("tests", {}).get("total", 0)
        extra += f" total_ddl={total_ddl} total_wal_bytes={wal['migration_bytes'] + wal['tests_bytes']}"
    if data.get("recovered_workers"):
        extra += f" recovered_workers={data['recovered_workers']}"
    if data.get("uncounted_cluster_start_sites") is not None:
        extra += f" uncounted_cluster_start_sites={data['uncounted_cluster_start_sites']} (static tree census)"
    io = data.get("cgroup_io")
    if io is not None:
        if io.get("wbytes_delta") is None:
            extra += " cgroup_io_wbytes=unavailable (no shard could read cgroup v2 io.stat; informational)"
        else:
            of = f" summed over {io['shards_reporting']} shard(s)" if "shards_reporting" in io else ""
            extra += (f" cgroup_io_wbytes={io['wbytes_delta']}{of} (block-device writes over the pytest window; "
                      "informational, never gated; the labrun NVMe sampler is the source of truth for disk writes)")
    return extra


def _python_measurement(data: dict, problems: list[str]) -> Measurement:
    metrics: dict[str, float] = {}
    if not problems:
        metrics = _python_metrics(data)
        problems = _proves_nothing(metrics, f"{int(data['workers_booted'])} booted worker(s)")
        if problems:
            metrics = {}
    return Measurement(metrics, problems, _python_extra(data))


def measure_python(report: Path) -> Measurement:
    data = json.loads(report.read_text())
    return _python_measurement(data, python_problems(data))


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
        problems = _proves_nothing(metrics, f"{shared} shared cluster(s)")
        if problems:
            metrics = {}
    extra = f"run={','.join(chosen) or '-'} forks={forks} shared_clusters={shared}"
    return Measurement(metrics, problems, extra)


def combine_shards(datas: list[dict]) -> dict:
    """The run as one python report: the shard reports' counts added. ``wal`` is the sum of the
    shards' phases, None as soon as a shard that booted a worker lacks one (``python_problems`` has
    already said so for that shard). ``cgroup_io`` is the sum of the shards' device writes, an
    informational figure that no ceiling reads."""
    def wal_sum(key: str) -> int | None:
        total = 0
        for d in datas:
            value = (d.get("wal") or {}).get(key)
            if value is None:
                if int(d.get("workers_booted", 0)):
                    return None
                continue  # a shard that booted nothing owes no WAL
            total += value
        return total

    def bucket_sum(name: str) -> dict:
        return {"total": sum(d.get("buckets", {}).get(name, {}).get("total", 0) for d in datas)}

    io_deltas = [d["cgroup_io"]["wbytes_delta"] for d in datas
                 if (d.get("cgroup_io") or {}).get("wbytes_delta") is not None]
    return {
        "cgroup_io": {"wbytes_delta": sum(io_deltas) if io_deltas else None, "shards_reporting": len(io_deltas)},
        "workers_booted": sum(int(d.get("workers_booted", 0)) for d in datas),
        "workers_ready_mark_seen": sum(int(d.get("workers_ready_mark_seen", 0)) for d in datas),
        "workers_lost": sum(int(d.get("workers_lost", 0)) for d in datas),
        "recovered_workers": sum(int(d.get("recovered_workers", 0)) for d in datas),
        "tests_run": sum(int(d.get("tests_run", 0)) for d in datas),
        "buckets": {"migration": bucket_sum("migration"), "tests": bucket_sum("tests")},
        "wal": {"migration_bytes": wal_sum("migration_bytes"), "tests_bytes": wal_sum("tests_bytes")},
    }


def fan_in(reports: list[Path], shards: int) -> Measurement:
    problems: list[str] = []
    if len(reports) != shards:
        problems.append(f"{len(reports)} shard report(s) given, {shards} expected: a total over a missing shard is not the run's")
    datas = [json.loads(path.read_text()) for path in reports]
    for path, data in zip(reports, datas, strict=True):
        for problem in python_problems(data, part_of_run=True):
            problems.append(f"{path.name}: {problem}")
    combined = combine_shards(datas)
    if not combined["workers_booted"]:
        problems.append("no worker of any shard booted the substrate, so nothing was counted")
    measurement = _python_measurement(combined, problems)
    measurement.extra += f" shards={len(reports)}"
    return measurement


# ── comparing ────────────────────────────────────────────────────────────────


def uncounted_clusters(path: Path, prefix: str = "") -> int | None:
    """The sum of the ``[uncounted]`` table of the cluster-start-site allowlist (a static census of call sites) (only the sites whose key
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
    """Whole numbers as integers and a large figure to the byte (never ``6.8262e+06``: the printed numbers
    are pasted into the ceiling file); a small fraction to six significant digits."""
    if abs(value) >= 1000 or float(value).is_integer():
        return f"{round(value)}"
    return f"{value:.6g}"


def compare(label: str, measured: Measurement, entry: dict, metric_names: tuple[str, ...], ceilings_name: str,
            section: str, ungated: frozenset[str] = frozenset(), ungated_why: str = "") -> tuple[int, str]:
    """``ungated`` metrics are compared and printed but never fail the run (``ungated_why`` says why)."""
    shown = " ".join(f"{k}={_fmt(v)}" for k, v in measured.metrics.items())
    head = f"ddl ratchet {label}: {shown} [{measured.extra}]"
    if measured.problems:
        return EXIT_NOT_COMPARABLE, head + "\n  not a valid measurement: " + "; ".join(measured.problems)
    bytes_tol, ddl_tol = entry.get("bytes_tolerance", 0.05), entry.get("ddl_tolerance", 0.0)
    if not _number(bytes_tol) or not _number(ddl_tol) or bytes_tol < 0 or ddl_tol < 0:
        return EXIT_NOT_COMPARABLE, head + f"\n  {section}: bytes_tolerance and ddl_tolerance must be non-negative numbers"
    for name in metric_names:
        own = entry.get(f"{name}_tolerance")
        if own is not None and (not _number(own) or own < 0):
            return EXIT_NOT_COMPARABLE, head + f"\n  {section}.{name}_tolerance must be a non-negative number, got {own!r}"
    unset: list[str] = []
    for name in metric_names:
        value = entry.get(name)
        if value is None:
            return EXIT_NOT_COMPARABLE, head + f"\n  {ceilings_name} [{section}] has no {name!r}"
        if value == MEASURE:
            unset.append(name)
        elif not _number(value) or value <= 0:
            return EXIT_NOT_COMPARABLE, head + f"\n  {section}.{name} must be a positive number or \"{MEASURE}\", got {value!r}"
    over: list[str] = []
    notes: list[str] = []
    for name in metric_names:
        if entry[name] == MEASURE:
            continue
        tol = entry.get(f"{name}_tolerance", bytes_tol if _is_bytes(name) else ddl_tol)
        limit = entry[name] * (1 + tol)
        value = measured.metrics[name]
        if value > limit and name in ungated:
            notes.append(f"{name} {_fmt(value)} is above the ceiling {_fmt(entry[name])}"
                         + (f" (+{tol:.0%} band = {_fmt(limit)})" if tol else "") + f"; NOT GATED: {ungated_why}")
        elif value > limit:
            over.append(f"{name} {_fmt(value)} is above the ceiling {_fmt(entry[name])}"
                        + (f" (+{tol:.0%} band = {_fmt(limit)})" if tol else ""))
        elif value < entry[name] * (1 - tol):
            notes.append(f"{name} {_fmt(value)} is below {_fmt(entry[name])}: lower the ceiling in the commit that lowered it")
    if over:
        hint = ". A change added writes or schema work to every run; remove it, or cut the same amount elsewhere."
        if any(o.startswith(("migration_ddl_per_worker ", "migration_ddl_per_fork ")) for o in over):
            hint += (" The migration's DDL count is the changelog's: a commit that adds a Liquibase changeset "
                     f"bumps the migration_ddl_* ceiling in {ceilings_name} in the same commit (raising a ceiling "
                     "needs the reason in that commit's message).")
        return EXIT_OVER, head + "\n  ABOVE: " + "; ".join(over) + hint
    if unset:
        snippet = "\n".join(f"  {n} = {_fmt(measured.metrics[n])}" for n in unset)
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
            measured.extra += f" | {count} uncounted cluster start sites (static tree census: the jOOQ code generator's build-time container)"
    names = PYTHON_METRICS if suite == "python_unit" else JAVA_METRICS
    return compare(suite, measured, entry, names, ceilings_path.name, suite)


#: Why a hosted fan-in does not gate the WAL metrics (nexus-ekp7w review): WAL per test is host-dependent and
#: the python ceilings were measured on the pool; the DDL metrics are exact counts and gate everywhere.
BYTES_ADVISORY_WHY = ("the shards ran on a hosted runner, and WAL bytes are host-dependent while the ceilings were "
                      "measured on the pool; the DDL metrics are still gated")


def check_fan_in(suite: str, reports: list[Path], shards: int, ceilings_path: Path = DEFAULT_CEILINGS,
                 bytes_advisory: bool = False) -> tuple[int, str]:
    if suite != "python_unit":
        return EXIT_NOT_COMPARABLE, f"fan-in sums python shard reports; {suite!r} has none"
    try:
        measured = fan_in(reports, shards)
        entry = load_ceilings(ceilings_path)[suite]
    except (OSError, ValueError, KeyError) as exc:
        return EXIT_NOT_COMPARABLE, f"ddl ratchet {suite} fan-in: cannot read the measurement or ceiling ({exc!r})"
    ungated = frozenset(n for n in PYTHON_METRICS if _is_bytes(n)) if bytes_advisory else frozenset()
    return compare(f"{suite} fan-in", measured, entry, PYTHON_METRICS, ceilings_path.name, suite,
                   ungated, BYTES_ADVISORY_WHY)


def measured_on(suite: str, path: Path = DEFAULT_CEILINGS) -> str | None:
    """The ``measured_on`` marker of a ceiling section (a CI run url or a sha: where the ceilings were
    measured on the enforcing platform), or None when absent, blank or unreadable."""
    try:
        value = load_ceilings(path).get(suite, {}).get("measured_on")
    except (OSError, ValueError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _fan_in_advisory_note(code: int, suite: str, ceilings_path: Path) -> str:
    marker = measured_on(suite, ceilings_path)
    note = f"\n  ADVISORY: this would exit {code}; not enforced here." if code != EXIT_OK else ""
    if marker is None:
        note += (f"\n  {suite}.measured_on is absent in {ceilings_path.name}: the ceilings are not yet measured on the enforcing "
                 "platform. To arm the gate, paste this line into nexus-ekp7w, set the four ceilings from it, add "
                 "measured_on = \"<ci run url or sha>\" and drop --advisory from ci.yml in the same commit.")
    return note


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="compare one run's (or one shard's) intensive numbers with their ceilings")
    c.add_argument("--suite", required=True, choices=SUITES)
    c.add_argument("--report", required=True, type=Path)
    c.add_argument("--run", help="java: the nexus.ddl.run id to read (default: the only run present)")
    c.add_argument("--ceilings", type=Path, default=DEFAULT_CEILINGS)
    c.add_argument("--advisory", action="store_true",
                   help="print the verdict and exit 0 whatever it is (a shard's mix is not the run's; a suite whose "
                        "ceilings are not set yet)")
    f = sub.add_parser("fan-in", help="sum N python shard reports and judge the whole run: the python gate")
    f.add_argument("--suite", required=True, choices=("python_unit",))
    f.add_argument("--report", required=True, type=Path, nargs="+")
    f.add_argument("--shards", required=True, type=int, help="how many shard reports the run has")
    f.add_argument("--ceilings", type=Path, default=DEFAULT_CEILINGS)
    f.add_argument("--advisory", action="store_true",
                   help="print the whole comparison and exit 0 whatever it is: the python ceilings are not yet "
                        "measured on the platform that enforces them (no [python_unit] measured_on)")
    f.add_argument("--bytes-advisory", action="store_true",
                   help="the shards ran on a hosted runner: report a WAL metric above its ceiling without failing "
                        "(WAL is host-dependent; the ceilings were measured on the pool); the DDL metrics still gate")
    args = parser.parse_args(argv)
    if args.advisory:
        # Advisory means exit 0 whatever happens, a malformed report included: a report of the
        # wrong shape must not red a CI step whose gate is not armed yet (nexus-ekp7w review).
        try:
            return _run(args)
        except Exception as exc:  # noqa: BLE001
            print(f"ADVISORY: the {args.command} step could not judge its input ({type(exc).__name__}: {exc}); "
                  "not enforced here.")
            return EXIT_OK
    return _run(args)


def _run(args: argparse.Namespace) -> int:
    if args.command == "check":
        code, message = check(args.suite, args.report, args.ceilings, args.run)
        if args.advisory and code != EXIT_OK:
            message += f"\n  ADVISORY: this would exit {code}; not enforced here."
            code = EXIT_OK
    else:
        code, message = check_fan_in(args.suite, args.report, args.shards, args.ceilings, args.bytes_advisory)
        if args.advisory:
            message += _fan_in_advisory_note(code, args.suite, args.ceilings)
            code = EXIT_OK
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
