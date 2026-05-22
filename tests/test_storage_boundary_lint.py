# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P0.A: storage-boundary lint.

AST-scan for direct storage opens outside the allowed daemon-internal
prefix. Banlist:

- ``sqlite3.connect(...)`` (and module-aliased forms such as
  ``import sqlite3 as _sqlite3; _sqlite3.connect(...)``)
- ``chromadb.PersistentClient(...)``
- ``chromadb.CloudClient(...)``
- ``chromadb.EphemeralClient(...)``

Allowed prefixes:

- ``src/nexus/db/`` — the daemon-internal substrate (always allowed)
- ``src/nexus/catalog/`` — P0-P4 phase-allowlist (removed at P5)
- per-line ``# epsilon-allow: <reason>`` override (>= 8 char reason)
"""
from __future__ import annotations

import pathlib

import pytest


REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"


def _check(extra_files=None, allowlist_prefixes=None):
    """Run the lint and return its result tuple."""
    from nexus.storage_boundary_lint import scan_repo

    return scan_repo(
        repo_root=REPO_ROOT,
        allowlist_prefixes=allowlist_prefixes,
        extra_files=extra_files,
    )


# ---------------------------------------------------------------------------
# Baseline against current main
# ---------------------------------------------------------------------------


def test_lint_reports_zero_violations_after_p4_cutover():
    """RDR-120 P4 (nexus-2ngox): after the T2 cutover, the lint reports
    zero violations. Pre-P4 there were 16 direct opens outside ``db/``
    and ``catalog/`` (baseline captured by P0); P4 migrated mcp_infra
    to T2Client and epsilon-allowed the remaining operator/debug
    paths with documented reasons.

    P5.A.2 (nexus-2t7o5) moved the catalog SQLite layer into T2 and
    dropped the catalog-allowlist count from 2 to 1; P5.A.3 retires
    the last site (synthesizer.py) and asserts ``== 0``.
    """
    result = _check()
    assert result.total_violations == 0, (
        f"expected zero violations after P4 cutover; got: "
        f"{[(v.file, v.line, v.symbol) for v in result.violations]}"
    )
    # Catalog substrate is partially migrated; P5.A.3 takes this to 0.
    assert result.catalog_allowlist_count == 1


def test_db_directory_is_allowlisted_by_default():
    """src/nexus/db/ is the daemon-internal substrate; not violations."""
    result = _check()
    for v in result.violations:
        assert "src/nexus/db/" not in v.file, (
            f"db/ should be allowlisted, got violation at {v.file}:{v.line}"
        )


def test_catalog_directory_is_allowlisted_p0_p4():
    """P0-P4: src/nexus/catalog/ allowed; removed at P5 in a follow-on."""
    result = _check()
    for v in result.violations:
        assert "src/nexus/catalog/" not in v.file, (
            f"catalog/ is P0-P4 allowlisted, got violation at {v.file}:{v.line}"
        )


def test_catalog_allowlist_count_metric():
    """The lint reports the count of catalog-allowlist call sites.

    Per the phase-boundary forcing function (RDR-120 §Approach), this
    metric is monotonically non-increasing across phases. P0 baseline
    was 2 (``catalog_db.py`` + ``synthesizer.py``). RDR-120 P5.A.2
    (nexus-2t7o5) moved the catalog SQLite layer into
    ``nexus.db.t2.catalog`` and reduced the count to 1 (``synthesizer.py``
    only). P5.A.3 will retire that last site and assert ``== 0``.
    """
    result = _check()
    assert result.catalog_allowlist_count == 1


# ---------------------------------------------------------------------------
# Synthetic offender + epsilon-allow escape
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_offender(tmp_path):
    """Write a file outside any allowlist with a forbidden call."""
    target = tmp_path / "synthetic_offender.py"
    target.write_text(
        "import sqlite3\n"
        "def bad():\n"
        "    return sqlite3.connect('/tmp/x.db')\n"
    )
    return target


def test_synthetic_offender_caught(synthetic_offender):
    result = _check(extra_files=[synthetic_offender])
    matched = [v for v in result.violations if v.file == str(synthetic_offender)]
    assert len(matched) == 1
    assert matched[0].symbol == "sqlite3.connect"
    assert matched[0].line == 3


def test_epsilon_allow_per_line_override(tmp_path):
    """A line tagged `# epsilon-allow: <reason>` (>= 8 chars) is skipped."""
    target = tmp_path / "allowed.py"
    target.write_text(
        "import sqlite3\n"
        "def ok():\n"
        "    return sqlite3.connect('/tmp/x.db')  # epsilon-allow: test fixture only\n"
    )
    result = _check(extra_files=[target])
    matched = [v for v in result.violations if v.file == str(target)]
    assert not matched


def test_epsilon_allow_with_short_reason_does_not_override(tmp_path):
    target = tmp_path / "shortallow.py"
    target.write_text(
        "import sqlite3\n"
        "def bad():\n"
        "    return sqlite3.connect('/tmp/x.db')  # epsilon-allow: x\n"
    )
    result = _check(extra_files=[target])
    matched = [v for v in result.violations if v.file == str(target)]
    assert len(matched) == 1


# ---------------------------------------------------------------------------
# Module-aliased imports (the alias-evasion trap from A5)
# ---------------------------------------------------------------------------


def test_aliased_sqlite_import_caught(tmp_path):
    """`import sqlite3 as _sqlite3; _sqlite3.connect(...)` is caught."""
    target = tmp_path / "aliased.py"
    target.write_text(
        "import sqlite3 as _sqlite3\n"
        "def bad():\n"
        "    return _sqlite3.connect('/tmp/x.db')\n"
    )
    result = _check(extra_files=[target])
    matched = [v for v in result.violations if v.file == str(target)]
    assert len(matched) == 1
    # The alias is resolved back to the canonical module name.
    assert "sqlite3.connect" in matched[0].symbol or "_sqlite3.connect" in matched[0].symbol


# ---------------------------------------------------------------------------
# All three chromadb client classes (not just PersistentClient)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_class", [
    "PersistentClient",
    "CloudClient",
    "EphemeralClient",
])
def test_all_chromadb_classes_in_banlist(client_class, tmp_path):
    """The lint covers all three chromadb client classes."""
    target = tmp_path / f"chromabad_{client_class.lower()}.py"
    target.write_text(
        "import chromadb\n"
        "def bad():\n"
        f"    return chromadb.{client_class}()\n"
    )
    result = _check(extra_files=[target])
    matched = [v for v in result.violations if v.file == str(target)]
    assert len(matched) == 1, f"missed {client_class}"
    assert client_class in matched[0].symbol


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_result_has_file_line_symbol(tmp_path):
    target = tmp_path / "shape.py"
    target.write_text(
        "import sqlite3\ndef f(): sqlite3.connect('/x')\n"
    )
    result = _check(extra_files=[target])
    matched = [v for v in result.violations if v.file == str(target)]
    assert len(matched) == 1
    v = matched[0]
    assert isinstance(v.file, str)
    assert isinstance(v.line, int)
    assert isinstance(v.symbol, str)
    assert v.line == 2
