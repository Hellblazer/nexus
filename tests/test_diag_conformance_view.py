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
    subset is exactly the two survivors post-RDR-191 unify (the gate must
    not grow; chunks_384/768/1024 collapsed into ONE nexus.chunks relation,
    nexus.chash_index retired by RDR-187 / nexus-piwya.5 ahead of the
    table DROP, independently of the unify)."""
    by_table = {t.table: t for t in CHASH_BEARING_TABLES}
    assert by_table["nexus.topic_assignments"].column == "doc_id"
    assert by_table["nexus.frecency"].column == "chunk_id"
    assert by_table["nexus.relevance_log"].column == "chunk_id"
    assert not by_table["nexus.topic_assignments"].poison
    assert not by_table["nexus.frecency"].poison
    assert not by_table["nexus.relevance_log"].poison
    # RDR-194 P3c (nexus-tk070.p3c): doc_id is bytea now; the other two debt
    # columns stay TEXT (nexus-lgdel.l1's canonical-only CHECK covers them
    # directly).
    assert by_table["nexus.topic_assignments"].bytea
    assert not by_table["nexus.frecency"].bytea
    assert not by_table["nexus.relevance_log"].bytea
    assert tuple(t.table for t in POISON_CHASH_TABLES) == (
        "nexus.chunks",
        "nexus.catalog_document_chunks",
    )
    # RDR-187 pin: the retired router must not reappear in the registry.
    assert "nexus.chash_index" not in {t.table for t in CHASH_BEARING_TABLES}
    # RDR-191 pin: the per-dim shards must not reappear either.
    assert "nexus.chunks_384" not in {t.table for t in CHASH_BEARING_TABLES}
    assert "nexus.chunks_768" not in {t.table for t in CHASH_BEARING_TABLES}
    assert "nexus.chunks_1024" not in {t.table for t in CHASH_BEARING_TABLES}
    assert all(t.column == "chash" for t in POISON_CHASH_TABLES)
    assert set(CHASH_BEARING_TABLES) == set(POISON_CHASH_TABLES) | set(DEBT_CHASH_TABLES)


def test_view_ddl_covers_exactly_the_chash_tables():
    ddl = diag_conformance_view_ddl()
    for t in POISON_CHASH_TABLES:
        assert f"'{t.table}' AS table_name" in ddl
        assert f"FROM {t.table} WHERE octet_length({t.column}) <> 32" in ddl
    # Debt legs are SEMANTIC anti-joins (RDR-180 .6 amendment 1): hex-shaped
    # references that miss every chunk-table join; titles/non-hex identities
    # are excluded by the hex guard (not chash debt). RDR-191: the anti-join
    # used to be a 3-way AND (one per dim shard) and is now ONE NOT EXISTS
    # against the unified nexus.chunks relation. RDR-194 P3c: debt columns
    # are no longer uniformly TEXT -- a bytea debt column (topic_assignments.
    # doc_id) skips the hex-shape guard entirely (direct bytea equality);
    # only the still-TEXT debt columns (frecency/relevance_log) keep it.
    for t in DEBT_CHASH_TABLES:
        assert f"'{t.table}' AS table_name" in ddl
        if t.bytea:
            assert f"c.chash = t.{t.column})" in ddl
            assert f"t.{t.column} ~ '^[0-9a-f]+$'" not in ddl
        else:
            assert f"t.{t.column} ~ '^[0-9a-f]+$'" in ddl
    assert ddl.count("NOT EXISTS") == len(DEBT_CHASH_TABLES)
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


def test_liquibase_owned_view_matches_the_generator():
    """RDR-194 P3c critical fix round (2026-08-17, critic Sig-2 / bead
    nexus-i3k3e): ``taxonomy-011-doc-id-bytea.xml``'s ``taxonomy-011-8``
    changeset embeds a LITERAL SQL copy of ``diag_conformance_view_ddl()``'s
    output (a static XML changelog cannot import the Python generator) —
    pin the two copies to each other, same containment-check pattern as
    :func:`test_docs_rendered_copy_matches_the_generator`, so a
    ``CHASH_BEARING_TABLES`` change regenerates BOTH the docs block and the
    changelog's embedded copy, not just one of them silently drifting from
    the other."""
    xml = (
        _REPO
        / "service/src/main/resources/db/changelog/taxonomy-011-doc-id-bytea.xml"
    ).read_text()
    # The changeset's SQL is nested inside a `DO $$ BEGIN ... BEGIN ... END;`
    # block (2026-08-17 defensive fix, critic Sig-1/-2: a foreign-owned
    # pre-existing view must degrade gracefully, not abort the migration),
    # so its lines carry an 8-space indent the bare generator output does
    # not — normalize that away the same way
    # test_docs_rendered_copy_matches_the_generator normalizes the docs'
    # 3-space markdown indent.
    xml_flat = re.sub(r"\n {8}", "\n", xml)
    ddl = diag_conformance_view_ddl()
    # The XML changelog escapes the two XML-special characters this DDL text
    # contains (`<>` in the poison legs' octet_length inequality) as entity
    # references — everything else in the DDL (single quotes, `~`, `$`) is
    # XML-legal as bare text content and needs no escaping.
    xml_escaped_ddl = ddl.replace("<", "&lt;").replace(">", "&gt;")
    assert xml_escaped_ddl in xml_flat, (
        "taxonomy-011-doc-id-bytea.xml's taxonomy-011-8 changeset's embedded "
        "CREATE VIEW SQL drifted from nexus.db.chash_tables."
        "diag_conformance_view_ddl() — regenerate the changeset's SQL"
    )


def test_poison_detail_token_couples_probe_and_gates():
    """The convergence gate (upgrade_finish.py) distinguishes REAL poison
    from probe-degraded WARNs by substring-matching the health detail
    against the ONE constant — a hand-typed phrase silently disarms the
    gate (nexus-jxizy.5). Since fc24123c (nexus-pgdcv) the install-binary
    gate (daemon.py) no longer matches the token itself: it couples
    through the shared tri-state ``_poison_probe`` classifier, so the two
    gates cannot diverge on the unknown state either."""
    from nexus.db.chash_tables import POISON_DETAIL_TOKEN

    for rel in (
        "src/nexus/health.py",
        "src/nexus/upgrade_finish.py",
    ):
        src = (_REPO / rel).read_text()
        assert "POISON_DETAIL_TOKEN" in src, rel
        assert '"non-32-char chash" in r.detail' not in src, rel
    daemon_src = (_REPO / "src/nexus/commands/daemon.py").read_text()
    # Require the CALL SITE, not a bare identifier mention (critic 2026-07-21:
    # a docstring reference alone must not satisfy the coupling tripwire).
    import re

    assert re.search(r"=\s*_poison_probe\(", daemon_src), (
        "daemon.py's install-binary gate must CALL the shared _poison_probe "
        "classifier (not merely mention it) — nexus-jxizy.5 / nexus-pgdcv"
    )
    assert '"non-32-char chash" in r.detail' not in daemon_src
    assert POISON_DETAIL_TOKEN  # non-empty, importable


def test_grants_changeset_view_era_revokes_tables():
    """The view-era changeset must exist, be view-conditional, and revoke the
    direct table SELECT that the legacy era granted — PER-RELATION and
    OWNER-RESTRICTED (nexus-46yy3, live-reproduced P0: the bulk
    ALL-TABLES-IN-SCHEMA form hard-errors on the superuser-owned view from
    the NOSUPERUSER nexus_admin migration connection, crash-looping every
    boot once the view exists). The REVOKE changeset (-2) must NOT grant the
    view — since nexus-lhuhe the view grant lives in grants-nexus-diag-3,
    the boot's LAST word (foreign-owner tolerant), because -2's own revoke
    loop strips whatever taxonomy-011-8 granted earlier the same boot."""
    xml = (_REPO / "service/src/main/resources/db/changelog/grants-nexus-diag.xml").read_text()
    assert "grants-nexus-diag-2" in xml
    # 2x in-body era guard (nexus-ixsxa moved these out of <preConditions>,
    # which INSERTED a changelog row per boot on a runAlways changeset — see
    # tests/test_changelog_markran_lint.py) + 5 prose mentions, + 4 from the
    # nexus-lhuhe view re-grant in -3 (1 GRANT target + 3 comment/NOTICE
    # mentions). Exact pin so a NEW writer of this name is a conscious edit
    # here, not silent drift.
    assert xml.count("diag_chash_conformance") == 11
    # Both changesets must stay runAlways with the era test in the BODY. Parsed,
    # not substring-matched: this file's header documents the rejected
    # alternatives verbatim, so `"runOnChange" not in xml` would fail on the
    # prose explaining why runOnChange is wrong. The corpus-wide rule lives in
    # tests/test_changelog_markran_lint.py.
    import xml.etree.ElementTree as ET

    ns = "{http://www.liquibase.org/xml/ns/dbchangelog}"
    root = ET.parse(
        _REPO / "service/src/main/resources/db/changelog/grants-nexus-diag.xml"
    ).getroot()
    diag_sets = list(root.iter(f"{ns}changeSet"))
    assert [cs.get("id") for cs in diag_sets] == [
        "grants-nexus-diag-1",
        "grants-nexus-diag-2",
        # nexus-8yz1p (2026-08-16): third, era-independent staging-schema
        # SELECT changeset -- deliberately its own changeset, not folded
        # into -1's era-gated body (see that changeset's own comment).
        "grants-nexus-diag-3",
    ]
    for cs in diag_sets:
        assert cs.get("runAlways") == "true", (
            f"{cs.get('id')}: the era is a per-boot runtime question — runOnChange "
            "would be filtered out once ran, so a later-created nexus_diag would "
            "never be granted and the view-era REVOKE would never fire (nexus-ixsxa)"
        )
        assert cs.find(f"{ns}preConditions") is None, (
            f"{cs.get('id')}: the era test belongs in the DO $$ body — as a "
            "preCondition it resolves onFail=MARK_RAN, which INSERTS a "
            "DATABASECHANGELOG row on every boot (nexus-ixsxa)"
        )
    # The P0 shape: per-relation loop, restricted to relations this role owns.
    assert "pg_get_userbyid(c.relowner) = current_user" in xml
    assert "REVOKE SELECT ON %I.%I FROM nexus_diag" in xml
    # The bulk form must never come back.
    assert "REVOKE SELECT ON ALL TABLES IN SCHEMA" not in xml
    # Exactly ONE view grant, in grants-nexus-diag-3 (nexus-lhuhe: the boot's
    # last word re-grants what -2's revoke loop stripped from taxonomy-011-8),
    # and it MUST be insufficient_privilege-wrapped — a bare non-owner GRANT
    # hard-errors (the nexus-46yy3 crash-loop class this test originally
    # pinned; the wrap is what makes the grant safe where the ban used to be).
    assert xml.count("GRANT SELECT ON nexus.diag_chash_conformance") == 1
    grant_at = xml.index("GRANT SELECT ON nexus.diag_chash_conformance")
    assert "EXCEPTION WHEN insufficient_privilege" in xml[grant_at : grant_at + 400], (
        "the view grant must be insufficient_privilege-wrapped (foreign-owner "
        "tolerant) or it reintroduces the nexus-46yy3 boot crash-loop"
    )
