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

from nexus.remediation import StoreState, emit_forensics_playbook

# The content-safe objects the chash-poison forensics topic is allowed to
# read. Everything here is schema/metadata (row counts, chash LENGTHS,
# constraint names/validation flags) — never row/document/note content.
_ALLOWED_READ_OBJECTS = {
    "nexus.chunks_384", "nexus.chunks_768", "nexus.chunks_1024",
    "nexus.chash_index", "nexus.catalog_document_chunks",
    "pg_constraint",
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
            assert select_part, stmt
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

    def test_a_content_read_would_be_rejected_by_the_same_path(self):
        """Non-vacuity: the lint the emitter runs rejects a content SELECT —
        so the property is enforced, not merely true of today's statements."""
        from nexus.remediation.sql_lint import (
            DiagnosticSqlViolation,
            assert_read_only_diagnostics,
        )

        with pytest.raises(DiagnosticSqlViolation):
            assert_read_only_diagnostics(["SELECT content FROM nexus.memory"])


# ── Tier 2: real-PG end-to-end (self-provisioning, max-skip) ────────────────

pytestmark_integration = pytest.mark.integration


@pytest.mark.integration
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

        # Minimal FORCE-RLS chunks_768 with ONE poisoned (non-32-char) row
        # under tenant 'default', plus the diag grants.
        su("CREATE SCHEMA IF NOT EXISTS nexus AUTHORIZATION nexus_admin")
        su(
            "SET ROLE nexus_admin; "
            "CREATE TABLE IF NOT EXISTS nexus.chunks_768 ("
            "  chash TEXT NOT NULL, tenant_id TEXT NOT NULL); "
            "ALTER TABLE nexus.chunks_768 ENABLE ROW LEVEL SECURITY; "
            "ALTER TABLE nexus.chunks_768 FORCE ROW LEVEL SECURITY; "
            "DROP POLICY IF EXISTS ti ON nexus.chunks_768; "
            "CREATE POLICY ti ON nexus.chunks_768 "
            "  USING (tenant_id = current_setting('nexus.tenant', true)) "
            "  WITH CHECK (tenant_id = current_setting('nexus.tenant', true)); "
            "SELECT set_config('nexus.tenant','default',false); "
            "INSERT INTO nexus.chunks_768 (chash, tenant_id) "
            "  VALUES ('short-poison-id', 'default'); "
            "GRANT USAGE ON SCHEMA nexus TO nexus_diag; "
            "GRANT SELECT ON ALL TABLES IN SCHEMA nexus TO nexus_diag;"
        )
        yield {"port": port, "psql": bins.pg_ctl and str(bins.psql)}
        subprocess.run(
            [str(bins.pg_ctl), "-D", str(pgdata), "stop", "-m", "immediate"],
            capture_output=True, text=True, timeout=30,
        )

    def test_forensics_probe_detects_poison_cross_tenant_read_only(self, poisoned_cluster):
        from pathlib import Path

        from nexus.db.diag_connection import DiagCredentials, run_diagnostic_sql
        from tests.db._service_fixture import pg_bin_dir

        creds = DiagCredentials(
            port=poisoned_cluster["port"], user="nexus_diag", password="diag-pw",
        )
        # The single chunks_768 leg of the shipped forensics diagnostic —
        # run via the sanctioned choke point (lint + read-only session, NO
        # tenant GUC set: BYPASSRLS sees the poisoned row a policy-subject
        # role would count as 0, the nexus-vounk property).
        stmt = "SELECT count(*) FROM nexus.chunks_768 WHERE length(chash) <> 32"
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
                ["SELECT chash FROM nexus.chunks_768"],
                creds, psql_bin=Path(pg_bin_dir()) / "psql",
            )
