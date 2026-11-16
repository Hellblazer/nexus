#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine-bundle canary: run the substrate on a NEW engine tag's PG bundle before a release pins it.

    engine_bundle_canary.py --tag engine-service-v0.1.195 [--bundle <extracted bin dir>] [--cache-dir <dir>]

WHY. develop's CI runs the engine substrate on the PG bundle of ``REQUIRED_ENGINE_VERSION``, and that pin
moves only at a client release (``tests/test_engine_version.py::test_pinned_floor_is_current`` derives it
from the newest released CHANGELOG section). So a new engine tag's bundle is first exercised by the RELEASE
PR's CI. conexus 8.3.0 found out that way: engine-service-v0.1.194 bundles pgvector 0.8.7, whose
``CREATE EXTENSION vector`` runs 147 more DDL statements than 0.8.2, and the release PR went red on
``tests/ddl_ceiling.toml`` ``[python_unit] migration_ddl_per_worker`` (2578 measured against 2431).

WHAT. Given a tag, this fetches that tag's PG bundle through the product's own verified seam
(``install_pg_bundle``: sha256 plus sigstore, then ``ensure_pg_bundle``), runs the one scoped substrate test
the ceiling is measured with plus a small engine-backed smoke subset with ``NEXUS_PG_BIN`` pointing at that
bundle, reads the ``ddl: migration=N`` line of the run summary, and compares N with the committed ceiling
(tolerance as the ceiling file says; 0 today, because the migration's count is exact).

EXIT CODES (the first that applies wins, and every problem is printed):
    0  the migration DDL equals the ceiling and the tests passed
    1  N differs from the ceiling: the remedy is printed
    2  no ``ddl: migration=`` line (or no test passed): the run measured nothing, which is a failure
    3  the pytest run failed
    4  the bundle could not be provisioned
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CEILING_FILE = REPO_ROOT / "tests" / "ddl_ceiling.toml"

#: The scoped test the ceiling's own comments are measured with (tests/ddl_ceiling.toml, tests/AGENTS.md).
DDL_TEST = "tests/test_scenario_journeys.py::test_store_put_is_searchable_and_catalogued"

#: The smoke subset: the journey file boots the real engine on the bundle and walks store, index, search and
#: the catalog manifest join end to end. Serial, one worker, so ``migration=`` is the per-worker count.
SMOKE_TESTS: tuple[str, ...] = ("tests/test_scenario_journeys.py",)

REMEDY = "raise the ceiling with the measured reason in the release that bumps REQUIRED_ENGINE_VERSION"

EXIT_OK, EXIT_MISMATCH, EXIT_NOTHING_MEASURED, EXIT_TESTS_FAILED, EXIT_NO_BUNDLE = 0, 1, 2, 3, 4

_DDL_LINE = re.compile(r"^ddl: migration=(\d+)\b", re.MULTILINE)
_PASSED = re.compile(r"\b(\d+) passed\b")
_TAG = re.compile(r"^engine-service-v\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class Ceiling:
    migration_ddl_per_worker: int
    tolerance: float  # a fraction of the ceiling; 0.0 means exact


def parse_migration_ddl(output: str) -> int | None:
    """The N of the run's last ``ddl: migration=N`` line, or ``None`` when no line carries one."""
    found = _DDL_LINE.findall(output)
    return int(found[-1]) if found else None


def parse_passed(output: str) -> int:
    """The pytest summary's passed count (0 when there is no such summary)."""
    found = _PASSED.findall(output)
    return int(found[-1]) if found else 0


def read_ceiling(path: Path = CEILING_FILE) -> Ceiling:
    """``[python_unit] migration_ddl_per_worker`` and its tolerance. A missing or non-numeric key raises."""
    section = tomllib.loads(path.read_text(encoding="utf-8"))["python_unit"]
    value = section["migration_ddl_per_worker"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path}: [python_unit] migration_ddl_per_worker must be an integer, got {value!r}")
    tolerance = float(section.get("migration_ddl_per_worker_tolerance", 0.0))
    if tolerance < 0:
        raise ValueError(f"{path}: migration_ddl_per_worker_tolerance must not be negative, got {tolerance}")
    return Ceiling(value, tolerance)


def within_ceiling(measured: int, ceiling: Ceiling) -> bool:
    """True when *measured* is within the tolerance band of the ceiling, in either direction."""
    return abs(measured - ceiling.migration_ddl_per_worker) <= int(ceiling.migration_ddl_per_worker * ceiling.tolerance)


def evaluate(
    *, tag: str, measured: int | None, passed: int, pytest_rc: int, ceiling: Ceiling
) -> tuple[int, list[str]]:
    """The exit code and the lines to print. The pure core: no I/O, no clock."""
    problems: list[tuple[int, str]] = []
    if measured is None:
        problems.append((
            EXIT_NOTHING_MEASURED,
            f"canary measured nothing for {tag}: no 'ddl: migration=' line in the pytest output "
            "(the engine substrate did not boot, or the run was skipped). A run that measured nothing is a failure.",
        ))
    elif not within_ceiling(measured, ceiling):
        ceil = ceiling.migration_ddl_per_worker
        direction = "above" if measured > ceil else "below"
        action = REMEDY if measured > ceil else "lower the ceiling with the measured reason in the release that bumps REQUIRED_ENGINE_VERSION"
        problems.append((
            EXIT_MISMATCH,
            f"MIGRATION DDL MISMATCH for {tag}: measured ddl: migration={measured}, ceiling "
            f"migration_ddl_per_worker={ceil} in tests/ddl_ceiling.toml [python_unit] (tolerance "
            f"{ceiling.tolerance}); the measurement is {abs(measured - ceil)} {direction} the ceiling. "
            f"Remedy: {action}. (A new pgvector or PG in the bundle moves the count with no changeset.)",
        ))
    if measured is not None and passed == 0:
        problems.append((EXIT_NOTHING_MEASURED, f"canary for {tag}: pytest reported no passed test"))
    if pytest_rc != 0:
        problems.append((EXIT_TESTS_FAILED, f"canary for {tag}: pytest exited {pytest_rc}; the engine-backed tests are red on this bundle"))
    if not problems:
        return EXIT_OK, [
            f"canary OK for {tag}: ddl: migration={measured} equals the ceiling "
            f"{ceiling.migration_ddl_per_worker}; {passed} tests passed"
        ]
    return min(code for code, _ in problems), [text for _, text in problems]


def pytest_argv(python: str = sys.executable) -> list[str]:
    return [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", DDL_TEST, *SMOKE_TESTS]


def provision_bundle(tag: str, cache_dir: Path) -> Path:
    """Fetch and extract *tag*'s PG bundle for this host through the product's verified seam.

    The same two calls tests/db/_service_fixture.py::_self_provision_pg_bundle makes for the pinned tag
    (sha256 plus sigstore in ``install_pg_bundle``, TUF retry included), pointed at the tag under test and a
    cache directory of this run's own. Returns the bundle ``bin/`` dir; raises when there is none.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from nexus.db.pg_bundle import ensure_pg_bundle  # noqa: PLC0415 — deferred: --bundle runs need neither module
    from tests.db._service_fixture import _install_pg_bundle_with_tuf_retry  # noqa: PLC0415

    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "service").mkdir(exist_ok=True)
    _install_pg_bundle_with_tuf_retry(tag, cache_dir)
    bin_dir = ensure_pg_bundle(cache_dir, search_dirs=[cache_dir / "service"])
    if bin_dir is None or not Path(bin_dir).is_dir():
        raise RuntimeError(f"no extracted PG bundle under {cache_dir} after installing {tag}")
    return Path(bin_dir)


def run_pytest(pg_bin: Path, report_dir: Path) -> tuple[int, str]:
    """Run the canary tests serially with ``NEXUS_PG_BIN`` set, echoing the output as it arrives."""
    env = dict(os.environ)
    env["NEXUS_PG_BIN"] = str(pg_bin)
    env["NX_DDL_REPORT_DIR"] = str(report_dir)
    # An absent or stale jar fails loudly instead of skipping (tests/conftest.py t2_service_env).
    env["NX_T2_SUBSTRATE_EXPECTED"] = "1"
    env.pop("NX_TEST_T2_SUBSTRATE", None)
    chunks: list[str] = []
    proc = subprocess.Popen(  # noqa: S603 — fixed argv built above
        pytest_argv(), cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        chunks.append(line)
    return proc.wait(), "".join(chunks)


def _say(text: str, *, err: bool = False) -> None:
    """One line to stdout, or stderr (no print: the CLI output channel is the stream itself)."""
    (sys.stderr if err else sys.stdout).write(text + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True, help="engine tag under test, engine-service-vX.Y.Z")
    parser.add_argument("--bundle", type=Path, help="an already extracted PG bundle bin/ dir: skip the fetch")
    parser.add_argument("--cache-dir", type=Path, help="where to place and extract the fetched bundle (default: a new temp dir)")
    args = parser.parse_args(argv)

    if not _TAG.match(args.tag):
        _say(f"canary: {args.tag!r} is not an engine-service-vX.Y.Z tag", err=True)
        return EXIT_NO_BUNDLE
    ceiling = read_ceiling()
    if args.bundle is not None:
        pg_bin = args.bundle
        if not pg_bin.is_dir():
            _say(f"canary: --bundle {pg_bin} is not a directory", err=True)
            return EXIT_NO_BUNDLE
    else:
        cache_dir = args.cache_dir or Path(tempfile.mkdtemp(prefix="engine-bundle-canary-"))
        try:
            pg_bin = provision_bundle(args.tag, cache_dir)
        except Exception as exc:  # noqa: BLE001 — any provisioning failure is this run's verdict, named once
            _say(f"canary: could not provision the PG bundle for {args.tag}: {type(exc).__name__}: {exc}", err=True)
            return EXIT_NO_BUNDLE
    _say(f"canary: {args.tag} bundle at {pg_bin}; ceiling migration_ddl_per_worker={ceiling.migration_ddl_per_worker}")

    with tempfile.TemporaryDirectory(prefix="engine-bundle-canary-ddl-") as report_dir:
        rc, output = run_pytest(pg_bin, Path(report_dir))
    code, lines = evaluate(
        tag=args.tag, measured=parse_migration_ddl(output), passed=parse_passed(output), pytest_rc=rc, ceiling=ceiling
    )
    for line in lines:
        _say(("::error::" if code and os.environ.get("GITHUB_ACTIONS") else "") + line, err=bool(code))
    return code


if __name__ == "__main__":
    sys.exit(main())
