# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ocu9.11 (RDR-096 P5.2): tests for
``migrate_drop_source_path_column`` — the final-deprecation
migration that drops ``document_aspects.source_path`` after the
two-release dual-read window.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus.db.migrations import (
    MigrationError,
    migrate_drop_source_path_column,
)


def _make_post_phase1c_aspects(path: Path) -> sqlite3.Connection:
    """Create document_aspects at the post-RDR-108-Phase-1c +
    post-pnje shape:
    - PK migrated to (doc_id) at 4.30.0; source_path is now a denorm
      cache column (not PK), which lets ALTER TABLE DROP COLUMN
      succeed (SQLite refuses to drop PK columns).
    - source_uri column is present (added at 4.16.0).

    This is the schema the ocu9.11 migration is designed to operate
    on. It always runs AFTER Phase 1c in the MIGRATIONS list (4.30.0
    < 4.31.0), so production never reaches the drop step with
    source_path still in the PK.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE document_aspects (
            doc_id                 TEXT NOT NULL,
            collection             TEXT NOT NULL,
            source_path            TEXT NOT NULL DEFAULT '',
            problem_formulation    TEXT,
            proposed_method        TEXT,
            experimental_datasets  TEXT,
            experimental_baselines TEXT,
            experimental_results   TEXT,
            extras                 TEXT,
            confidence             REAL,
            extracted_at           TEXT NOT NULL,
            model_version          TEXT NOT NULL,
            extractor_name         TEXT NOT NULL,
            source_uri             TEXT,
            PRIMARY KEY (doc_id)
        );
    """)
    conn.commit()
    return conn


def _insert(
    conn: sqlite3.Connection,
    *, source_path: str, source_uri: str | None = None,
    collection: str = "knowledge__delos",
    doc_id: str | None = None,
) -> None:
    """Insert a row into the post-Phase-1c document_aspects table.
    ``doc_id`` is the PK; defaults to a hash of source_path so each
    test row has a unique key without callers having to invent one.
    """
    if doc_id is None:
        # Quick deterministic doc_id; tests just need uniqueness.
        doc_id = f"d-{abs(hash(source_path)) % 10**8}"
    conn.execute(
        "INSERT INTO document_aspects "
        "(doc_id, collection, source_path, source_uri, extracted_at, "
        " model_version, extractor_name) "
        "VALUES (?, ?, ?, ?, '2026-05-10T00:00:00Z', "
        " 'claude-haiku-4-5-20251001', 'scholarly-paper-v1')",
        (doc_id, collection, source_path, source_uri),
    )
    conn.commit()


class TestMigrateDropSourcePathColumn:
    def test_no_op_when_table_absent(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(str(tmp_path / "empty.db"))
        try:
            # Should not raise.
            migrate_drop_source_path_column(conn)
        finally:
            conn.close()

    def test_no_op_when_column_already_dropped(self, tmp_path: Path) -> None:
        """The migration is idempotent: re-running on a DB where
        source_path is already gone does nothing.
        """
        conn = sqlite3.connect(str(tmp_path / "post.db"))
        try:
            # Build the post-drop schema directly.
            conn.executescript("""
                CREATE TABLE document_aspects (
                    collection TEXT NOT NULL,
                    source_uri TEXT,
                    extracted_at TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    extractor_name TEXT NOT NULL
                );
            """)
            conn.commit()
            migrate_drop_source_path_column(conn)
            cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(document_aspects)"
                ).fetchall()
            }
            assert "source_path" not in cols
        finally:
            conn.close()

    def test_drops_source_path_when_all_rows_have_source_uri(
        self, tmp_path: Path,
    ) -> None:
        """The happy path: every row has source_uri populated, so
        the audit passes and the column drops.
        """
        conn = _make_post_phase1c_aspects(tmp_path / "ok.db")
        try:
            _insert(
                conn, source_path="/papers/a.pdf",
                source_uri="chroma://knowledge__delos//papers/a.pdf",
            )
            _insert(
                conn, source_path="/papers/b.pdf",
                source_uri="chroma://knowledge__delos//papers/b.pdf",
            )

            migrate_drop_source_path_column(conn)

            cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(document_aspects)"
                ).fetchall()
            }
            assert "source_path" not in cols
            assert "source_uri" in cols
            # Rows survive.
            n = conn.execute(
                "SELECT COUNT(*) FROM document_aspects"
            ).fetchone()[0]
            assert n == 2
        finally:
            conn.close()

    def test_blocks_when_a_row_has_null_source_uri(
        self, tmp_path: Path,
    ) -> None:
        """The pre-audit refuses to drop the column when even one row
        has NULL source_uri. Dropping would leave the row
        unaddressable; per the no-silent-fallback rule the migration
        raises MigrationError so the operator can triage.
        """
        conn = _make_post_phase1c_aspects(tmp_path / "null.db")
        try:
            _insert(
                conn, source_path="/papers/a.pdf",
                source_uri="chroma://knowledge__delos//papers/a.pdf",
            )
            _insert(
                conn, source_path="/papers/b.pdf",
                source_uri=None,  # The unmigrated row.
            )

            with pytest.raises(MigrationError, match="source_uri"):
                migrate_drop_source_path_column(conn)

            # Schema must be UNCHANGED — column still present, no
            # half-applied state.
            cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(document_aspects)"
                ).fetchall()
            }
            assert "source_path" in cols
        finally:
            conn.close()

    def test_blocks_when_a_row_has_empty_source_uri(
        self, tmp_path: Path,
    ) -> None:
        """Empty-string source_uri is treated the same as NULL: a row
        whose only addressing path is empty is unreachable post-drop.
        """
        conn = _make_post_phase1c_aspects(tmp_path / "empty.db")
        try:
            _insert(
                conn, source_path="/papers/a.pdf",
                source_uri="",  # Empty string, not NULL.
            )
            with pytest.raises(MigrationError, match="source_uri"):
                migrate_drop_source_path_column(conn)
        finally:
            conn.close()

    def test_audit_error_message_names_the_count(self, tmp_path: Path) -> None:
        """Operator-facing error message must surface the count of
        bad rows so triage isn't a guessing game.
        """
        conn = _make_post_phase1c_aspects(tmp_path / "count.db")
        try:
            _insert(conn, source_path="/p1.pdf", source_uri=None)
            _insert(conn, source_path="/p2.pdf", source_uri="")
            _insert(
                conn, source_path="/p3.pdf",
                source_uri="chroma://knowledge__delos//p3.pdf",
            )

            with pytest.raises(MigrationError) as exc_info:
                migrate_drop_source_path_column(conn)

            msg = str(exc_info.value)
            # 2 bad rows out of 3.
            assert "2" in msg
            # Names the remediation path so operator knows what to do.
            assert "backfill" in msg.lower() or "repair" in msg.lower()
        finally:
            conn.close()


class TestMigrationListRegistration:
    def test_drop_source_path_deferred_pending_callers_refactor(self) -> None:
        """nexus-ocu9.11 is intentionally deferred from MIGRATIONS.

        The schema migration is correct, but ``DocumentAspects.upsert``
        and ``DocumentAspects.get`` still reference ``source_path`` via
        SQL. Re-enabling requires a wider refactor to add a
        ``_has_source_path_column`` schema flag and branch every
        reference. The function definition stays in place so re-enable
        is a one-line registry change.
        """
        from nexus.db.migrations import MIGRATIONS

        targets = [
            m for m in MIGRATIONS
            if m.fn is migrate_drop_source_path_column
        ]
        assert targets == []
