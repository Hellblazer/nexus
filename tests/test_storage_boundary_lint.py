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


def _check(extra_files=None, allowlist_prefixes=None,
           construction_allowlist_prefixes=None):
    """Run the lint and return its result."""
    from nexus.storage_boundary_lint import scan_repo

    return scan_repo(
        repo_root=REPO_ROOT,
        allowlist_prefixes=allowlist_prefixes,
        extra_files=extra_files,
        construction_allowlist_prefixes=construction_allowlist_prefixes,
    )


# ---------------------------------------------------------------------------
# Baseline against current main
# ---------------------------------------------------------------------------


def test_lint_reports_zero_violations_after_p4_cutover():
    """RDR-120 P4 (nexus-2ngox) + P5.A (nexus-kvn44): after the T2
    cutover and the catalog collapse, the lint reports zero
    violations AND zero catalog-allowlist sites.

    Pre-P4 there were 16 direct opens outside ``db/`` and ``catalog/``;
    P4 migrated mcp_infra to T2Client and epsilon-allowed the
    operator/debug paths. P5.A.2 moved the catalog SQLite layer into
    ``nexus.db.t2.catalog``; P5.A.3 retired the remaining
    ``catalog/synthesizer.py`` site and the two replay-equality gate
    sites in ``commands/catalog.py``. Catalog-allowlist count is now
    explicitly **0** per RDR §Approach Phase 5
    ('``count == 0`` explicitly').
    """
    result = _check()
    assert result.total_violations == 0, (
        f"expected zero violations after P4 cutover; got: "
        f"{[(v.file, v.line, v.symbol) for v in result.violations]}"
    )
    # P5.A.3 explicit assertion (count == 0).
    assert result.catalog_allowlist_count == 0


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
    was 2 (``catalog_db.py`` + ``synthesizer.py``); P5.A.2 (nexus-2t7o5)
    moved the catalog SQLite layer into ``nexus.db.t2.catalog`` and
    dropped to 1; P5.A.3 (nexus-nbsng) retired
    ``catalog/synthesizer.py`` and asserts ``count == 0`` explicitly
    per RDR §Approach Phase 5.
    """
    result = _check()
    assert result.catalog_allowlist_count == 0


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
    assert matched[0].symbol in ("sqlite3.connect", "_sqlite3.connect")


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


# ---------------------------------------------------------------------------
# RDR-128 P0c (RF-5): T2Database construction detection + dual baseline
# ---------------------------------------------------------------------------
#
# The lint counts two baseline populations so the single-writer cure
# (P1/P3) is measurable from day one:
#   * population 1 — ``sqlite3.connect`` sites carrying a deliberate
#     ``# epsilon-allow:`` override (the raw-connect exceptions);
#   * population 2 — direct ``T2Database(...)`` syntactic constructions
#     outside the db/ + daemon/ construction-allowlist.
#
# RDR-128 RF-1 cited 20 / 53 from a *codebase grep* (RDR §RF-1, line
# "Verified ... codebase grep"). That grep over-counts: ``grep
# 'T2Database('`` substring-matches the 16 ``_T2Database(...)`` wrapper
# call sites in commands/taxonomy_cmd.py plus several doc-comments. The
# AST lint counts true syntactic constructions (the taxonomy_cmd wrapper
# body is one site, reused 16×), giving the authoritative baseline below.


def test_t2database_construction_flagged(tmp_path):
    """A direct ``T2Database(...)`` construction is counted (population 2)."""
    base = _check().t2database_constructions
    target = tmp_path / "ctor_offender.py"
    target.write_text(
        "from nexus.db.t2 import T2Database\n"
        "def bad():\n"
        "    return T2Database('/tmp/x.db')\n"
    )
    assert _check(extra_files=[target]).t2database_constructions == base + 1


def test_t2database_construction_aliased_import_flagged(tmp_path):
    """``from ... import T2Database as DB; DB(...)`` is resolved + counted."""
    base = _check().t2database_constructions
    target = tmp_path / "ctor_aliased.py"
    target.write_text(
        "from nexus.db.t2 import T2Database as DB\n"
        "def bad():\n"
        "    return DB('/tmp/x.db')\n"
    )
    assert _check(extra_files=[target]).t2database_constructions == base + 1


def test_t2database_attribute_form_flagged(tmp_path):
    """``module.T2Database(...)`` (attribute access) is also counted."""
    base = _check().t2database_constructions
    target = tmp_path / "ctor_attr.py"
    target.write_text(
        "import nexus.db.t2 as t2mod\n"
        "def bad():\n"
        "    return t2mod.T2Database('/tmp/x.db')\n"
    )
    assert _check(extra_files=[target]).t2database_constructions == base + 1


def test_epsilon_allow_connect_counted_as_population(tmp_path):
    """An epsilon-allow'd ``sqlite3.connect`` is counted (population 1),
    not silently dropped, and is NOT a hard violation."""
    base = _check()
    target = tmp_path / "eps_connect.py"
    target.write_text(
        "import sqlite3\n"
        "def ok():\n"
        "    return sqlite3.connect('/tmp/x.db')  # epsilon-allow: documented exception\n"
    )
    result = _check(extra_files=[target])
    assert result.epsilon_allow_connects == base.epsilon_allow_connects + 1
    # Still not a hard violation (the override suppresses that).
    assert [v for v in result.violations if v.file == str(target)] == []


def test_daemon_construction_is_allowlisted(tmp_path):
    """db/ AND daemon/ are construction-allowlisted (the daemon is the
    legitimate single writer). Removing daemon/ from the allowlist must
    reveal strictly more constructions."""
    default = _check().t2database_constructions
    db_only = _check(
        construction_allowlist_prefixes=("src/nexus/db/",)
    ).t2database_constructions
    assert db_only > default, (
        "daemon/ T2Database construction(s) should be excluded by default"
    )


def test_dual_population_baseline_locked():
    """Exact baseline lock (silent-corruption guard, RDR-128 P0c).

    These are the AST-authoritative counts on develop @ 5.0.4 + this P0
    change. Locked as ``== N`` (never ``>=``): a regression that adds a
    raw connect or a T2Database construction outside the daemon trips the
    assertion, forcing either a daemon route (P1/P3) or a deliberate
    baseline bump with justification.

    Divergence from RDR RF-1's grep figures (20 / 53) is expected and
    documented in this module's header — the AST count is authoritative.
    """
    result = _check()
    # 16 = 15 pre-existing raw-connect exceptions + the RDR-128 P0b
    # lock-acquirability probe (daemon.py), itself a deliberate raw connect.
    assert result.epsilon_allow_connects == 16, (
        f"raw-connect epsilon-allow baseline moved: {result.epsilon_allow_connects}"
    )
    assert result.t2database_constructions == 35, (
        f"T2Database construction baseline moved: {result.t2database_constructions}"
    )
