#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine-bundle canary: run the substrate on a NEW engine tag's PG bundle before a release pins it.

    engine_bundle_canary.py --tag engine-service-v0.1.195 [--pinned-tag T] [--bundle DIR] [--pinned-bundle DIR] [--cache-dir DIR]

WHY. develop's CI runs the engine substrate on the PG bundle of ``REQUIRED_ENGINE_VERSION``, and that pin moves
only at a client release (``tests/test_engine_version.py::test_pinned_floor_is_current``). So a new engine
tag's bundle is first exercised by the RELEASE PR's CI. conexus 8.3.0 found out that way:
engine-service-v0.1.194 bundles pgvector 0.8.7, whose ``CREATE EXTENSION vector`` runs 147 more DDL statements
than 0.8.2, and the release PR went red on ``tests/ddl_ceiling.toml`` ``[python_unit]``
``migration_ddl_per_worker`` (2578 measured against 2431).

WHAT. Two measurements in one job, each a scoped substrate run (the one test the ceiling is measured with plus
the journey file as the engine-backed smoke subset) with ``NEXUS_PG_BIN`` at a bundle fetched through the
product's verified seam (``install_pg_bundle``: sha256 plus sigstore, then ``ensure_pg_bundle``):

* the PINNED bundle (``REQUIRED_ENGINE_VERSION``'s tag): its ``ddl: migration=N`` must equal the ceiling.
  When it does not, develop itself has drifted from its own ceiling, and that is a failure whatever the new
  tag does. It is the control that makes the second measurement mean something.
* the TAG's bundle: when its count differs from the pinned bundle's, the tag changes what a fresh migration
  costs. That is NOT a failure: the job exits 0 with a ``::warning::`` and a summary line naming the remedy
  (the client release that pins the tag moves the ceiling), so it can never recur as a red.

Each bundle's PostgreSQL and pgvector (``vector.control`` default_version) versions go in the summary.

EXIT CODES (the first that applies wins, and every problem is printed):
    0  both runs measured and passed, pinned count equals the ceiling (a tag delta is a warning)
    1  the pinned bundle's count differs from the ceiling (develop drifted)
    2  a run measured nothing: no ``ddl: migration=`` line, or no test passed
    3  a pytest run failed
    4  a bundle could not be provisioned, or the arguments are wrong
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

EXIT_OK, EXIT_MISMATCH, EXIT_NOTHING_MEASURED, EXIT_TESTS_FAILED, EXIT_NO_BUNDLE = 0, 1, 2, 3, 4

_DDL_LINE = re.compile(r"^ddl: migration=(\d+)\b", re.MULTILINE)
_PASSED = re.compile(r"\b(\d+) passed\b")
_TAG = re.compile(r"engine-service-v\d+\.\d+\.\d+")
_CONTROL_VERSION = re.compile(r"^\s*default_version\s*=\s*'([^']+)'", re.MULTILINE)


@dataclass(frozen=True)
class Ceiling:
    migration_ddl_per_worker: int
    tolerance: float  # a fraction of the ceiling; 0.0 means exact


@dataclass(frozen=True)
class Measurement:
    """What one scoped substrate run on one bundle produced."""

    tag: str
    measured: int | None  # the run's ``ddl: migration=N``; None when no line carried one
    passed: int
    rc: int
    postgres: str = "unknown"
    pgvector: str = "unknown"


@dataclass(frozen=True)
class Verdict:
    code: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    summary: tuple[str, ...] = ()


def parse_migration_ddl(output: str) -> int | None:
    """The N of the run's last ``ddl: migration=N`` line, or ``None`` when no line carries one."""
    found = _DDL_LINE.findall(output)
    return int(found[-1]) if found else None


def parse_passed(output: str) -> int:
    """The pytest summary's passed count (0 when there is no such summary)."""
    found = _PASSED.findall(output)
    return int(found[-1]) if found else 0


def parse_control_version(text: str) -> str | None:
    """``default_version`` of a PostgreSQL extension ``.control`` file, or ``None``."""
    found = _CONTROL_VERSION.search(text)
    return found.group(1) if found else None


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


def _describe(m: Measurement) -> str:
    return f"{m.tag}: postgres {m.postgres}, pgvector {m.pgvector}, ddl: migration={m.measured}, {m.passed} passed"


def evaluate(*, tag: Measurement, pinned: Measurement | None, ceiling: Ceiling) -> Verdict:
    """The verdict for a tag's measurement against the pinned bundle's and the ceiling. Pure: no I/O.

    *pinned* is ``None`` only when the tag under test IS the pinned tag (one run serves both roles).
    """
    runs = [tag] if pinned is None else [pinned, tag]
    problems: list[tuple[int, str]] = []
    for run in runs:
        if run.measured is None:
            problems.append((
                EXIT_NOTHING_MEASURED,
                f"canary measured nothing for {run.tag}: no 'ddl: migration=' line in the pytest output "
                "(the engine substrate did not boot, or the run was skipped). A run that measured nothing is a failure.",
            ))
        elif run.passed == 0:
            problems.append((EXIT_NOTHING_MEASURED, f"canary for {run.tag}: pytest reported no passed test"))
        if run.rc != 0:
            problems.append((
                EXIT_TESTS_FAILED,
                f"canary for {run.tag}: pytest exited {run.rc}; the engine-backed tests are red on this bundle",
            ))
    control = tag if pinned is None else pinned
    ceil = ceiling.migration_ddl_per_worker
    if control.measured is not None and not within_ceiling(control.measured, ceiling):
        problems.append((
            EXIT_MISMATCH,
            f"DEVELOP DRIFTED FROM ITS CEILING: the pinned bundle {control.tag} measured ddl: migration={control.measured}, "
            f"tests/ddl_ceiling.toml [python_unit] migration_ddl_per_worker={ceil} (tolerance {ceiling.tolerance}). "
            "This is not the new tag's doing: a changeset or a bundle change landed without its ceiling bump. "
            "Fix the ceiling (with the measured reason) on develop.",
        ))
    summary = [f"Ceiling migration_ddl_per_worker={ceil}."]
    summary += [f"- {_describe(run)}" for run in runs]
    if problems:
        return Verdict(min(code for code, _ in problems), tuple(text for _, text in problems), (), tuple(summary))

    warnings: list[str] = []
    if pinned is not None and tag.measured != pinned.measured:
        assert tag.measured is not None and pinned.measured is not None
        word = "raises" if tag.measured > pinned.measured else "lowers"
        line = (
            f"{tag.tag}'s bundle changes migration DDL from {pinned.measured} to {tag.measured} "
            f"(postgres {pinned.postgres} to {tag.postgres}, pgvector {pinned.pgvector} to {tag.pgvector}): "
            f"the client release that pins it {word} tests/ddl_ceiling.toml migration_ddl_per_worker to "
            f"{tag.measured} with this reason"
        )
        warnings.append(line)
        summary.append(f"**{line}.**")
    else:
        summary.append(f"{tag.tag}'s bundle leaves migration DDL at {tag.measured}: nothing to change in the ceiling.")
    return Verdict(EXIT_OK, (), tuple(warnings), tuple(summary))


def pytest_argv(python: str = sys.executable) -> list[str]:
    return [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", DDL_TEST, *SMOKE_TESTS]


def bundle_versions(bin_dir: Path) -> tuple[str, str]:
    """``(postgres version, pgvector version)`` of a bundle's ``bin/`` dir, ``"unknown"`` for one that cannot be read."""
    postgres = "unknown"
    try:
        out = subprocess.run(  # noqa: S603 — a binary from the bundle just fetched
            [str(bin_dir / "postgres"), "--version"], capture_output=True, text=True, timeout=30, check=False
        )
        if out.returncode == 0 and out.stdout.strip():
            postgres = out.stdout.strip().rsplit(" ", 1)[-1]
    except (OSError, subprocess.SubprocessError):
        pass
    candidates: list[Path] = []
    try:
        out = subprocess.run(  # noqa: S603
            [str(bin_dir / "pg_config"), "--sharedir"], capture_output=True, text=True, timeout=30, check=False
        )
        if out.returncode == 0 and out.stdout.strip():
            candidates.append(Path(out.stdout.strip()))
    except (OSError, subprocess.SubprocessError):
        pass
    prefix = bin_dir.parent
    candidates += [prefix / "share" / "postgresql", prefix / "share"]
    for base in candidates:
        control = base / "extension" / "vector.control"
        if control.is_file():
            return postgres, parse_control_version(control.read_text(encoding="utf-8")) or "unknown"
    return postgres, "unknown"


def provision_bundle(tag: str, cache_dir: Path) -> Path:
    """Fetch and extract *tag*'s PG bundle for this host through the product's verified seam.

    The same two calls tests/db/_service_fixture.py::_self_provision_pg_bundle makes for the pinned tag
    (sha256 plus sigstore in ``install_pg_bundle``, TUF retry included), pointed at *tag* and a cache
    directory that must be empty or absent: a directory holding another tag's bundle would be
    extracted over and measured as this one. Returns the bundle ``bin/`` dir; raises when there is none.
    """
    if cache_dir.exists() and any(cache_dir.iterdir()):
        raise RuntimeError(f"{cache_dir} is not empty: a bundle cache is keyed by one tag and is never reused")
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


def pinned_tag() -> str:
    """The engine tag develop's client pins (``REQUIRED_ENGINE_VERSION``), the substrate's own default."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from nexus.daemon.binary_install import PINNED_SERVICE_TAG  # noqa: PLC0415

    if not PINNED_SERVICE_TAG:
        raise RuntimeError("nexus.daemon.binary_install.PINNED_SERVICE_TAG is unset")
    return PINNED_SERVICE_TAG


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


def _measure(tag: str, pg_bin: Path) -> Measurement:
    postgres, pgvector = bundle_versions(pg_bin)
    _say(f"canary: measuring {tag}: bundle at {pg_bin}, postgres {postgres}, pgvector {pgvector}")
    with tempfile.TemporaryDirectory(prefix="engine-bundle-canary-ddl-") as report_dir:
        rc, output = run_pytest(pg_bin, Path(report_dir))
    return Measurement(tag, parse_migration_ddl(output), parse_passed(output), rc, postgres, pgvector)


def _report(verdict: Verdict) -> None:
    ci = bool(os.environ.get("GITHUB_ACTIONS"))
    for line in verdict.errors:
        _say(("::error::" if ci else "ERROR: ") + line, err=True)
    for line in verdict.warnings:
        _say(("::warning::" if ci else "WARNING: ") + line)
    if not verdict.errors and not verdict.warnings:
        _say("canary OK: " + " ".join(verdict.summary))
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("### Engine bundle canary\n\n" + "\n".join(verdict.summary) + "\n")
            for line in (*verdict.errors, *verdict.warnings):
                fh.write(f"\n{line}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True, help="engine tag under test, engine-service-vX.Y.Z")
    parser.add_argument("--pinned-tag", help="the tag develop pins (default: REQUIRED_ENGINE_VERSION's)")
    parser.add_argument("--bundle", type=Path, help="an already extracted PG bundle bin/ dir for --tag: skip the fetch")
    parser.add_argument("--pinned-bundle", type=Path, help="the same for the pinned tag")
    parser.add_argument(
        "--cache-dir", type=Path, help="parent of one subdirectory per fetched tag (default: a new temp dir)"
    )
    args = parser.parse_args(argv)

    try:
        pinned = args.pinned_tag or pinned_tag()
    except Exception as exc:  # noqa: BLE001 — a tree with no pin cannot be judged
        _say(f"canary: cannot read the pinned engine tag: {type(exc).__name__}: {exc}", err=True)
        return EXIT_NO_BUNDLE
    for name in (args.tag, pinned):
        if not _TAG.fullmatch(name):
            _say(f"canary: {name!r} is not an engine-service-vX.Y.Z tag", err=True)
            return EXIT_NO_BUNDLE
    ceiling = read_ceiling()
    cache_root = args.cache_dir or Path(tempfile.mkdtemp(prefix="engine-bundle-canary-"))

    def bundle_for(tag: str, given: Path | None) -> Path | None:
        if given is not None:
            if not given.is_dir():
                _say(f"canary: bundle {given} is not a directory", err=True)
                return None
            return given
        try:
            return provision_bundle(tag, cache_root / tag)
        except Exception as exc:  # noqa: BLE001 — any provisioning failure is this run's verdict, named once
            _say(f"canary: could not provision the PG bundle for {tag}: {type(exc).__name__}: {exc}", err=True)
            return None

    same = args.tag == pinned
    pinned_bin = bundle_for(pinned, args.bundle if same else args.pinned_bundle)
    tag_bin = pinned_bin if same else bundle_for(args.tag, args.bundle)
    if pinned_bin is None or tag_bin is None:
        return EXIT_NO_BUNDLE
    pinned_run = None if same else _measure(pinned, pinned_bin)
    tag_run = _measure(args.tag, tag_bin)
    verdict = evaluate(tag=tag_run, pinned=pinned_run, ceiling=ceiling)
    _report(verdict)
    return verdict.code


if __name__ == "__main__":
    sys.exit(main())
