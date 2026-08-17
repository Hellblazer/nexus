# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 P5.2 (nexus-ykzbj.18): MVV proof (b) — the no-store-content-read
property + the end-to-end opt-in remediation flow.

Two tiers:

1. MECHANICAL (always runs): the shipped forensics topic's diagnostic SQL
   reads only SCHEMA/METADATA — aggregate counts over chunk tables and
   catalog constraint metadata — never row/document/note CONTENT. Proven by
   enumerating the objects each statement reads and asserting they are a
   subset of the known metadata-safe set, and by proving a content-reading
   statement is rejected by the same lint the emitter runs.

2. REAL-PG END-TO-END (self-provisioning, max-skip): seed a poisoned store
   (a non-32-char chash row under tenant ``default``), then drive the exact
   forensics diagnostic through the sanctioned ``nexus_diag`` choke point and
   assert (a) it DETECTS the poison cross-tenant (BYPASSRLS — the nexus-vounk
   property: a policy-subject role would count 0), (b) it reads only the
   metadata objects, and (c) a content SELECT is refused before DB contact.
"""
from __future__ import annotations

import getpass
import re
import subprocess

import pytest

from nexus.db.chash_tables import (
    CHUNKS_TABLE,
    DIAG_CONFORMANCE_VIEW,
    diag_conformance_view_ddl,
    legacy_chash_conformance_statements,
)
from nexus.remediation import StoreState, emit_forensics_playbook

# The content-safe objects the chash-poison forensics topic is allowed to
# read. Everything here is schema/metadata (row counts, chash LENGTHS,
# constraint names/validation flags) — never row/document/note content.
_ALLOWED_READ_OBJECTS = {
    # RDR-191 (nexus-o8dil.19): chunks_384/768/1024 unify into ONE relation.
    "nexus.chunks",
    "nexus.chash_index", "nexus.catalog_document_chunks",
    "pg_constraint",
    # Amendment A6 (nexus-9bufb): the superuser-owned counts view. Stronger
    # than the raw tables above — it exposes ONLY (table_name, count), so
    # content projection is impossible by construction, not just by lint.
    "nexus.diag_chash_conformance",
    # nexus-z5j0t legacy-debt legs: these names appear ONLY as table_name
    # string literals filtering the counts view (the object-reference regex
    # below cannot tell a literal from a FROM target); the statements read
    # the view, and even a direct count over them would be aggregate-only
    # metadata (chash-reference lengths, never content).
    "nexus.topic_assignments", "nexus.frecency", "nexus.relevance_log",
    # nexus-rpw6u (RDR-191 straddle handling): the legacy-era statement set
    # filters the SAME counts view by the pre-unify per-dim table names —
    # same string-literal caveat as the debt legs above, plus the
    # to_regclass('nexus.chunks') era-probe statement's own literal.
    "nexus.chunks_384", "nexus.chunks_768", "nexus.chunks_1024",
}
#: Column tokens that would indicate CONTENT projection (must never appear
#: as a bare projected column in a diagnostic statement).
_CONTENT_COLUMNS = ("content", "document", "title", "text", "body", "note")


def _forensics_sql() -> tuple[str, ...]:
    return emit_forensics_playbook(
        "chash-poison", StoreState(detail="")
    ).diagnostic_sql


# ── Tier 1: mechanical no-content-read property ─────────────────────────────

class TestNoContentReadProperty:
    def test_every_statement_reads_only_metadata_objects(self):
        for stmt in _forensics_sql():
            refs = set(re.findall(r"\b(nexus\.\w+|pg_constraint)\b", stmt))
            assert refs, f"no object reference parsed from: {stmt}"
            assert refs <= _ALLOWED_READ_OBJECTS, (
                f"statement reads a non-metadata object: {stmt} -> "
                f"{refs - _ALLOWED_READ_OBJECTS}"
            )

    def test_no_statement_projects_a_content_column(self):
        for stmt in _forensics_sql():
            select_part = re.search(
                r"\bSELECT\b(.*?)\bFROM\b", stmt, re.IGNORECASE | re.DOTALL
            )
            if select_part is None:
                # nexus-rpw6u: the RDR-191 era-probe statement
                # (to_regclass('nexus.chunks')) has no FROM target at all —
                # a metadata function call with nothing to project content
                # from, so there is nothing for this check to examine.
                assert "FROM" not in stmt.upper(), stmt
                continue
            projected = select_part.group(1).lower()
            for col in _CONTENT_COLUMNS:
                # `length(chash)` is a metadata function over a hash, not a
                # content projection — allow the count/length forms, forbid a
                # bare content column.
                assert not re.search(rf"\b{col}\b(?!\s*\()", projected), (
                    f"content column {col!r} projected by: {stmt}"
                )

    def test_forensics_sql_passes_the_read_only_lint(self):
        from nexus.remediation.sql_lint import assert_read_only_diagnostics

        assert_read_only_diagnostics(_forensics_sql())  # raises on violation

    def test_health_probe_and_forensics_share_the_chash_table_set(self):
        """nexus-vounk drift guard: the operator health probe and the
        agent-facing forensics topic count the SAME tables (one poisoned
        table must not slip past one surface but not the other)."""
        from nexus.db.chash_tables import chash_conformance_statements

        shared = chash_conformance_statements()
        # The forensics topic's diagnostic SQL leads with exactly these
        # per-table count statements (then adds the constraint-state query).
        assert _forensics_sql()[:len(shared)] == shared

    def test_a_content_read_would_be_rejected_by_the_same_path(self):
        """Non-vacuity: the lint the emitter runs rejects a content SELECT —
        so the property is enforced, not merely true of today's statements."""
        from nexus.remediation.sql_lint import (
            DiagnosticSqlViolation,
            assert_read_only_diagnostics,
        )

        with pytest.raises(DiagnosticSqlViolation):
            assert_read_only_diagnostics(["SELECT content FROM nexus.memory"])
        # And the UNQUALIFIED form is fail-closed too (critic-final M1) — a
        # future topic author omitting the schema prefix cannot leak content.
        with pytest.raises(DiagnosticSqlViolation):
            assert_read_only_diagnostics(["SELECT content FROM chunks_768"])


# ── Tier 2: real-PG end-to-end (self-provisioning, max-skip) ────────────────


from tests.db._service_fixture import pg_bin_dir

# THE GATE RESOLVES THROUGH THE SELF-PROVISIONING SEAM, NEVER AMBIENT DISCOVERY.
# Locked policy: nexus ALWAYS uses the PostgreSQL it BUILDS — never Homebrew,
# never a host install (T2 always-install-pg-bundle-no-fallback). Gating on
# discover_pg_binaries() asked whether the BOX carries a PostgreSQL, which is a
# property of the machine rather than of the substrate under test, and silently
# skipped this whole class on any box without a host install — while every
# fixture below already resolved its binaries through pg_bin_dir(). Mechanically
# enforced by tests/db/test_pg_gate_is_self_provisioning.py.
#
#: Real max-skip guard (testval-182 Low): clean SKIP when self-provisioning
#: fails, never an ERROR from a fixture calling a nonexistent initdb.
_INITDB = pg_bin_dir() / "initdb"

_requires_pg = pytest.mark.skipif(
    not _INITDB.exists(),
    reason=f"skipped: nexus-pg bundle self-provisioning failed (no {_INITDB}). "
           "NOT a missing host PostgreSQL — these tests never use one.",
)


@pytest.mark.integration
@_requires_pg
class TestEndToEndPoisonedStore:
    @pytest.fixture(scope="class")
    def poisoned_cluster(self, tmp_path_factory):
        from nexus.db.pg_provision import (
            PgBinaries,
            _configure_cluster,
            _create_db,
            _create_roles,
            _init_cluster,
            _start_cluster,
        )
        from tests.db._service_fixture import pg_bin_dir

        bins = PgBinaries.from_dir(pg_bin_dir())
        pgdata = tmp_path_factory.mktemp("mvv-pg") / "data"
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        os_user = getpass.getuser()

        _init_cluster(bins, pgdata, os_user)
        _configure_cluster(pgdata, port)
        _start_cluster(bins, pgdata, port)
        _create_db(bins, port, os_user)
        created = _create_roles(bins, port, os_user, "a-pw", "s-pw", "diag-pw")
        assert created.diag_created is True

        def su(sql: str) -> str:
            proc = subprocess.run(
                [str(bins.psql), "-h", "127.0.0.1", "-p", str(port),
                 "-U", os_user, "-d", "nexus", "-v", "ON_ERROR_STOP=1",
                 "-tAc", sql],
                capture_output=True, text=True, timeout=30,
            )
            assert proc.returncode == 0, proc.stderr
            return proc.stdout.strip()

        # Minimal FORCE-RLS nexus.chunks (RDR-191 unified relation — the
        # table the SHIPPED forensics diagnostic actually targets, nexus-
        # rpw6u retarget) with ONE poisoned (non-32-byte) row under tenant
        # 'default', PLUS the four other tables diag_conformance_view_ddl()
        # references (catalog_document_chunks + the three debt legs) so the
        # REAL Amendment-A6 counts view can be created — critic round 1
        # CRITICAL-1: the old fixture used a hand-rolled direct count
        # (`legacy_chash_conformance_statements()`, the pre-A6/view-absent
        # FALLBACK shape), never the VIEW-based statement
        # (`chash_conformance_statements()`) `_chash_poison_forensics`
        # actually SHIPS — so the view path had zero end-to-end coverage.
        # chash is BYTEA here (not TEXT) because the debt legs' anti-join
        # decodes against it (see diag_conformance_view_ddl's docstring);
        # convert_to(...) produces an arbitrary-length bytea without going
        # through a hex parse.
        su("CREATE SCHEMA IF NOT EXISTS nexus AUTHORIZATION nexus_admin")
        su(
            f"SET ROLE nexus_admin; "
            f"CREATE TABLE IF NOT EXISTS {CHUNKS_TABLE} ("
            "  chash BYTEA NOT NULL, tenant_id TEXT NOT NULL); "
            f"ALTER TABLE {CHUNKS_TABLE} ENABLE ROW LEVEL SECURITY; "
            f"ALTER TABLE {CHUNKS_TABLE} FORCE ROW LEVEL SECURITY; "
            f"DROP POLICY IF EXISTS ti ON {CHUNKS_TABLE}; "
            f"CREATE POLICY ti ON {CHUNKS_TABLE} "
            "  USING (tenant_id = current_setting('nexus.tenant', true)) "
            "  WITH CHECK (tenant_id = current_setting('nexus.tenant', true)); "
            "SELECT set_config('nexus.tenant','default',false); "
            f"INSERT INTO {CHUNKS_TABLE} (chash, tenant_id) "
            "  VALUES (convert_to('short-poison-id', 'UTF8'), 'default'); "
            "CREATE TABLE IF NOT EXISTS nexus.catalog_document_chunks "
            "  (chash TEXT NOT NULL); "
            # RDR-194 P3c (taxonomy-011-doc-id-bytea.xml): doc_id is BYTEA in
            # the real schema — ChashBearingTable("nexus.topic_assignments",
            # "doc_id", poison=False, bytea=True) — so the debt leg's
            # anti-join (c.chash = t.doc_id, both against nexus.chunks.chash
            # BYTEA) needs a BYTEA column here too, or PG rejects the view
            # with "operator does not exist: bytea = text".
            "CREATE TABLE IF NOT EXISTS nexus.topic_assignments "
            "  (doc_id BYTEA NOT NULL); "
            "CREATE TABLE IF NOT EXISTS nexus.frecency "
            "  (chunk_id TEXT NOT NULL); "
            "CREATE TABLE IF NOT EXISTS nexus.relevance_log "
            "  (chunk_id TEXT NOT NULL);"
        )
        # The view is created by the CLUSTER SUPERUSER (os_user, a fresh
        # connection — SET ROLE from the call above does not carry over),
        # matching production's superuser provisioning path. RLS bypass for
        # nexus_diag comes from its own BYPASSRLS role attribute
        # (_create_roles), not from view ownership.
        #
        # bead nexus-lhuhe (2026-08-17, grants-nexus-diag-3.xml) SUPERSEDES
        # the "SELECT on the VIEW only" shape this comment used to describe:
        # diag_conformance_view_ddl() now carries WITH (security_invoker =
        # true) (nexus-i3k3e/Sig-2, same day), which means Postgres checks
        # the INVOKING role's OWN table privileges for every relation the
        # view reads — nexus_diag's BYPASSRLS exempts it from row-level
        # policies, never from ordinary GRANT-based privilege checks. So
        # nexus_diag needs DIRECT SELECT on every CHASH_BEARING_TABLES
        # relation too, exactly what grants-nexus-diag-3 grants in
        # production (named individually, never a schema-wide bulk grant).
        su(diag_conformance_view_ddl())
        su(
            "GRANT USAGE ON SCHEMA nexus TO nexus_diag; "
            f"GRANT SELECT ON {DIAG_CONFORMANCE_VIEW} TO nexus_diag; "
            f"GRANT SELECT ON {CHUNKS_TABLE} TO nexus_diag; "
            "GRANT SELECT ON nexus.catalog_document_chunks TO nexus_diag; "
            "GRANT SELECT ON nexus.topic_assignments TO nexus_diag; "
            "GRANT SELECT ON nexus.frecency TO nexus_diag; "
            "GRANT SELECT ON nexus.relevance_log TO nexus_diag;"
        )
        yield {"port": port, "psql": bins.pg_ctl and str(bins.psql)}
        subprocess.run(
            [str(bins.pg_ctl), "-D", str(pgdata), "stop", "-m", "immediate"],
            capture_output=True, text=True, timeout=30,
        )

    def test_forensics_probe_detects_poison_cross_tenant_read_only(self, poisoned_cluster):
        from pathlib import Path

        from nexus.db.chash_tables import chash_conformance_statements
        from nexus.db.diag_connection import DiagCredentials, run_diagnostic_sql
        from tests.db._service_fixture import pg_bin_dir

        creds = DiagCredentials(
            port=poisoned_cluster["port"], user="nexus_diag", password="diag-pw",
        )
        # The nexus.chunks (unified) leg of the ACTUAL shipped forensics
        # statement — the Amendment-A6 VIEW-based sum
        # (`chash_conformance_statements()`, what `_chash_poison_forensics`
        # really emits — critic round 1 CRITICAL-1 retarget, not the
        # direct-count fallback shape) — run via the sanctioned choke point
        # (lint + read-only session, NO tenant GUC set: nexus_diag's
        # BYPASSRLS role attribute sees the poisoned row a policy-subject
        # role would count as 0, the nexus-vounk property).
        stmt = chash_conformance_statements()[0]
        assert f"'{CHUNKS_TABLE}'" in stmt
        out = run_diagnostic_sql([stmt], creds, psql_bin=Path(pg_bin_dir()) / "psql")
        assert out == ["1"], "forensics probe did not detect the poisoned row"

    def test_content_read_refused_before_db_contact(self, poisoned_cluster):
        from pathlib import Path

        from nexus.db.diag_connection import DiagCredentials, run_diagnostic_sql
        from nexus.remediation.sql_lint import DiagnosticSqlViolation
        from tests.db._service_fixture import pg_bin_dir

        creds = DiagCredentials(
            port=poisoned_cluster["port"], user="nexus_diag", password="diag-pw",
        )
        with pytest.raises(DiagnosticSqlViolation):
            run_diagnostic_sql(
                [f"SELECT chash FROM {CHUNKS_TABLE}"],
                creds, psql_bin=Path(pg_bin_dir()) / "psql",
            )

    def test_vounk_admin_counts_zero_but_diag_counts_the_poison(self, poisoned_cluster):
        """nexus-vounk regression, the exact 0-vs-9 shape on a REAL FORCE-RLS
        store: nexus_admin with NO tenant GUC counts 0 (the vacuous health
        probe the fix retires), while the nexus_diag/BYPASSRLS path counts the
        poisoned row — what Liquibase VALIDATE sees and the pre-upgrade gate
        MUST see."""
        import os
        import subprocess
        from pathlib import Path

        from nexus.db.chash_tables import chash_conformance_statements
        from nexus.db.diag_connection import DiagCredentials, run_diagnostic_sql
        from tests.db._service_fixture import pg_bin_dir

        # (a) nexus_admin (NOSUPERUSER, the health probe's old identity), no
        # GUC -> FORCE RLS filters everything -> 0 (the vacuous count). Must
        # NOT be a superuser here — superusers bypass RLS and would see the
        # row, hiding the very bug this asserts. Direct table query — this
        # half proves RLS applies to nexus_admin at all, independent of the
        # diagnostic surface half (b) below, so it uses the direct-count
        # shape (nexus_admin has no view grant, only its own table).
        admin_stmt = legacy_chash_conformance_statements()[0]
        admin = subprocess.run(
            [str(Path(pg_bin_dir()) / "psql"), "-h", "127.0.0.1",
             "-p", str(poisoned_cluster["port"]), "-U", "nexus_admin",
             "-d", "nexus", "-tAc", admin_stmt],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PGPASSWORD="a-pw"),
        )
        assert admin.returncode == 0, admin.stderr
        assert admin.stdout.strip() == "0", (
            "nexus_admin (no GUC) should count 0 under FORCE RLS — the vacuous "
            "path the vounk fix retires"
        )
        # (b) nexus_diag via the choke point, through the SAME view-based
        # statement the shipped forensics diagnostic runs (nexus-rpw6u
        # retarget) -> nexus_diag's BYPASSRLS role attribute sees the
        # poison the admin path missed.
        creds = DiagCredentials(
            port=poisoned_cluster["port"], user="nexus_diag", password="diag-pw",
        )
        diag_stmt = chash_conformance_statements()[0]
        diag = run_diagnostic_sql([diag_stmt], creds, psql_bin=Path(pg_bin_dir()) / "psql")
        assert diag == ["1"], "diag path must see the poisoned row the admin path missed"


class TestEndToEndOptInRemediationFlow:
    """MVV proof #2's REMEDIATE leg (critic-final C1): the forensics-only
    integration above proves the read-only boundary; this proves the opt-in
    remediation FLOW — flag on → remediate hands over the playbook AND a
    granted=True consent row is recorded along the path.

    Scope honesty: the RDR's MVV #2 also names "walk §8.1 → a re-run upgrade
    succeeds." That live upgrade-to-VERIFIED-and-unlocked is the domain of the
    guided-upgrade rehearsal E2E (tests/e2e/migration-rehearsal/, a container
    tier), NOT re-proven here — this asserts the product's half of the
    contract (consented, audited handoff of the recovery playbook), which is
    what RDR-182 actually ships. The mutation itself is executed by the user's
    agent per §5, not by the product.
    """

    def _remediate(self, monkeypatch, tmp_path, recorder, confirm):
        from contextlib import contextmanager

        from nexus.mcp import core

        cfg = tmp_path / "config"
        cfg.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
        monkeypatch.chdir(tmp_path)
        (cfg / "config.yml").write_text(
            "claude_assisted_remediation:\n  enabled: true\n"
        )
        # No live PG needed for the flow proof — stub the diag leg.
        monkeypatch.setattr(core, "_diag_resolve", lambda creds_path=None: None)

        class _Db:
            telemetry = recorder

        @contextmanager
        def _ctx():
            yield _Db()

        monkeypatch.setattr(core, "_t2_ctx", _ctx)
        return core.remediate("chash-poison", confirm=confirm)

    def test_opt_in_flow_records_consent_and_hands_over_playbook(
        self, tmp_path, monkeypatch
    ):
        from nexus.remediation import StoreState, emit_playbook

        rows: list[dict] = []

        class _Recorder:
            def record_consent(self, *, scope, ts, granted):
                rows.append({"scope": scope, "ts": ts, "granted": granted})

        out = self._remediate(monkeypatch, tmp_path, _Recorder(), confirm=True)

        # (1) consent recorded along the path — granted=True, correct scope
        assert rows == [
            {"scope": "remediate:chash-poison", "ts": rows[0]["ts"],
             "granted": True}
        ]
        # (2) the recovery playbook was handed over (ordered steps released)
        steps = emit_playbook("chash-poison", StoreState(detail="x")).steps
        for step in steps:
            assert step in out

    def test_describe_stage_hands_over_nothing_and_records_nothing(
        self, tmp_path, monkeypatch
    ):
        from nexus.remediation import StoreState, emit_playbook

        rows: list[dict] = []

        class _Recorder:
            def record_consent(self, *, scope, ts, granted):
                rows.append(1)

        out = self._remediate(monkeypatch, tmp_path, _Recorder(), confirm=False)
        assert rows == []  # no consent at describe stage
        steps = emit_playbook("chash-poison", StoreState(detail="x")).steps
        for step in steps:
            assert step not in out  # steps withheld until consent
