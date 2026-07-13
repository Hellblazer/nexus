# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 Amendment A6 (nexus-9bufb): the structural content boundary.

The nexus_diag role's content boundary moves from lint-only (the
diag_connection choke point) to counts-by-construction: a superuser-owned
view over the chash-bearing tables, with direct table SELECT revoked by the
view-era grants changeset. These tests pin every coupling the design leans
on so no surface can drift from the others.
"""
from __future__ import annotations

import re
from pathlib import Path

from nexus.db.chash_tables import (
    CHASH_BEARING_TABLES,
    DIAG_CONFORMANCE_VIEW,
    chash_conformance_statements,
    diag_conformance_view_ddl,
    legacy_chash_conformance_statements,
)
from nexus.remediation.sql_lint import assert_read_only_diagnostics

_REPO = Path(__file__).resolve().parents[1]


def test_view_ddl_covers_exactly_the_chash_tables():
    ddl = diag_conformance_view_ddl()
    for t in CHASH_BEARING_TABLES:
        assert f"'{t}' AS table_name" in ddl
        assert f"FROM {t} WHERE length(chash) <> 32" in ddl
    # One UNION arm per table, no extras.
    assert ddl.count("UNION ALL") == len(CHASH_BEARING_TABLES) - 1


def test_statements_one_per_table_against_the_view():
    stmts = chash_conformance_statements()
    assert len(stmts) == len(CHASH_BEARING_TABLES)
    for stmt, t in zip(stmts, CHASH_BEARING_TABLES):
        assert DIAG_CONFORMANCE_VIEW in stmt
        assert f"table_name = '{t}'" in stmt


def test_view_statements_pass_the_diagnostic_lint():
    """The whole point: the emitted shape must clear the fail-closed
    aggregate-only lint (nexus.* target => aggregate select list)."""
    assert_read_only_diagnostics(chash_conformance_statements())
    assert_read_only_diagnostics(legacy_chash_conformance_statements())


def test_provision_embeds_the_generator_not_a_copy():
    src = (_REPO / "src/nexus/db/pg_provision.py").read_text()
    assert "diag_conformance_view_ddl" in src, (
        "pg_provision must render the view from the generator - a hand-typed "
        "copy would drift from CHASH_BEARING_TABLES"
    )


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


def test_grants_changeset_view_era_revokes_tables():
    """The view-era changeset must exist, be view-conditional, and revoke the
    direct table SELECT that the legacy era granted."""
    xml = (_REPO / "service/src/main/resources/db/changelog/grants-nexus-diag.xml").read_text()
    assert "grants-nexus-diag-2" in xml
    assert xml.count("diag_chash_conformance") >= 3  # precondition x2 + grant
    assert "REVOKE SELECT ON ALL TABLES IN SCHEMA nexus FROM nexus_diag" in xml
    assert "REVOKE SELECT ON ALL TABLES IN SCHEMA t1    FROM nexus_diag" in xml
    assert "GRANT SELECT ON nexus.diag_chash_conformance TO nexus_diag" in xml
