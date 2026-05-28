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


def test_t2database_construction_unannotated_is_violation(tmp_path):
    """RDR-128 P3: a direct un-annotated ``T2Database(...)`` outside the
    construction-allowlist is now a HARD VIOLATION.

    At P0c it was counted-only (baseline metric). P3 flips the lint to
    enforce: an un-annotated construction is a violation exactly like an
    un-annotated ``sqlite3.connect``. It must NOT inflate the documented
    ``t2database_constructions`` population (that population is the
    annotated survivors only)."""
    base = _check().t2database_constructions
    target = tmp_path / "ctor_offender.py"
    target.write_text(
        "from nexus.db.t2 import T2Database\n"
        "def bad():\n"
        "    return T2Database('/tmp/x.db')\n"
    )
    result = _check(extra_files=[target])
    matched = [
        v for v in result.violations
        if v.file == str(target) and v.symbol == "T2Database"
    ]
    assert len(matched) == 1
    assert matched[0].line == 3
    assert result.t2database_constructions == base


def test_t2database_construction_aliased_import_is_violation(tmp_path):
    """``from ... import T2Database as DB; DB(...)`` resolves to a violation."""
    target = tmp_path / "ctor_aliased.py"
    target.write_text(
        "from nexus.db.t2 import T2Database as DB\n"
        "def bad():\n"
        "    return DB('/tmp/x.db')\n"
    )
    result = _check(extra_files=[target])
    matched = [
        v for v in result.violations
        if v.file == str(target) and v.symbol == "T2Database"
    ]
    assert len(matched) == 1


def test_t2database_attribute_form_is_violation(tmp_path):
    """``module.T2Database(...)`` (attribute access) resolves to a violation."""
    target = tmp_path / "ctor_attr.py"
    target.write_text(
        "import nexus.db.t2 as t2mod\n"
        "def bad():\n"
        "    return t2mod.T2Database('/tmp/x.db')\n"
    )
    result = _check(extra_files=[target])
    matched = [
        v for v in result.violations
        if v.file == str(target) and v.symbol == "T2Database"
    ]
    assert len(matched) == 1


def test_t2database_construction_annotated_is_documented(tmp_path):
    """RDR-128 P3: a ``T2Database(...)`` carrying a valid ``# epsilon-allow:``
    is counted into the documented population (``t2database_constructions``)
    and is NOT a hard violation — the exact mirror of the ``sqlite3.connect``
    epsilon-allow treatment. This is how an irreducible direct construction
    (read-only diagnostic, bootstrap chicken-and-egg, by-design daemon
    fallback) declares its lock-discipline justification."""
    base = _check().t2database_constructions
    target = tmp_path / "ctor_documented.py"
    target.write_text(
        "from nexus.db.t2 import T2Database\n"
        "def ok():\n"
        "    return T2Database('/tmp/x.db')  "
        "# epsilon-allow: read-only diagnostic, no WAL writer contention\n"
    )
    result = _check(extra_files=[target])
    assert result.t2database_constructions == base + 1
    assert [v for v in result.violations if v.file == str(target)] == []


def test_t2database_construction_short_reason_is_violation(tmp_path):
    """An epsilon-allow with a too-short (<8 char) reason does NOT suppress
    the violation — same reason-length floor as the connect override."""
    target = tmp_path / "ctor_shortreason.py"
    target.write_text(
        "from nexus.db.t2 import T2Database\n"
        "def bad():\n"
        "    return T2Database('/tmp/x.db')  # epsilon-allow: x\n"
    )
    result = _check(extra_files=[target])
    assert [
        v for v in result.violations
        if v.file == str(target) and v.symbol == "T2Database"
    ]


# ───────────────────────────────────────────────────────────────────────
# RDR-120 P4.B (nexus-vyqah): T3Database construction detection.
#
# A consumer-side ``T3Database(local_mode=True, ...)`` without an injected
# ``_client`` opens its own ``chromadb.PersistentClient`` on the local
# on-disk store — the T3 analogue of the T2 multi-process WAL contention.
# The ``chromadb.PersistentClient`` call itself lives in the allowlisted
# ``db/t3.py``, so the BANLIST module-attr scan cannot catch it; the
# consumer-side ``T3Database(...)`` construction is the detectable boundary.
# Consumers must call ``make_t3()`` / ``make_t3_client()`` instead.
# ───────────────────────────────────────────────────────────────────────


def test_t3database_construction_unannotated_is_violation(tmp_path):
    """A direct un-annotated ``T3Database(...)`` outside the
    construction-allowlist is a HARD VIOLATION, exactly like T2Database."""
    target = tmp_path / "t3_ctor_offender.py"
    target.write_text(
        "from nexus.db.t3 import T3Database\n"
        "def bad():\n"
        "    return T3Database(local_mode=True, local_path='/tmp/x')\n"
    )
    result = _check(extra_files=[target])
    matched = [
        v for v in result.violations
        if v.file == str(target) and v.symbol == "T3Database"
    ]
    assert len(matched) == 1
    assert matched[0].line == 3


def test_t3database_construction_aliased_import_is_violation(tmp_path):
    """``from ... import T3Database as DB; DB(...)`` resolves to a violation."""
    target = tmp_path / "t3_ctor_aliased.py"
    target.write_text(
        "from nexus.db.t3 import T3Database as DB\n"
        "def bad():\n"
        "    return DB(local_mode=True, local_path='/tmp/x')\n"
    )
    result = _check(extra_files=[target])
    matched = [
        v for v in result.violations
        if v.file == str(target) and v.symbol == "T3Database"
    ]
    assert len(matched) == 1


def test_t3database_construction_annotated_is_documented(tmp_path):
    """A ``T3Database(...)`` carrying a valid ``# epsilon-allow:`` is counted
    into the documented population and is NOT a hard violation — the mirror
    of the T2Database / sqlite3.connect epsilon-allow treatment."""
    base = _check().t2database_constructions
    target = tmp_path / "t3_ctor_documented.py"
    target.write_text(
        "from nexus.db.t3 import T3Database\n"
        "def ok():\n"
        "    return T3Database(local_mode=True, local_path='/tmp/x')  "
        "# epsilon-allow: read-only diagnostic, no daemon to contend with\n"
    )
    result = _check(extra_files=[target])
    assert result.t2database_constructions == base + 1
    assert [v for v in result.violations if v.file == str(target)] == []


def test_t3database_is_in_banned_constructors():
    """RDR-120 P4.B: the T3 consumer boundary is enforced by listing
    ``T3Database`` alongside ``T2Database`` in BANNED_CONSTRUCTORS — not by
    a separate code path. Lock the membership so a future refactor cannot
    silently drop the T3 boundary."""
    from nexus.storage_boundary_lint import BANNED_CONSTRUCTORS
    assert "T3Database" in BANNED_CONSTRUCTORS
    assert "T2Database" in BANNED_CONSTRUCTORS


def test_doctor_fix_does_not_construct_t3database_directly():
    """RDR-120 P4.B (nexus-vyqah): ``nx doctor --fix`` HNSW tuning was the
    sole consumer-side direct ``T3Database(local_mode=True, ...)`` site. It
    now routes through ``make_t3()`` (daemon-backed in local mode). Assert
    the source carries no direct construction so a regression is caught at
    the file level, independent of the whole-repo scan."""
    import pathlib
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src" / "nexus" / "commands" / "doctor.py"
    )
    result = _check(extra_files=[src])
    direct = [
        v for v in result.violations
        if v.symbol == "T3Database" and "doctor.py" in v.file
    ]
    assert direct == [], f"doctor.py must not construct T3Database directly: {direct}"


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
    legitimate single writer). Under the RDR-128 P3 enforcement flip,
    removing daemon/ from the allowlist must reveal strictly more
    VIOLATIONS — the daemon's own (un-annotated) ``T2Database(...)``
    constructions become hard violations once unallowlisted."""
    default = _check().total_violations
    db_only = _check(
        construction_allowlist_prefixes=("src/nexus/db/",)
    ).total_violations
    assert db_only > default, (
        "daemon/ T2Database construction(s) should be exempt by default"
    )


def test_dual_population_baseline_locked():
    """Exact baseline lock (silent-corruption guard, RDR-128 P0c → P3 close).

    AST-authoritative counts. Locked as ``== N`` (never ``>=``): a
    regression that adds a raw connect or an un-annotated ``T2Database(...)``
    construction outside the daemon trips the assertion (the un-annotated
    case via ``total_violations``), forcing either a daemon route or a
    deliberate ``# epsilon-allow:`` exemption with justification.

    RDR-128 P3 (nexus-sbxbe.3) flipped the lint from counted-only to
    ENFORCING: ``t2database_constructions`` now counts the DOCUMENTED
    survivors (annotated direct constructions), and any un-annotated
    construction is a hard violation. The close drove un-annotated
    constructions to ZERO — every surviving direct construction carries a
    lock-discipline justification (the acceptance criterion's
    "documented-irreducible set").
    """
    result = _check()
    # 16 = 15 pre-existing raw-connect exceptions + the RDR-128 P0b
    # lock-acquirability probe (daemon.py). Unchanged by P3: the P3 routing
    # added no raw sqlite3.connect sites (the migration-flock extension in
    # upgrade.py uses fcntl.flock, not a connect), and the T2Database
    # constructions it removed were not raw connects.
    assert result.epsilon_allow_connects == 16, (
        f"raw-connect epsilon-allow baseline moved: {result.epsilon_allow_connects}"
    )
    # P3 endpoint: ZERO un-annotated direct T2Database constructions outside
    # db/ + daemon/. A new direct writer that lands without routing or an
    # epsilon-allow justification fails CI here — the enforcement teeth that
    # "close" the single-writer invariant.
    assert result.total_violations == 0, (
        "un-annotated T2Database construction(s) or raw connect(s) outside "
        f"the allowlist: {[(v.file, v.line, v.symbol) for v in result.violations]}"
    )
    # 30 = the documented-irreducible survivor set after the P3 close. P0c
    # baselined 36 (all constructions, counted-only). P3 routed the hot-path
    # and routable writers away (indexer/worker-poll/session-end/rename/
    # scratch-promote/doctor-metric/aspect-delete/collection-delete) and
    # annotated the genuinely-irreducible remainder with documented
    # justifications: the daemon-unreachable fallback + t2_ctx worker-persist
    # (mcp_infra), the bootstrap upgrade path, document_aspects.upsert /
    # raw-DDL / raw-cursor writers, the read-only diagnostic/CLI reads, and
    # the taxonomy CLI factory (raw-cursor reads + chroma-interleaved writes
    # that cannot cross the daemon RPC). Each survivor's ``# epsilon-allow:``
    # reason states why it cannot route.
    assert result.t2database_constructions == 31, (
        f"T2Database documented-construction baseline moved: {result.t2database_constructions}"
    )
