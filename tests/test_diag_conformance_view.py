# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 Amendment A6 (nexus-9bufb): the structural content boundary.

The nexus_diag role's content boundary moves from lint-only (the
diag_connection choke point) to counts-by-construction: a superuser-owned
view over the chash-bearing tables, with direct table SELECT revoked by the
view-era grants changeset. These tests pin every coupling the design leans
on so no surface can drift from the others.

nexus-z5j0t extends the authoritative set with column-name-aware entries:
poison tables (width-CHECK-bearing, upgrade-gating) vs legacy-debt tables
(CHECK-less soft references, observed-only). The gate statements stay
poison-only so deployed 5-leg views keep satisfying the gate unchanged.
"""
from __future__ import annotations

import re
from pathlib import Path

from nexus.db.chash_tables import (
    CHASH_BEARING_TABLES,
    DEBT_CHASH_TABLES,
    DIAG_CONFORMANCE_VIEW,
    POISON_CHASH_TABLES,
    chash_conformance_statements,
    debt_chash_conformance_statements,
    diag_conformance_view_ddl,
    legacy_chash_conformance_statements,
)
from nexus.remediation.sql_lint import assert_read_only_diagnostics

_REPO = Path(__file__).resolve().parents[1]


def test_the_authoritative_set_is_column_aware_and_complete():
    """nexus-z5j0t: the RDR-185 .13 audit gaps are IN the set, with their
    real column names (the chunk_id-naming blind spot), and the poison
    subset is exactly the four survivors (the gate must not grow;
    nexus.chash_index retired by RDR-187 / nexus-piwya.5 ahead of the
    table DROP)."""
    by_table = {t.table: t for t in CHASH_BEARING_TABLES}
    assert by_table["nexus.topic_assignments"].column == "doc_id"
    assert by_table["nexus.frecency"].column == "chunk_id"
    assert by_table["nexus.relevance_log"].column == "chunk_id"
    assert not by_table["nexus.topic_assignments"].poison
    assert not by_table["nexus.frecency"].poison
    assert not by_table["nexus.relevance_log"].poison
    assert tuple(t.table for t in POISON_CHASH_TABLES) == (
        "nexus.chunks_384",
        "nexus.chunks_768",
        "nexus.chunks_1024",
        "nexus.catalog_document_chunks",
    )
    # RDR-187 pin: the retired router must not reappear in the registry.
    assert "nexus.chash_index" not in {t.table for t in CHASH_BEARING_TABLES}
    assert all(t.column == "chash" for t in POISON_CHASH_TABLES)
    assert set(CHASH_BEARING_TABLES) == set(POISON_CHASH_TABLES) | set(DEBT_CHASH_TABLES)


def test_view_ddl_covers_exactly_the_chash_tables():
    ddl = diag_conformance_view_ddl()
    for t in POISON_CHASH_TABLES:
        assert f"'{t.table}' AS table_name" in ddl
        assert f"FROM {t.table} WHERE octet_length({t.column}) <> 32" in ddl
    # Debt legs are SEMANTIC anti-joins (RDR-180 .6 amendment 1): hex-shaped
    # references that miss every chunk-table join; titles/non-hex identities
    # are excluded by the hex guard (not chash debt).
    for t in DEBT_CHASH_TABLES:
        assert f"'{t.table}' AS table_name" in ddl
        assert f"t.{t.column} ~ '^[0-9a-f]+$'" in ddl
    assert ddl.count("NOT EXISTS") == 3 * len(DEBT_CHASH_TABLES)
    # One UNION arm per table, no extras.
    assert ddl.count("UNION ALL") == len(CHASH_BEARING_TABLES) - 1


def test_predicate_is_era_safe_octet_length_never_length():
    """RDR-180 Item6a (nexus-jxizy.5): octet_length accepts exactly the
    era-canonical form in each era (32-hex TEXT today == 32 octets; 32-byte
    BYTEA post-flip == 32 octets), so ONE spelling survives the cutover.
    Bare length() (chars on text, bytes on bytea) must never come back —
    it is the 32-vs-64 units ambiguity this RDR exists to kill."""
    for stmt in legacy_chash_conformance_statements():
        assert "octet_length(" in stmt
        assert not re.search(r"(?<!octet_)length\(", stmt), stmt
    # The view: poison legs are octet_length; debt legs use length() only in
    # the even-hex guard (chars of a hex TEXT column — deliberate).
    ddl = diag_conformance_view_ddl()
    assert "octet_length(" in ddl


def test_gate_statements_are_poison_only_against_the_view():
    """The install-binary gate's statements must be invariant across view
    generations: poison-only, so a deployed 5-leg view still answers every
    one of them (a debt-table statement against that view would NULL out)."""
    stmts = chash_conformance_statements()
    assert len(stmts) == len(POISON_CHASH_TABLES)
    for stmt, t in zip(stmts, POISON_CHASH_TABLES):
        assert DIAG_CONFORMANCE_VIEW in stmt
        assert f"table_name = '{t.table}'" in stmt
    for t in DEBT_CHASH_TABLES:
        assert all(t.table not in s for s in stmts)


def test_debt_statements_cover_the_debt_tables_against_the_view():
    stmts = debt_chash_conformance_statements()
    assert len(stmts) == len(DEBT_CHASH_TABLES)
    for stmt, t in zip(stmts, DEBT_CHASH_TABLES):
        assert DIAG_CONFORMANCE_VIEW in stmt
        assert f"table_name = '{t.table}'" in stmt


def test_legacy_statements_are_poison_only_direct_counts():
    """Pre-A6 engines predate the telemetry-001 debt tables — a direct debt
    count there would fail on a missing relation and poison the fallback."""
    stmts = legacy_chash_conformance_statements()
    assert len(stmts) == len(POISON_CHASH_TABLES)
    for stmt, t in zip(stmts, POISON_CHASH_TABLES):
        assert f"FROM {t.table} WHERE octet_length({t.column}) <> 32" in stmt


def test_view_statements_pass_the_diagnostic_lint():
    """The whole point: the emitted shape must clear the fail-closed
    aggregate-only lint (nexus.* target => aggregate select list)."""
    assert_read_only_diagnostics(chash_conformance_statements())
    assert_read_only_diagnostics(debt_chash_conformance_statements())
    assert_read_only_diagnostics(legacy_chash_conformance_statements())


def test_provision_embeds_the_generator_not_a_copy():
    """Review 47dcb65e: ONE helper, called from BOTH provisioning paths
    (_create_roles and _backfill_diag_role) - a hand-typed copy in either
    would drift from CHASH_BEARING_TABLES."""
    src = (_REPO / "src/nexus/db/pg_provision.py").read_text()
    assert src.count("def _provision_diag_conformance_view") == 1
    # THREE call sites: _create_roles, _backfill_diag_role, and the RDR-180
    # post-rekey re-provision helper (rdr180-001 drops the view; the rung
    # recreates it).
    assert src.count("_provision_diag_conformance_view(bins, port, os_user)") == 3
    assert "diag_conformance_view_ddl" in src
    # The existence guard derives from the constant, never hand-typed, and
    # requires EVERY chash table (the view references all of them).
    assert "for t in CHASH_BEARING_TABLES" in src
    assert ") = {len(CHASH_BEARING_TABLES)} THEN " in src


def test_docs_rendered_copy_matches_the_generator():
    """docs/configuration.md carries a rendered copy for BYO-Postgres DBAs —
    pin it to the generator so a table-set change regenerates the docs."""
    doc = (_REPO / "docs/configuration.md").read_text()
    ddl = diag_conformance_view_ddl()
    # normalize the doc's 3-space continuation indent
    doc_flat = re.sub(r"\n   ", "\n", doc)
    assert ddl in doc_flat, (
        "docs/configuration.md's Amendment-A6 view SQL drifted from "
        "nexus.db.chash_tables.diag_conformance_view_ddl() - regenerate the "
        "docs block"
    )


def test_cascade_covers_every_debt_table():
    """nexus-z5j0t's completeness link: every legacy-debt entry must have a
    remap-cascade implementation (same table + same column), so the set that
    OBSERVES debt and the machinery that CONVERGES it cannot drift."""
    from nexus.migration.remap_cascade import _STORE_COLUMNS

    cascade = {
        (f"nexus.{table}", column)
        for (_db, table, column) in _STORE_COLUMNS.values()
    }
    for t in DEBT_CHASH_TABLES:
        assert (t.table, t.column) in cascade, (
            f"{t.table}.{t.column} is observed as chash legacy debt but has "
            "no remap-cascade implementation - extend CASCADE_STORES"
        )


def test_poison_detail_token_couples_probe_and_gates():
    """The install-binary gate (daemon.py) and the convergence gate
    (upgrade_finish.py) distinguish REAL poison from probe-degraded WARNs
    by substring-matching the health detail. All three sides must use the
    ONE constant — a hand-typed phrase on any side silently disarms the
    gate (nexus-jxizy.5)."""
    from nexus.db.chash_tables import POISON_DETAIL_TOKEN

    for rel in (
        "src/nexus/health.py",
        "src/nexus/commands/daemon.py",
        "src/nexus/upgrade_finish.py",
    ):
        src = (_REPO / rel).read_text()
        assert "POISON_DETAIL_TOKEN" in src, rel
        assert '"non-32-char chash" in r.detail' not in src, rel
    assert POISON_DETAIL_TOKEN  # non-empty, importable


def test_grants_changeset_view_era_revokes_tables():
    """The view-era changeset must exist, be view-conditional, and revoke the
    direct table SELECT that the legacy era granted — PER-RELATION and
    OWNER-RESTRICTED (nexus-46yy3, live-reproduced P0: the bulk
    ALL-TABLES-IN-SCHEMA form hard-errors on the superuser-owned view from
    the NOSUPERUSER nexus_admin migration connection, crash-looping every
    boot once the view exists). The changeset must NOT grant the view either
    — only the view's owner (the superuser provisioning path) can."""
    xml = (_REPO / "service/src/main/resources/db/changelog/grants-nexus-diag.xml").read_text()
    assert "grants-nexus-diag-2" in xml
    assert xml.count("diag_chash_conformance") == 5  # 2x sqlCheck + 3 prose mentions
    # The P0 shape: per-relation loop, restricted to relations this role owns.
    assert "pg_get_userbyid(c.relowner) = current_user" in xml
    assert "REVOKE SELECT ON %I.%I FROM nexus_diag" in xml
    # The bulk form must never come back.
    assert "REVOKE SELECT ON ALL TABLES IN SCHEMA" not in xml
    # No cross-owner grant on the view (a non-owner GRANT hard-errors too).
    assert "GRANT SELECT ON nexus.diag_chash_conformance" not in xml
