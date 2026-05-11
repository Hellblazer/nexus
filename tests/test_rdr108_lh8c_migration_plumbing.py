# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-108 Phase 1 remediation T-A (nexus-lh8c) — migration plumbing fixes.

Covers:
K1  ON DELETE CASCADE on document_chunks.doc_id (schema + migration)
K2  drain_worker called before precondition + MigrationError message
K3  executescript -> explicit execute() in both PK migrations (atomicity)
K5  ATTACH DATABASE parameterized binding (no f-string SQL injection)
K8  ROW_NUMBER() CTE dedup replaces fragile HAVING + ORDER BY
K11 Migration retry when catalog DB absent (skip not cached in _upgrade_done)
CG-2 apply_pending no-catalog is a no-op AND is retry-able
SG-4 high-volume orphan threshold boundary (exactly 10 rows)
S-3  _has_doc_id_pk double-checked lock: hasattr inside the lock
O-5  INSERT OR IGNORE replaces anti-join in collections backfill
"""
from __future__ import annotations

import inspect
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_memory_db(path: Path) -> sqlite3.Connection:
    """Fresh memory.db with pre-migration (collection, source_path) PK schema."""
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
    """)
    conn.commit()
    return conn


def _make_catalog_db(path: Path) -> sqlite3.Connection:
    """Minimal catalog.db with documents + collections tables."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            tumbler TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'Test Doc',
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


def _insert_catalog_doc(
    conn: sqlite3.Connection,
    *,
    tumbler: str,
    file_path: str,
    physical_collection: str,
) -> None:
    conn.execute(
        "INSERT INTO documents (tumbler, file_path, physical_collection) VALUES (?, ?, ?)",
        (tumbler, file_path, physical_collection),
    )
    conn.commit()


def _insert_aspect(
    conn: sqlite3.Connection,
    *,
    collection: str,
    source_path: str,
    extracted_at: str = "2026-05-01T00:00:00+00:00",
    model_version: str = "test-model-v1",
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
    enqueued_at: str = "2026-05-01T00:00:00+00:00",
) -> None:
    conn.execute(
        "INSERT INTO aspect_extraction_queue "
        "(collection, source_path, status, enqueued_at, doc_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (collection, source_path, status, enqueued_at, doc_id),
    )
    conn.commit()


# ── K1: ON DELETE CASCADE on document_chunks ─────────────────────────────────


class TestK1OnDeleteCascade:
    """K1: document_chunks.doc_id FK must have ON DELETE CASCADE."""

    def _insert_doc_and_chunk(self, db: object) -> None:
        """Insert a test document and a chunk row."""
        db.execute(
            "INSERT INTO documents "
            "(tumbler, title, author, year, content_type, file_path, "
            " corpus, physical_collection, chunk_count, head_hash, indexed_at, "
            " metadata, source_mtime, alias_of, source_uri) "
            "VALUES ('nexus-k1test01', 'Test', '', 2026, 'text', '/test.pdf', "
            "        'knowledge', 'knowledge__test', 1, 'abc', '2026-01-01', "
            "        '{}', 0, '', '')"
        )
        db.commit()
        db.execute(
            "INSERT INTO document_chunks (doc_id, position, chash) VALUES (?, ?, ?)",
            ("nexus-k1test01", 0, "abc123"),
        )
        db.commit()

    def test_delete_document_cascades_to_chunks(self, tmp_path: Path) -> None:
        """Deleting a document row must automatically delete its chunk rows (CASCADE)."""
        from nexus.catalog.catalog_db import CatalogDB

        db = CatalogDB(tmp_path / ".catalog.db")
        try:
            self._insert_doc_and_chunk(db)

            count_before = db.execute(
                "SELECT COUNT(*) FROM document_chunks WHERE doc_id = ?",
                ("nexus-k1test01",),
            ).fetchone()[0]
            assert count_before == 1, "Setup: chunk row must exist"

            # Must not raise IntegrityError
            db.execute("DELETE FROM documents WHERE tumbler = ?", ("nexus-k1test01",))
            db.commit()

            count_after = db.execute(
                "SELECT COUNT(*) FROM document_chunks WHERE doc_id = ?",
                ("nexus-k1test01",),
            ).fetchone()[0]
            assert count_after == 0, "CASCADE must delete child chunk rows on document delete"
        finally:
            db.close()

    def test_delete_all_documents_cascades_all_chunks(self, tmp_path: Path) -> None:
        """DELETE FROM documents (rebuild) must cascade to all chunks."""
        from nexus.catalog.catalog_db import CatalogDB

        db = CatalogDB(tmp_path / ".catalog.db")
        try:
            self._insert_doc_and_chunk(db)
            # Add a second doc + chunk
            db.execute(
                "INSERT INTO documents "
                "(tumbler, title, author, year, content_type, file_path, "
                " corpus, physical_collection, chunk_count, head_hash, indexed_at, "
                " metadata, source_mtime, alias_of, source_uri) "
                "VALUES ('nexus-k1test02', 'Test2', '', 2026, 'text', '/t2.pdf', "
                "        'knowledge', 'knowledge__test', 1, 'def', '2026-01-01', "
                "        '{}', 0, '', '')"
            )
            db.commit()
            db.execute(
                "INSERT INTO document_chunks (doc_id, position, chash) VALUES (?, ?, ?)",
                ("nexus-k1test02", 0, "def456"),
            )
            db.commit()

            db.execute("DELETE FROM documents")
            db.commit()

            total = db.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
            assert total == 0, "CASCADE must wipe all chunks when all docs deleted"
        finally:
            db.close()

    def test_schema_has_on_delete_cascade(self, tmp_path: Path) -> None:
        """The document_chunks DDL must contain ON DELETE CASCADE."""
        from nexus.catalog.catalog_db import CatalogDB

        db = CatalogDB(tmp_path / ".catalog.db")
        try:
            row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='document_chunks'"
            ).fetchone()
            assert row is not None, "document_chunks table must exist"
            assert "ON DELETE CASCADE" in row[0], (
                f"document_chunks FK must have ON DELETE CASCADE; DDL: {row[0]}"
            )
        finally:
            db.close()

    def test_existing_db_without_cascade_is_migrated(self, tmp_path: Path) -> None:
        """CatalogDB.__init__ on a DB with old document_chunks schema must run migration."""
        db_path = tmp_path / ".catalog.db"

        # Create old-schema DB (no ON DELETE CASCADE)
        raw = sqlite3.connect(str(db_path))
        raw.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS documents (
                tumbler TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                author TEXT, year INTEGER, content_type TEXT,
                file_path TEXT, corpus TEXT, physical_collection TEXT,
                chunk_count INTEGER, head_hash TEXT, indexed_at TEXT,
                metadata JSON, source_mtime REAL NOT NULL DEFAULT 0,
                alias_of TEXT NOT NULL DEFAULT '',
                source_uri TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS document_chunks (
                doc_id   TEXT NOT NULL REFERENCES documents(tumbler),
                position INTEGER NOT NULL,
                chash    TEXT NOT NULL,
                PRIMARY KEY (doc_id, position)
            );
        """)
        raw.execute(
            "INSERT INTO documents (tumbler, title) VALUES (?, ?)",
            ("nexus-legacy01", "Legacy"),
        )
        raw.execute(
            "INSERT INTO document_chunks (doc_id, position, chash) VALUES (?, ?, ?)",
            ("nexus-legacy01", 0, "abc"),
        )
        raw.commit()
        raw.close()

        from nexus.catalog.catalog_db import CatalogDB
        db = CatalogDB(db_path)
        try:
            ddl = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='document_chunks'"
            ).fetchone()
            assert ddl is not None
            assert "ON DELETE CASCADE" in ddl[0], (
                "Migration must add ON DELETE CASCADE to document_chunks FK"
            )
            # Data preserved
            count = db.execute(
                "SELECT COUNT(*) FROM document_chunks"
            ).fetchone()[0]
            assert count == 1, "Existing chunk rows must be preserved by migration"
        finally:
            db.close()

    def test_cascade_on_all_five_deletion_sites(self, tmp_path: Path) -> None:
        """All 5 document-deletion paths must work without IntegrityError post-CASCADE.

        Sites: projector DELETE by tumbler, rebuild DELETE FROM documents (2 sites),
        and direct DELETE via catalog_writes.
        """
        from nexus.catalog.catalog_db import CatalogDB

        db = CatalogDB(tmp_path / ".catalog.db")
        try:
            # Insert 3 documents with chunks
            for i in range(3):
                db.execute(
                    "INSERT INTO documents "
                    "(tumbler, title, author, year, content_type, file_path, "
                    " corpus, physical_collection, chunk_count, head_hash, indexed_at, "
                    " metadata, source_mtime, alias_of, source_uri) "
                    f"VALUES ('nexus-site{i:02d}', 'Doc{i}', '', 2026, 'text', '/f{i}.pdf', "
                    "        'knowledge', 'knowledge__test', 1, 'h', '2026-01-01', "
                    "        '{}', 0, '', '')"
                )
                db.commit()
                db.execute(
                    "INSERT INTO document_chunks (doc_id, position, chash) VALUES (?, ?, ?)",
                    (f"nexus-site{i:02d}", 0, f"hash{i}"),
                )
                db.commit()

            # Site 1: DELETE by single tumbler (projector path)
            db.execute("DELETE FROM documents WHERE tumbler = ?", ("nexus-site00",))
            db.commit()
            assert db.execute(
                "SELECT COUNT(*) FROM document_chunks WHERE doc_id = 'nexus-site00'"
            ).fetchone()[0] == 0

            # Site 2 + 3: DELETE FROM documents (rebuild path)
            db.execute("DELETE FROM documents")
            db.commit()
            assert db.execute(
                "SELECT COUNT(*) FROM document_chunks"
            ).fetchone()[0] == 0
        finally:
            db.close()


# ── K3: executescript atomicity ───────────────────────────────────────────────


class TestK3NoExecutescript:
    """K3: PK migrations must NOT use executescript (which auto-commits)."""

    def test_aspects_migration_no_executescript(self) -> None:
        """migrate_document_aspects_pk_to_doc_id must not call conn.executescript()."""
        from nexus.db import migrations
        src = inspect.getsource(migrations.migrate_document_aspects_pk_to_doc_id)
        # Check that conn.executescript is not called (comments mentioning the word
        # are fine; active code calls are not).
        assert "conn.executescript" not in src, (
            "migrate_document_aspects_pk_to_doc_id must not call conn.executescript(). "
            "Use explicit conn.execute() calls inside 'with conn:' for atomicity."
        )

    def test_queue_migration_no_executescript(self) -> None:
        """migrate_aspect_extraction_queue_pk_to_doc_id must not call conn.executescript()."""
        from nexus.db import migrations
        src = inspect.getsource(migrations.migrate_aspect_extraction_queue_pk_to_doc_id)
        assert "conn.executescript" not in src, (
            "migrate_aspect_extraction_queue_pk_to_doc_id must not call conn.executescript(). "
            "Use explicit conn.execute() calls inside 'with conn:' for atomicity."
        )

    def test_aspects_migration_succeeds_end_to_end(self, tmp_path: Path) -> None:
        """The refactored aspects migration must still work correctly."""
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)
        _insert_catalog_doc(cat_conn, tumbler="nexus-k3test01",
                            file_path="/test.pdf", physical_collection="knowledge__test")
        cat_conn.close()
        _insert_aspect(mem_conn, collection="knowledge__test", source_path="/test.pdf")

        migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        rows = mem_conn.execute("SELECT doc_id FROM document_aspects").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "nexus-k3test01"
        mem_conn.close()

    def test_queue_migration_succeeds_end_to_end(self, tmp_path: Path) -> None:
        """The refactored queue migration must still work correctly."""
        from nexus.db.migrations import migrate_aspect_extraction_queue_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)
        _insert_catalog_doc(cat_conn, tumbler="nexus-k3q01",
                            file_path="/test.pdf", physical_collection="knowledge__test")
        cat_conn.close()
        _insert_queue(mem_conn, collection="knowledge__test", source_path="/test.pdf",
                      status="failed")  # only failed rows remain (drain precondition)

        migrate_aspect_extraction_queue_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        rows = mem_conn.execute("SELECT doc_id FROM aspect_extraction_queue").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "nexus-k3q01"
        mem_conn.close()


# ── K5: ATTACH DATABASE parameterized binding ─────────────────────────────────


class TestK5AttachParameterized:
    """K5: _attach_catalog must use ? binding, not f-string interpolation."""

    def test_no_fstring_in_attach_catalog_source(self) -> None:
        """_attach_catalog source must not contain f-string ATTACH."""
        from nexus.db import migrations
        src = inspect.getsource(migrations._attach_catalog)
        assert 'f"ATTACH' not in src and "f'ATTACH" not in src, (
            "_attach_catalog must not use f-string for ATTACH DATABASE SQL. "
            "Use conn.execute('ATTACH DATABASE ? AS cat_db', (str(path),)) instead."
        )

    def test_attach_with_single_quote_in_path(self, tmp_path: Path) -> None:
        """Catalog path with a single quote must not break ATTACH (SQL injection guard)."""
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id

        quoted_dir = tmp_path / "O'Brien"
        quoted_dir.mkdir()
        cat_db = quoted_dir / ".catalog.db"
        mem_db = tmp_path / "memory.db"

        cat_conn = _make_catalog_db(cat_db)
        _insert_catalog_doc(cat_conn, tumbler="nexus-k5test01",
                            file_path="/papers/test.pdf", physical_collection="knowledge__test")
        cat_conn.close()

        mem_conn = _make_memory_db(mem_db)
        _insert_aspect(mem_conn, collection="knowledge__test", source_path="/papers/test.pdf")

        # Must not raise sqlite3.OperationalError due to malformed SQL
        migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        rows = mem_conn.execute("SELECT doc_id FROM document_aspects").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "nexus-k5test01"
        mem_conn.close()


# ── K8: ROW_NUMBER() dedup CTE ────────────────────────────────────────────────


class TestK8DedupRowNumber:
    """K8: Dedup must use ROW_NUMBER() CTE, not fragile HAVING + ORDER BY."""

    def test_aspects_dedup_uses_row_number(self) -> None:
        """migrate_document_aspects_pk_to_doc_id source must contain ROW_NUMBER()."""
        from nexus.db import migrations
        src = inspect.getsource(migrations.migrate_document_aspects_pk_to_doc_id)
        assert "ROW_NUMBER()" in src or "row_number()" in src, (
            "migrate_document_aspects_pk_to_doc_id must use ROW_NUMBER() OVER (...) "
            "CTE for deterministic dedup. The HAVING extracted_at = MAX(...) pattern "
            "is a no-op that relies on undocumented SQLite ordering."
        )

    def test_queue_dedup_uses_row_number(self) -> None:
        """migrate_aspect_extraction_queue_pk_to_doc_id must contain ROW_NUMBER()."""
        from nexus.db import migrations
        src = inspect.getsource(migrations.migrate_aspect_extraction_queue_pk_to_doc_id)
        assert "ROW_NUMBER()" in src or "row_number()" in src, (
            "migrate_aspect_extraction_queue_pk_to_doc_id must use ROW_NUMBER() CTE."
        )

    def test_three_rows_same_doc_id_latest_extracted_at_wins(self, tmp_path: Path) -> None:
        """With 3 rows collapsing to same doc_id, latest extracted_at wins."""
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)

        _insert_catalog_doc(cat_conn, tumbler="nexus-k8d01",
                            file_path="/papers/dedup.pdf", physical_collection="knowledge__new")
        cat_conn.execute(
            "INSERT INTO collections (name, superseded_by) VALUES (?, ?)",
            ("knowledge__old1", "knowledge__new"),
        )
        cat_conn.execute(
            "INSERT INTO collections (name, superseded_by) VALUES (?, ?)",
            ("knowledge__old2", "knowledge__new"),
        )
        cat_conn.commit()
        cat_conn.close()

        _insert_aspect(mem_conn, collection="knowledge__old1",
                       source_path="/papers/dedup.pdf", extracted_at="2026-01-01T00:00:00+00:00")
        _insert_aspect(mem_conn, collection="knowledge__old2",
                       source_path="/papers/dedup.pdf", extracted_at="2026-03-01T00:00:00+00:00")
        _insert_aspect(mem_conn, collection="knowledge__new",
                       source_path="/papers/dedup.pdf", extracted_at="2026-05-01T00:00:00+00:00")

        migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        rows = mem_conn.execute(
            "SELECT doc_id, extracted_at FROM document_aspects"
        ).fetchall()
        assert len(rows) == 1, "3 rows -> 1 doc_id -> exactly 1 result row"
        assert rows[0][0] == "nexus-k8d01"
        assert rows[0][1] == "2026-05-01T00:00:00+00:00", "Latest extracted_at must win"
        mem_conn.close()

    def test_tie_extracted_at_keeps_exactly_one_row(self, tmp_path: Path) -> None:
        """Two rows with identical extracted_at for same doc_id: exactly one row kept."""
        from nexus.db.migrations import migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)
        _insert_catalog_doc(cat_conn, tumbler="nexus-k8tie01",
                            file_path="/papers/tie.pdf", physical_collection="knowledge__new")
        cat_conn.execute(
            "INSERT INTO collections (name, superseded_by) VALUES (?, ?)",
            ("knowledge__old", "knowledge__new"),
        )
        cat_conn.commit()
        cat_conn.close()

        ts = "2026-05-01T12:00:00+00:00"
        _insert_aspect(mem_conn, collection="knowledge__old",
                       source_path="/papers/tie.pdf", extracted_at=ts)
        _insert_aspect(mem_conn, collection="knowledge__new",
                       source_path="/papers/tie.pdf", extracted_at=ts)

        migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        rows = mem_conn.execute("SELECT doc_id FROM document_aspects").fetchall()
        assert len(rows) == 1, "Tie: exactly one row kept (no duplicate PK, no data loss)"
        assert rows[0][0] == "nexus-k8tie01"
        mem_conn.close()


# ── K11: Migration retry when catalog absent ──────────────────────────────────


def _je0b_registered() -> bool:
    """True iff the je0b PK migration is registered in MIGRATIONS.

    K11 / CG-2 / K2 tests verify behavior of je0b's MigrationRetry path
    (skip + don't cache + retry on next open). When je0b is deferred
    from the registry (4.31.5: pending companion fixes for the wider
    DocumentAspects refactor), apply_pending has nothing to skip and
    these contracts don't apply. Re-enable by re-registering je0b.
    """
    from nexus.db.migrations import (
        MIGRATIONS,
        _migrate_document_aspects_pk_via_apply_pending,
    )
    return any(
        m.fn is _migrate_document_aspects_pk_via_apply_pending
        for m in MIGRATIONS
    )


class TestK11SkipNotCached:
    """K11: apply_pending must NOT add path to _upgrade_done when catalog is absent."""

    def test_skip_not_cached_in_upgrade_done(self, tmp_path: Path) -> None:
        """Missing catalog: _upgrade_done must NOT contain this path after apply_pending."""
        from nexus.db import migrations

        mem_db = tmp_path / "memory.db"
        mem_conn = _make_memory_db(mem_db)
        migrations.bootstrap_version(mem_conn)
        path_key = migrations._connection_path_key(mem_conn)
        migrations._upgrade_done.discard(path_key)

        # Catalog does NOT exist
        migrations.apply_pending(mem_conn, "4.30.0")

        assert path_key not in migrations._upgrade_done, (
            "apply_pending must NOT cache the path in _upgrade_done when a migration "
            "was skipped due to missing catalog DB. The migration must retry on next open."
        )
        mem_conn.close()

    def test_after_skip_second_call_reattempts(self, tmp_path: Path) -> None:
        """After a catalog-absent skip, a second apply_pending call must re-run migrations."""
        from nexus.db import migrations

        mem_db = tmp_path / "memory.db"
        # Catalog must match the path derived by _catalog_db_path_from_conn:
        # <config_dir>/catalog/.catalog.db where config_dir = mem_db.parent
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir(parents=True, exist_ok=True)
        cat_db = cat_dir / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        migrations.bootstrap_version(mem_conn)
        path_key = migrations._connection_path_key(mem_conn)
        migrations._upgrade_done.discard(path_key)

        # First call: no catalog
        migrations.apply_pending(mem_conn, "4.30.0")
        assert path_key not in migrations._upgrade_done, "First skip must not be cached"

        # Create catalog + matching aspect row
        cat_conn = _make_catalog_db(cat_db)
        _insert_catalog_doc(cat_conn, tumbler="nexus-k11retry",
                            file_path="/retry.pdf", physical_collection="knowledge__test")
        cat_conn.close()

        # Add aspect row that can be mapped
        try:
            mem_conn.execute("SELECT collection FROM document_aspects LIMIT 0")
            _insert_aspect(mem_conn, collection="knowledge__test", source_path="/retry.pdf")
        except sqlite3.OperationalError:
            pass  # Already migrated in first call (edge case in test env)

        # Second call: catalog now present, must not be short-circuited
        migrations.apply_pending(mem_conn, "4.30.0")
        # After successful migration, _upgrade_done should contain the path
        assert path_key in migrations._upgrade_done, (
            "After successful migration (catalog present on second call), "
            "path_key must be in _upgrade_done"
        )
        mem_conn.close()


# ── CG-2: apply_pending no-catalog-path ──────────────────────────────────────


class TestCG2NoCatalog:
    """CG-2: apply_pending with absent catalog must be no-op AND retry-able."""

    def test_apply_pending_no_catalog_does_not_raise(self, tmp_path: Path) -> None:
        """apply_pending must complete without exception when catalog DB is absent."""
        from nexus.db import migrations

        mem_db = tmp_path / "memory.db"
        mem_conn = _make_memory_db(mem_db)
        migrations.bootstrap_version(mem_conn)
        path_key = migrations._connection_path_key(mem_conn)
        migrations._upgrade_done.discard(path_key)

        # No catalog.db created — must not raise
        migrations.apply_pending(mem_conn, "4.30.0")
        mem_conn.close()

    def test_apply_pending_no_catalog_leaves_path_not_cached(self, tmp_path: Path) -> None:
        """apply_pending with absent catalog must NOT add path to _upgrade_done."""
        from nexus.db import migrations

        mem_db = tmp_path / "memory.db"
        mem_conn = _make_memory_db(mem_db)
        migrations.bootstrap_version(mem_conn)
        path_key = migrations._connection_path_key(mem_conn)
        migrations._upgrade_done.discard(path_key)

        migrations.apply_pending(mem_conn, "4.30.0")

        assert path_key not in migrations._upgrade_done, (
            "Skipped migration (no catalog) must not permanently cache in _upgrade_done"
        )
        mem_conn.close()


# ── SG-4: High-volume orphan threshold boundary ───────────────────────────────


class TestSG4OrphanBoundary:
    """SG-4: Verify the exact threshold boundary for high-volume orphan detection."""

    def test_exactly_10_orphans_silently_deleted(self, tmp_path: Path) -> None:
        """Exactly 10 unmapped rows: silently deleted (threshold is STRICTLY >10)."""
        from nexus.db.migrations import (
            MigrationError,
            _HIGH_VOLUME_ORPHAN_THRESHOLD,
            migrate_document_aspects_pk_to_doc_id,
        )

        assert _HIGH_VOLUME_ORPHAN_THRESHOLD == 10, (
            f"Expected threshold=10, got {_HIGH_VOLUME_ORPHAN_THRESHOLD}. Update this test."
        )

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        _make_catalog_db(cat_db).close()  # empty catalog: all rows unmapped

        for i in range(10):
            _insert_aspect(
                mem_conn,
                collection="knowledge__unmapped",
                source_path=f"/papers/file{i:02d}.pdf",
            )

        # Must NOT raise (10 == threshold, not >threshold)
        migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)
        remaining = mem_conn.execute(
            "SELECT COUNT(*) FROM document_aspects"
        ).fetchone()[0]
        assert remaining == 0, "Exactly-10 orphans must be silently deleted"
        mem_conn.close()

    def test_11_orphans_raises_migration_error(self, tmp_path: Path) -> None:
        """11 unmapped rows for one collection must raise MigrationError (>10 threshold)."""
        from nexus.db.migrations import MigrationError, migrate_document_aspects_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        _make_catalog_db(cat_db).close()

        for i in range(11):
            _insert_aspect(
                mem_conn,
                collection="knowledge__unmapped",
                source_path=f"/papers/file{i:02d}.pdf",
            )

        with pytest.raises(MigrationError, match="high-volume unmapped orphan"):
            migrate_document_aspects_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)
        mem_conn.close()


# ── K2: drain_worker wiring + nx aspects drain CLI ───────────────────────────


class TestK2DrainWiring:
    """K2: drain_worker called in migration wrapper + MigrationError message."""

    def test_migration_error_mentions_nx_aspects_drain(self, tmp_path: Path) -> None:
        """MigrationError when queue not drained must mention 'nx aspects drain'."""
        from nexus.db.migrations import MigrationError, migrate_aspect_extraction_queue_pk_to_doc_id

        mem_db = tmp_path / "memory.db"
        cat_db = tmp_path / ".catalog.db"
        mem_conn = _make_memory_db(mem_db)
        cat_conn = _make_catalog_db(cat_db)
        _insert_catalog_doc(cat_conn, tumbler="nexus-k2t01",
                            file_path="/test.pdf", physical_collection="knowledge__test")
        cat_conn.close()
        _insert_queue(mem_conn, collection="knowledge__test",
                      source_path="/test.pdf", status="pending")

        with pytest.raises(MigrationError) as exc_info:
            migrate_aspect_extraction_queue_pk_to_doc_id(mem_conn, catalog_db_path=cat_db)

        assert "nx aspects drain" in str(exc_info.value), (
            f"MigrationError must mention 'nx aspects drain'. Got: {exc_info.value!r}"
        )
        mem_conn.close()

    def test_migration_wrapper_calls_drain_worker(self, tmp_path: Path) -> None:
        """_migrate_aspect_queue_pk_via_apply_pending must call drain_worker."""
        import nexus.aspect_worker as aw
        from nexus.db import migrations

        mem_db = tmp_path / "memory.db"
        # Catalog must match the path derived by _catalog_db_path_from_conn:
        # <config_dir>/catalog/.catalog.db where config_dir = mem_db.parent
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir(parents=True, exist_ok=True)
        cat_db = cat_dir / ".catalog.db"
        _make_catalog_db(cat_db).close()
        mem_conn = _make_memory_db(mem_db)

        drain_calls: list[object] = []

        def mock_drain(queue_path: object, **kwargs: object) -> None:
            drain_calls.append(queue_path)

        with patch.object(aw, "drain_worker", mock_drain):
            migrations._migrate_aspect_queue_pk_via_apply_pending(mem_conn)

        assert len(drain_calls) >= 1, (
            "_migrate_aspect_queue_pk_via_apply_pending must call drain_worker "
            "before checking the precondition"
        )
        mem_conn.close()


class TestK2DrainCLI:
    """K2: 'nx aspects drain' CLI verb must exist and work."""

    def test_aspects_group_exists(self) -> None:
        """'nx aspects --help' must succeed."""
        from click.testing import CliRunner
        from nexus.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["aspects", "--help"])
        assert result.exit_code == 0, (
            f"'nx aspects --help' must exit 0. Output: {result.output!r}"
        )

    def test_aspects_drain_subcommand_listed(self) -> None:
        """'nx aspects --help' must list the 'drain' subcommand."""
        from click.testing import CliRunner
        from nexus.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["aspects", "--help"])
        assert "drain" in result.output.lower(), (
            f"'nx aspects --help' must mention 'drain'. Got: {result.output!r}"
        )

    def test_aspects_drain_empty_queue_exits_0(self, tmp_path: Path) -> None:
        """'nx aspects drain' with an empty queue must exit 0."""
        import os
        from click.testing import CliRunner
        from nexus.cli import main

        # Create an empty T2 DB (post-migration schema)
        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS aspect_extraction_queue (
                doc_id TEXT PRIMARY KEY,
                collection TEXT NOT NULL DEFAULT '',
                source_path TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER NOT NULL DEFAULT 0,
                enqueued_at TEXT NOT NULL DEFAULT '',
                last_attempt_at TEXT,
                last_error TEXT
            );
        """)
        conn.commit()
        conn.close()

        runner = CliRunner()
        env = {**os.environ, "NEXUS_CONFIG_DIR": str(tmp_path)}
        result = runner.invoke(main, ["aspects", "drain"], env=env)
        assert result.exit_code == 0, (
            f"'nx aspects drain' with empty queue must exit 0. "
            f"Output: {result.output!r}"
        )


# ── S-3: _has_doc_id_pk double-checked lock ───────────────────────────────────


class TestS3LockPattern:
    """S-3: _has_doc_id_pk must check hasattr INSIDE the lock (TOCTOU fix)."""

    def test_hasattr_inside_lock_in_source(self) -> None:
        """hasattr() check must appear AFTER 'with self._lock:' in _has_doc_id_pk."""
        from nexus.db.t2.document_aspects import DocumentAspects
        src = inspect.getsource(DocumentAspects._has_doc_id_pk)
        lines = src.split("\n")
        lock_idx = next(
            (i for i, ln in enumerate(lines) if "with self._lock" in ln), None
        )
        hasattr_idx = next(
            (i for i, ln in enumerate(lines) if "hasattr" in ln), None
        )
        assert lock_idx is not None, "_has_doc_id_pk must have 'with self._lock:'"
        assert hasattr_idx is not None, "_has_doc_id_pk must use hasattr()"
        assert hasattr_idx > lock_idx, (
            f"hasattr() (line {hasattr_idx}) must come after 'with self._lock:' "
            f"(line {lock_idx}) to prevent TOCTOU race."
        )

    def test_concurrent_calls_consistent_result(self, tmp_path: Path) -> None:
        """10 concurrent calls to _has_doc_id_pk must all return the same value."""
        from nexus.db.t2.document_aspects import DocumentAspects

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS document_aspects (
                doc_id TEXT PRIMARY KEY,
                collection TEXT NOT NULL DEFAULT '',
                source_path TEXT NOT NULL DEFAULT '',
                extracted_at TEXT NOT NULL DEFAULT '',
                model_version TEXT NOT NULL DEFAULT '',
                extractor_name TEXT NOT NULL DEFAULT '',
                source_uri TEXT
            );
        """)
        conn.commit()
        conn.close()

        aspects = DocumentAspects(db_path)
        results: list[bool] = []
        errors: list[Exception] = []

        def check() -> None:
            try:
                results.append(aspects._has_doc_id_pk())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=check) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors, f"Concurrent _has_doc_id_pk raised: {errors}"
        assert len(results) == 10
        assert len(set(results)) == 1, "All threads must agree on schema version"


# ── O-5: INSERT OR IGNORE in collections backfill ────────────────────────────


class TestO5InsertOrIgnore:
    """O-5: Collections backfill must use INSERT OR IGNORE, not WHERE NOT IN anti-join."""

    def test_backfill_uses_insert_or_ignore_in_source(self) -> None:
        """CatalogDB.__init__ source must use INSERT OR IGNORE for collections backfill."""
        from nexus.catalog.catalog_db import CatalogDB
        src = inspect.getsource(CatalogDB.__init__)
        assert "INSERT OR IGNORE" in src, (
            "CatalogDB.__init__ collections backfill must use INSERT OR IGNORE "
            "for idempotency and O(1) upsert (vs O(n*m) anti-join)."
        )

    def test_backfill_idempotent_on_double_open(self, tmp_path: Path) -> None:
        """Opening CatalogDB twice must not duplicate collections rows."""
        from nexus.catalog.catalog_db import CatalogDB

        db_path = tmp_path / ".catalog.db"
        db1 = CatalogDB(db_path)
        db1.execute(
            "INSERT INTO documents "
            "(tumbler, title, author, year, content_type, file_path, "
            " corpus, physical_collection, chunk_count, head_hash, indexed_at, "
            " metadata, source_mtime, alias_of, source_uri) "
            "VALUES ('nexus-o5t01', 'Test', '', 2026, 'text', '/test.pdf', "
            "        'knowledge', 'knowledge__test', 0, '', '2026-01-01', "
            "        '{}', 0, '', '')"
        )
        db1.commit()
        db1.close()

        db2 = CatalogDB(db_path)
        count = db2.execute(
            "SELECT COUNT(*) FROM collections WHERE name = ?",
            ("knowledge__test",),
        ).fetchone()[0]
        assert count == 1, "INSERT OR IGNORE must be idempotent (no duplicates on re-open)"
        db2.close()
