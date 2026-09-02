"""Lint bucket for RDR-201 closed-vocabulary tables (nexus-j9z30.3).

Runs :func:`nexus.tables.check.check_table` over every ``*.toml`` file
under BOTH ``src/nexus/tables/`` (the packaged tables) and
``docs/tables/`` (repo-only tables, e.g. the later release-choreography
table), asserting zero BLOCKING findings per table. This is the "-m lint"
leg (see ``tests/AGENTS.md``): O(repo) meta-tests, excluded from default
``addopts``, run in the dedicated CI lint job.

Non-vacuity (the nexus-moht0 vacuous-gate doctrine): a fixture copy of the
lifecycle table with a planted second ``accept`` row must be reported as
``overlap``, and a fixture copy with a deleted row must be reported as
``coverage-gap`` -- proving the sweep's assertion is live detection code,
not an accidentally-passing "findings is empty" tautology. A sweep that
found nothing to check (zero tables discovered) is a failure, not a pass.

Packaging (RDR-201 P1.3's TABLE LOCATION note): the lifecycle table ships
INSIDE THE PACKAGE at ``src/nexus/tables/rdr-lifecycle.toml`` because only
``src/nexus`` reaches a wheel (``pyproject.toml``'s
``[tool.hatch.build.targets.wheel]`` ``packages = ["src/nexus"]``) and
``nx rdr set-status`` is a top-level CLI that must resolve the table from
any installed conexus, not just this checkout. A test that only reads the
table off the source tree cannot see a packaging regression (hatchling
silently dropping the ``.toml`` from the wheel); only building a real
wheel and inspecting its contents can, so this module builds one.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from nexus.tables.check import BLOCKING_CODES, Finding, check_table
from nexus.tables.load import load_table

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
TABLE_DIRS = (
    REPO_ROOT / "src" / "nexus" / "tables",
    REPO_ROOT / "docs" / "tables",
)
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "tables"


def _blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.code in BLOCKING_CODES]


def _discover_tables() -> list[Path]:
    paths: list[Path] = []
    for table_dir in TABLE_DIRS:
        if table_dir.is_dir():
            paths.extend(sorted(table_dir.glob("*.toml")))
    return paths


# --------------------------------------------------------------------------
# Every shipped table lints clean


def test_every_shipped_table_lints_clean():
    tables = _discover_tables()
    # Non-vacuity floor: a sweep over zero tables is a failure, not a pass
    # (nexus-moht0). At minimum src/nexus/tables/rdr-lifecycle.toml must
    # be discovered.
    assert tables, (
        "no *.toml tables found under src/nexus/tables/ or docs/tables/ "
        "-- the sweep examined nothing"
    )
    for path in tables:
        table = load_table(path)
        findings = check_table(table)
        blocking = _blocking(findings)
        assert blocking == [], (
            f"{path.relative_to(REPO_ROOT)}: blocking findings "
            f"{[f.to_json() for f in blocking]}"
        )


def test_lifecycle_table_is_discovered_by_the_sweep():
    """Pins the non-vacuity floor to the specific table this bead ships,
    not just "the glob found something" (a stray unrelated *.toml under
    either directory would otherwise satisfy the assert above alone)."""
    tables = {p.name for p in _discover_tables()}
    assert "rdr-lifecycle.toml" in tables


# --------------------------------------------------------------------------
# Non-vacuity: planted defects are caught, not silently passed


def test_planted_second_accept_row_is_reported_as_overlap():
    table = load_table(FIXTURES / "rdr_lifecycle_overlap.toml")
    findings = check_table(table)
    codes = sorted(f.code for f in findings)
    assert "overlap" in codes, f"expected an overlap finding, got {codes}"


def test_planted_deleted_row_is_reported_as_coverage_gap():
    table = load_table(FIXTURES / "rdr_lifecycle_gap.toml")
    findings = check_table(table)
    codes = sorted(f.code for f in findings)
    assert "coverage-gap" in codes, f"expected a coverage-gap finding, got {codes}"


# --------------------------------------------------------------------------
# Packaging: the lifecycle table must reach a BUILT wheel, not just the
# source tree


def test_lifecycle_table_present_in_built_wheel(tmp_path: Path):
    dist_dir = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"uv build --wheel failed (rc={result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    wheels = sorted(dist_dir.glob("*.whl"))
    assert wheels, f"uv build --wheel produced no .whl in {dist_dir}"

    with zipfile.ZipFile(wheels[-1]) as zf:
        names = set(zf.namelist())

    assert "nexus/tables/rdr-lifecycle.toml" in names, (
        "src/nexus/tables/rdr-lifecycle.toml is missing from the built wheel -- "
        "this is the packaging defect nexus-j9z30.3's TABLE LOCATION note "
        "exists to prevent; a docs-only table would 404 on every installed "
        "conexus. Wheel contents (nexus/tables/*): "
        f"{sorted(n for n in names if n.startswith('nexus/tables/'))}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
