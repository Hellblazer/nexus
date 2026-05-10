# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-108 Phase 1c: document_aspects + aspect_extraction_queue PK migration
to (doc_id) — nexus-je0b.

Tests cover:
- Schema target: PRIMARY KEY (doc_id), denorm cache columns retained
- Backfill via direct (collection, file_path) JOIN against catalog docs
- Backfill via supersede-chain JOIN for legacy-but-mapped collections
- Test-fixture rows hard-deleted (5 fixture collection prefixes)
- High-volume unmapped rows trigger fail-loud (MigrationError)
- Drain precondition: migration BLOCKS if queue has pending/in_progress rows
- Drain precondition: migration RUNS if queue is empty or only has failed rows
- mark_done(doc_id) works post-migration
- claim_next returns doc_id field
- Idempotent: re-running migration on already-migrated DB is a no-op
- Cross-DB ATTACH cleanup: migration leaves no dangling attached schemas
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_memory_db(path: Path) -> sqlite3.Connection:
    """Create a fresh memory.db with pre-migration schema for both tables."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS document_aspects (
            collection             TEXT NOT NULL,
            source_path            TEXT NOT NULL,
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
            PRIMARY KEY (collection, source_path)
        );
        CREATE INDEX IF NOT EXISTS idx_document_aspects_extractor
            ON document_aspects(extractor_name, model_version);

        CREATE TABLE IF NOT EXISTS aspect_extraction_queue (
            collection      TEXT NOT NULL,
            source_path     TEXT NOT NULL,
            doc_id          TEXT NOT NULL DEFAULT '',
            content_hash    TEXT NOT NULL DEFAULT '',
            content         TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'pending',
            retry_count     INTEGER NOT NULL DEFAULT 0,
            enqueued_at     TEXT NOT NULL,
            last_attempt_at TEXT,
            last_error      TEXT,
            PRIMARY KEY (collection, source_path)
        );
        CREATE INDEX IF NOT EXISTS idx_aspect_queue_status
            ON aspect_extraction_queue(status);
    """)
    conn.commit()
    return conn


def _make_catalog_db(path: Path) -> sqlite3.Connection:
    """Create a minimal catalog.db with documents + collections tables."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            tumbler TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            file_path TEXT,
            physical_collection TEXT
        );

        CREATE TABLE IF NOT EXISTS collections (
            name TEXT PRIMARY KEY,
            superseded_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.commit()
    return conn


def _insert_aspect(
    conn: sqlite3.Connection,
    *,
    collection: str,
    source_path: str,
    extracted_at: str = "2026-05-01T00:00:00+00:00",
    model_version: str = "claude-haiku-4-5-20251001",
    extractor_name: str = "scholarly-paper-v1",
) -> None:
    conn.execute(
        "INSERT INTO document_aspects "
        "(collection, source_path, extracted_at, model_version, extractor_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (collection, source_path, extracted_at, model_version, extractor_name),
    )
    conn.commit()


def _insert_queue(
    conn: sqlite3.Connection,
    *,
    collection: str,
    source_path: str,
    status: str = "pending",
    doc_id: str = "",
) -> None:
    conn.execute(
        "INSERT INTO aspect_extraction_queue "
        "(collection, source_path, status, enqueued_at, doc_id) "
        "VALUES (?, ?, ?, '2026-05-01T00:00:00+00:00', ?)",
        (collection, source_path, status, doc_id),
    )
    conn.commit()


def _insert_catalog_doc(
    conn: sqlite3.Connection,
    *,
    tumbler: str,
    file_path: str,
    physical_collection: str,
) -> None:
    conn.execute(
        "INSERT INTO documents (tumbler, title, file_path, physical_collection) "
        "VALUES (?, 'Test Doc', ?, ?)",
        (tumbler, file_path, physical_collection),
    )
    conn.commit()


def _aspect_columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(document_aspects)").fetchall()}


def _queue_columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(aspect_extraction_queue)").fetchall()}


# ── document_aspects PK migration: schema shape ───────────────────────────────


class TestDocumentAspectsPKMigration:
    """The migration produces PRIMARY KEY (doc_id) with denorm cache columns."""

    def test_schema_has_doc_id_as_pk(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        conn = _make_memory_db(mem_db)
        _make_catalog_db(cat_db)

        migrate_document_aspects_pk_to_doc_id(conn, catalog_db_path=cat_db)

        # doc_id must be a column
        cols = _aspect_columns(conn)
        assert "doc_id" in cols

        # Verify doc_id is the primary key via table_info
        pk_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(document_aspects)").fetchall()
            if r[5] == 1  # pk flag
        }
        assert pk_cols == {"doc_id"}
        conn.close()

    def test_denorm_cache_columns_retained(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        conn = _make_memory_db(mem_db)
        _make_catalog_db(cat_db)

        migrate_document_aspects_pk_to_doc_id(conn, catalog_db_path=cat_db)

        cols = _aspect_columns(conn)
        # Denorm cache columns retained
        assert "collection" in cols
        assert "source_path" in cols
        # Existing extraction columns retained
        assert "problem_formulation" in cols
        assert "proposed_method" in cols
        assert "extracted_at" in cols
        assert "model_version" in cols
        assert "extractor_name" in cols
        assert "source_uri" in cols
        conn.close()

    def test_backfill_via_direct_join(self, tmp_path: Path) -> None:
        """Rows matching (collection, file_path) in catalog get doc_id populated."""
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)

        # Seed catalog document
        _insert_catalog_doc(
            cat_conn,
            tumbler="nexus-abc123",
            file_path="/papers/paper1.pdf",
            physical_collection="knowledge__delos",
        )
        cat_conn.close()

        # Seed aspect row matching that catalog doc
        _insert_aspect(
            mem_conn,
            collection="knowledge__delos",
            source_path="/papers/paper1.pdf",
        )

        migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        row = mem_conn.execute(
            "SELECT doc_id, collection, source_path FROM document_aspects"
        ).fetchone()
        assert row is not None
        assert row[0] == "nexus-abc123"
        assert row[1] == "knowledge__delos"  # denorm cache retained
        assert row[2] == "/papers/paper1.pdf"  # denorm cache retained
        mem_conn.close()

    def test_backfill_via_supersede_chain(self, tmp_path: Path) -> None:
        """Rows in a legacy collection that has superseded_by mapping get doc_id
        from the successor collection."""
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)

        # The legacy collection that is superseded
        cat_conn.execute(
            "INSERT INTO collections (name, superseded_by) VALUES (?, ?)",
            ("rdr__legacy-abc123", "rdr__nexus-1-1__voyage-context-3__v1"),
        )
        # Catalog doc is in the NEW collection but same file path
        _insert_catalog_doc(
            cat_conn,
            tumbler="nexus-def456",
            file_path="/rdrs/rdr-001.md",
            physical_collection="rdr__nexus-1-1__voyage-context-3__v1",
        )
        cat_conn.commit()
        cat_conn.close()

        # Aspect row still references the old collection name
        _insert_aspect(
            mem_conn,
            collection="rdr__legacy-abc123",
            source_path="/rdrs/rdr-001.md",
        )

        migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        row = mem_conn.execute(
            "SELECT doc_id FROM document_aspects"
        ).fetchone()
        assert row is not None
        assert row[0] == "nexus-def456"
        mem_conn.close()

    @pytest.mark.parametrize("fixture_collection", [
        "knowledge__cli-test",
        "knowledge__cli-abc",
        "knowledge__nexus-integration-test",
        "knowledge__reproducer",
        "knowledge__pagtest",
        "knowledge__pagend",
    ])
    def test_fixture_rows_hard_deleted(
        self, tmp_path: Path, fixture_collection: str
    ) -> None:
        """Test-fixture collections are hard-deleted, never migrated."""
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        _make_catalog_db(cat_db)

        _insert_aspect(mem_conn, collection=fixture_collection, source_path="/doc.pdf")

        migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        count = mem_conn.execute(
            "SELECT COUNT(*) FROM document_aspects"
        ).fetchone()[0]
        assert count == 0, f"Expected 0 rows, got {count} for fixture {fixture_collection}"
        mem_conn.close()

    def test_high_volume_unmapped_raises_migration_error(self, tmp_path: Path) -> None:
        """Orphan collection with >10 rows and no supersede mapping raises MigrationError."""
        from nexus.db.migrations import MigrationError, migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)
        cat_conn.close()

        # Insert 15 rows in an unmapped legacy collection (no catalog entry, no supersede)
        for i in range(15):
            _insert_aspect(
                mem_conn,
                collection="rdr__nexus-571b8edd",
                source_path=f"/rdrs/rdr-{i:03d}.md",
            )

        with pytest.raises(MigrationError, match="rdr__nexus-571b8edd"):
            migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)
        mem_conn.close()

    def test_low_volume_unmapped_hard_deleted(self, tmp_path: Path) -> None:
        """Unmapped collections with <=10 rows are hard-deleted (acceptable orphan loss)."""
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)

        # Insert a valid in-catalog row (should survive)
        _insert_catalog_doc(
            cat_conn,
            tumbler="nexus-valid01",
            file_path="/papers/valid.pdf",
            physical_collection="knowledge__delos",
        )
        cat_conn.commit()
        cat_conn.close()

        _insert_aspect(mem_conn, collection="knowledge__delos", source_path="/papers/valid.pdf")
        # Insert 3 orphan rows (below threshold — hard delete)
        for i in range(3):
            _insert_aspect(
                mem_conn,
                collection="rdr__some-old-hash",
                source_path=f"/rdrs/orphan-{i}.md",
            )

        migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        # Only the valid row survives
        rows = mem_conn.execute(
            "SELECT doc_id, collection FROM document_aspects ORDER BY collection"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "nexus-valid01"
        assert rows[0][1] == "knowledge__delos"
        mem_conn.close()

    def test_idempotent_on_already_migrated_db(self, tmp_path: Path) -> None:
        """Re-running migration on an already-migrated DB is a no-op (no error, same rows)."""
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)

        _insert_catalog_doc(
            cat_conn,
            tumbler="nexus-idem01",
            file_path="/papers/idem.pdf",
            physical_collection="knowledge__delos",
        )
        cat_conn.commit()
        cat_conn.close()

        _insert_aspect(mem_conn, collection="knowledge__delos", source_path="/papers/idem.pdf")

        migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)
        # Run again — should not raise
        migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        count = mem_conn.execute("SELECT COUNT(*) FROM document_aspects").fetchone()[0]
        assert count == 1
        mem_conn.close()

    def test_no_dangling_attach_after_migration(self, tmp_path: Path) -> None:
        """After migration, no 'cat_db' attached database remains on the connection.

        SQLite always has 'main' and 'temp' in PRAGMA database_list; the
        migration must not leave 'cat_db' (or any other attached user DB)
        behind after it finishes.
        """
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)
        _insert_catalog_doc(
            cat_conn,
            tumbler="nexus-clean01",
            file_path="/papers/clean.pdf",
            physical_collection="knowledge__delos",
        )
        cat_conn.commit()
        cat_conn.close()

        migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        # PRAGMA database_list shows only 'main' (and SQLite's built-in 'temp')
        # No 'cat_db' (or other user-attached names) should remain.
        dbs = {r[1] for r in mem_conn.execute("PRAGMA database_list").fetchall()}
        assert "cat_db" not in dbs
        assert "main" in dbs
        mem_conn.close()

    def test_dedup_keeps_latest_extracted_at(self, tmp_path: Path) -> None:
        """When two old (collection, source_path) rows map to same doc_id,
        keep the row with the latest extracted_at."""
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)

        # Two catalog docs share the same file path but different collections
        # that both map to the same tumbler (via supersede chain)
        cat_conn.execute(
            "INSERT INTO collections (name, superseded_by) VALUES (?, ?)",
            ("knowledge__old", "knowledge__delos"),
        )
        _insert_catalog_doc(
            cat_conn,
            tumbler="nexus-dedup01",
            file_path="/papers/same.pdf",
            physical_collection="knowledge__delos",
        )
        cat_conn.commit()
        cat_conn.close()

        # Row 1: in old collection, earlier extraction
        _insert_aspect(
            mem_conn,
            collection="knowledge__old",
            source_path="/papers/same.pdf",
            extracted_at="2026-04-01T00:00:00+00:00",
            model_version="claude-haiku-4-5-20251001",
        )
        # Row 2: in current collection, later extraction
        _insert_aspect(
            mem_conn,
            collection="knowledge__delos",
            source_path="/papers/same.pdf",
            extracted_at="2026-04-30T00:00:00+00:00",
            model_version="claude-haiku-4-5-20251001",
        )

        migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        rows = mem_conn.execute(
            "SELECT doc_id, extracted_at FROM document_aspects"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "nexus-dedup01"
        assert rows[0][1] == "2026-04-30T00:00:00+00:00"  # latest wins
        mem_conn.close()


# ── aspect_extraction_queue PK migration ─────────────────────────────────────


class TestAspectQueuePKMigration:
    """The queue migration produces PRIMARY KEY (doc_id) with denorm cache columns."""

    def test_schema_has_doc_id_as_pk(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_aspect_extraction_queue_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        conn = _make_memory_db(mem_db)
        _make_catalog_db(cat_db)

        migrate_aspect_extraction_queue_pk_to_doc_id(conn, catalog_db_path=cat_db)

        pk_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(aspect_extraction_queue)").fetchall()
            if r[5] == 1
        }
        assert pk_cols == {"doc_id"}
        conn.close()

    def test_denorm_cache_columns_retained(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_aspect_extraction_queue_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        conn = _make_memory_db(mem_db)
        _make_catalog_db(cat_db)

        migrate_aspect_extraction_queue_pk_to_doc_id(conn, catalog_db_path=cat_db)

        cols = _queue_columns(conn)
        assert "collection" in cols
        assert "source_path" in cols
        assert "status" in cols
        assert "content_hash" in cols
        assert "content" in cols
        conn.close()

    def test_drain_precondition_blocks_on_pending(self, tmp_path: Path) -> None:
        """Migration raises MigrationError when queue has pending rows."""
        from nexus.db.migrations import MigrationError, migrate_aspect_extraction_queue_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        conn = _make_memory_db(mem_db)
        _make_catalog_db(cat_db)

        _insert_queue(conn, collection="knowledge__delos", source_path="/doc.pdf", status="pending")

        with pytest.raises(MigrationError, match="not drained"):
            migrate_aspect_extraction_queue_pk_to_doc_id(conn, catalog_db_path=cat_db)
        conn.close()

    def test_drain_precondition_blocks_on_in_progress(self, tmp_path: Path) -> None:
        """Migration raises MigrationError when queue has in_progress rows."""
        from nexus.db.migrations import MigrationError, migrate_aspect_extraction_queue_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        conn = _make_memory_db(mem_db)
        _make_catalog_db(cat_db)

        _insert_queue(conn, collection="knowledge__delos", source_path="/doc.pdf", status="in_progress")

        with pytest.raises(MigrationError, match="not drained"):
            migrate_aspect_extraction_queue_pk_to_doc_id(conn, catalog_db_path=cat_db)
        conn.close()

    def test_drain_precondition_passes_on_empty_queue(self, tmp_path: Path) -> None:
        """Empty queue: migration proceeds without error."""
        from nexus.db.migrations import migrate_aspect_extraction_queue_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        conn = _make_memory_db(mem_db)
        _make_catalog_db(cat_db)

        # Should not raise
        migrate_aspect_extraction_queue_pk_to_doc_id(conn, catalog_db_path=cat_db)
        conn.close()

    def test_drain_precondition_passes_on_failed_only(self, tmp_path: Path) -> None:
        """Failed rows are inert — migration proceeds normally."""
        from nexus.db.migrations import migrate_aspect_extraction_queue_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)

        _insert_catalog_doc(
            cat_conn,
            tumbler="nexus-fail01",
            file_path="/papers/fail.pdf",
            physical_collection="knowledge__delos",
        )
        cat_conn.commit()
        cat_conn.close()

        _insert_queue(
            conn,
            collection="knowledge__delos",
            source_path="/papers/fail.pdf",
            status="failed",
        )

        # Should not raise
        migrate_aspect_extraction_queue_pk_to_doc_id(conn, catalog_db_path=cat_db)
        conn.close()

    def test_backfill_via_direct_join(self, tmp_path: Path) -> None:
        """Queue rows matching (collection, source_path) in catalog get doc_id populated."""
        from nexus.db.migrations import migrate_aspect_extraction_queue_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)

        _insert_catalog_doc(
            cat_conn,
            tumbler="nexus-qrow01",
            file_path="/papers/qrow.pdf",
            physical_collection="knowledge__delos",
        )
        cat_conn.commit()
        cat_conn.close()

        _insert_queue(
            mem_conn,
            collection="knowledge__delos",
            source_path="/papers/qrow.pdf",
            status="failed",  # only failed rows may exist (drain precondition)
        )

        migrate_aspect_extraction_queue_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        row = mem_conn.execute(
            "SELECT doc_id, collection, source_path FROM aspect_extraction_queue"
        ).fetchone()
        assert row is not None
        assert row[0] == "nexus-qrow01"
        assert row[1] == "knowledge__delos"
        assert row[2] == "/papers/qrow.pdf"
        mem_conn.close()

    def test_idempotent_on_already_migrated_db(self, tmp_path: Path) -> None:
        """Re-running queue migration on an already-migrated DB is a no-op."""
        from nexus.db.migrations import migrate_aspect_extraction_queue_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        conn = _make_memory_db(mem_db)
        _make_catalog_db(cat_db)

        migrate_aspect_extraction_queue_pk_to_doc_id(conn, catalog_db_path=cat_db)
        migrate_aspect_extraction_queue_pk_to_doc_id(conn, catalog_db_path=cat_db)
        conn.close()

    def test_no_dangling_attach_after_migration(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_aspect_extraction_queue_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        conn = _make_memory_db(mem_db)
        _make_catalog_db(cat_db)

        migrate_aspect_extraction_queue_pk_to_doc_id(conn, catalog_db_path=cat_db)

        dbs = {r[1] for r in conn.execute("PRAGMA database_list").fetchall()}
        assert "cat_db" not in dbs
        assert "main" in dbs
        conn.close()


# ── AspectExtractionQueue post-migration accessor API ─────────────────────────


class TestQueuePostMigrationAPI:
    """After migration, mark_done(doc_id) and claim_next returning doc_id."""

    @pytest.fixture()
    def migrated_queue_path(self, tmp_path: Path) -> Path:
        """Return path to a migrated aspect_extraction_queue DB."""
        from nexus.db.migrations import migrate_aspect_extraction_queue_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)
        _insert_catalog_doc(
            cat_conn,
            tumbler="nexus-api01",
            file_path="/papers/api.pdf",
            physical_collection="knowledge__delos",
        )
        cat_conn.commit()
        cat_conn.close()
        migrate_aspect_extraction_queue_pk_to_doc_id(conn, catalog_db_path=cat_db)
        conn.close()
        return mem_db

    def test_enqueue_and_claim_returns_doc_id(self, migrated_queue_path: Path) -> None:
        """After migration, enqueue + claim_next returns a QueueRow with doc_id populated."""
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        q = AspectExtractionQueue(migrated_queue_path)
        q.enqueue(
            "knowledge__delos",
            "/papers/api.pdf",
            doc_id="nexus-api01",
        )
        row = q.claim_next()
        assert row is not None
        assert row.doc_id == "nexus-api01"
        q.close()

    def test_mark_done_by_doc_id(self, migrated_queue_path: Path) -> None:
        """mark_done(doc_id=...) deletes the row by doc_id on a migrated table."""
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        q = AspectExtractionQueue(migrated_queue_path)
        q.enqueue(
            "knowledge__delos",
            "/papers/api.pdf",
            doc_id="nexus-api01",
        )
        row = q.claim_next()
        assert row is not None
        deleted = q.mark_done(doc_id="nexus-api01")
        assert deleted == 1

        # Queue should now be empty
        assert q.pending_count() == 0
        q.close()

    def test_mark_done_legacy_collection_source_path(self, migrated_queue_path: Path) -> None:
        """mark_done(collection, source_path) still works after migration (backward compat)."""
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        q = AspectExtractionQueue(migrated_queue_path)
        q.enqueue(
            "knowledge__delos",
            "/papers/api.pdf",
            doc_id="nexus-api01",
        )
        q.claim_next()
        # Legacy call form
        deleted = q.mark_done(collection="knowledge__delos", source_path="/papers/api.pdf")
        assert deleted == 1
        q.close()


# ── DocumentAspects post-migration accessor API ───────────────────────────────


class TestDocumentAspectsPostMigrationAPI:
    """After migration, get_by_doc_id works; existing API still usable."""

    @pytest.fixture()
    def migrated_aspects_path(self, tmp_path: Path) -> Path:
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id
        from nexus.db.t2.document_aspects import AspectRecord

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)
        _insert_catalog_doc(
            cat_conn,
            tumbler="nexus-asp01",
            file_path="/papers/asp.pdf",
            physical_collection="knowledge__delos",
        )
        cat_conn.commit()
        cat_conn.close()

        _insert_aspect(conn, collection="knowledge__delos", source_path="/papers/asp.pdf")
        migrate_document_aspects_pk_to_doc_id(conn, catalog_db_path=cat_db)
        conn.close()
        return mem_db

    def test_get_by_doc_id(self, migrated_aspects_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(migrated_aspects_path)
        record = store.get_by_doc_id("nexus-asp01")
        assert record is not None
        assert record.collection == "knowledge__delos"
        store.close()

    def test_upsert_by_doc_id(self, migrated_aspects_path: Path) -> None:
        from nexus.db.t2.document_aspects import AspectRecord, DocumentAspects

        store = DocumentAspects(migrated_aspects_path)
        record = AspectRecord(
            collection="knowledge__delos",
            source_path="/papers/new.pdf",
            doc_id="nexus-new01",
            problem_formulation="Test",
            proposed_method="Test method",
            # nexus-17wf: confidence floor-gated (>=0.3); pass an
            # explicit value so the upsert lands. Test exercises
            # doc_id PK round-trip, not confidence semantics.
            confidence=0.9,
            extracted_at="2026-05-09T00:00:00+00:00",
            model_version="claude-haiku-4-5-20251001",
            extractor_name="scholarly-paper-v1",
        )
        store.upsert(record)
        fetched = store.get_by_doc_id("nexus-new01")
        assert fetched is not None
        assert fetched.problem_formulation == "Test"
        store.close()


# ── MIGRATIONS list registration ──────────────────────────────────────────────


class TestMigrationsListRegistration:
    """Both migrations appear in the MIGRATIONS list at version 4.30.0."""

    def test_document_aspects_migration_registered(self) -> None:
        from nexus.db.migrations import MIGRATIONS

        names = [m.name for m in MIGRATIONS]
        assert any("document_aspects" in n and "doc_id" in n for n in names), (
            f"document_aspects PK migration not found in MIGRATIONS: {names}"
        )

    def test_aspect_queue_migration_registered(self) -> None:
        from nexus.db.migrations import MIGRATIONS

        names = [m.name for m in MIGRATIONS]
        assert any("aspect_extraction_queue" in n and "doc_id" in n for n in names), (
            f"aspect_extraction_queue PK migration not found in MIGRATIONS: {names}"
        )

    def test_both_at_version_4_30_0(self) -> None:
        from nexus.db.migrations import MIGRATIONS

        migrated = [
            m for m in MIGRATIONS
            if "doc_id" in m.name and (
                "document_aspects" in m.name or "aspect_extraction_queue" in m.name
            )
        ]
        assert len(migrated) == 2
        for m in migrated:
            assert m.introduced == "4.30.0", (
                f"Migration {m.name!r} has version {m.introduced!r}, expected '4.30.0'"
            )
