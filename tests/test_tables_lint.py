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

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import migrate_rdr_status_vocabulary  # scripts/ is on the test pythonpath (pyproject)
import pytest

from nexus.tables.check import BLOCKING_CODES, Finding, check_table
from nexus.tables.load import load_packaged_table, load_table

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


# --------------------------------------------------------------------------
# docs/rdr/AGENTS.md's lifecycle prose must equal the table's status domain
# exactly (RDR-201 P1.8, nexus-j9z30.8) -- docs/rdr/AGENTS.md:21-26 was a
# FOURTH copy of this vocabulary before this bead (draft/accepted/closed/
# superseded, missing deferred and abandoned) in the very document AGENTS.md
# sends readers to first for RDR lifecycle semantics. This assertion is what
# keeps it from drifting again.


def _agents_md_lifecycle_statuses() -> list[str]:
    """Extract the backtick-quoted status words from docs/rdr/AGENTS.md's
    ``## RDR lifecycle`` section's bullet list."""
    text = (REPO_ROOT / "docs" / "rdr" / "AGENTS.md").read_text(encoding="utf-8")
    m = re.search(r"\n## RDR lifecycle\n(.*?)\n## ", text, re.DOTALL)
    assert m, "docs/rdr/AGENTS.md has no '## RDR lifecycle' section"
    section = m.group(1)
    return re.findall(r"^- `([a-z][a-z-]*)`", section, re.MULTILINE)


def _docs_rdr_md_lifecycle_statuses() -> list[str]:
    """Extract the pipe-separated status words from docs/rdr.md's Full
    Template section's ``status:`` YAML line -- the literal template new
    RDR authors copy when running ``/conexus:rdr-create`` (RDR-201 P1.8
    fix round: this was a FIFTH vocabulary copy, carrying the retired
    ``implemented``/``reverted`` words and missing ``deferred``/``closed``,
    in the exact text new authors paste into a fresh RDR file)."""
    text = (REPO_ROOT / "docs" / "rdr.md").read_text(encoding="utf-8")
    m = re.search(r"^status:\s*(.+\|.+)$", text, re.MULTILINE)
    assert m, "docs/rdr.md has no pipe-separated 'status:' template line"
    return [w.strip() for w in m.group(1).split("|")]


def _assert_lifecycle_statuses_match_table(doc_statuses: list[str], source: str) -> None:
    table = load_packaged_table("rdr-lifecycle.toml")
    table_domain = set(table.dimensions["status"].domain)

    # Non-vacuity: a sweep over zero extracted statuses is a failure, not a
    # pass (nexus-moht0) -- proves the regex is actually matching the
    # source's vocabulary list, not silently finding nothing.
    assert doc_statuses, f"no statuses extracted from {source}"
    assert set(doc_statuses) == table_domain, (
        f"{source} lifecycle list {sorted(doc_statuses)} != "
        f"table domain {sorted(table_domain)}"
    )
    # No duplicates in the doc list either.
    assert len(doc_statuses) == len(set(doc_statuses)), doc_statuses


def test_agents_md_lifecycle_statuses_match_table_domain_exactly():
    _assert_lifecycle_statuses_match_table(
        _agents_md_lifecycle_statuses(), "docs/rdr/AGENTS.md"
    )


def test_docs_rdr_md_template_lifecycle_statuses_match_table_domain_exactly():
    _assert_lifecycle_statuses_match_table(
        _docs_rdr_md_lifecycle_statuses(), "docs/rdr.md"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))


@pytest.mark.lint
def test_migration_sweep_target_vocabulary_matches_table_domain_exactly():
    """scripts/migrate_rdr_status_vocabulary.py carries the target vocabulary
    as a literal (a one-shot migration cannot import the table it installs
    the domain FOR without a bootstrap circularity), so this pins it: the
    sweep's LIFECYCLE_STATUSES equals the packaged table's status domain
    (RDR-201 Phase 1 code review, T2 nexus/code-review-rdr-201-phase-1-2026-09-01)."""
    domain = frozenset(load_packaged_table("rdr-lifecycle.toml").dimensions["status"].domain)
    assert migrate_rdr_status_vocabulary.LIFECYCLE_STATUSES == domain
    assert domain, "vacuous: empty status domain"
