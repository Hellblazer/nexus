# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-096 P2.1 schema migration tests.

The 4.16.0 ``migrate_document_aspects_source_uri`` function:

* Adds a nullable ``source_uri TEXT`` column to ``document_aspects``.
* Backfills filesystem-backed collections (``rdr__/docs__/code__``)
  with ``'file://' || abspath(source_path)``.
* Backfills chroma-backed collections (``knowledge__*`` and other
  prefixes) with ``'chroma://' || collection || '/' || source_path``.
* Skips empty ``source_path`` rows (research-2 mitigation, id 1009).
* Idempotent on re-application.

The catalog ``documents`` table inline migration (in
``CatalogDB.__init__``):

* Adds ``source_uri TEXT NOT NULL DEFAULT ''`` to existing databases.
* Idempotent — re-opening a migrated DB is a no-op.

AspectRecord + DocumentAspects also widen to round-trip ``source_uri``
through the store.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


# ── document_aspects migration ───────────────────────────────────────────────


class TestDocumentAspectsSourceUriMigration:
    def _seed_legacy_table(self, conn: sqlite3.Connection) -> None:
        """Create a ``document_aspects`` table at the pre-4.16.0
        schema (no source_uri column) so the migration has something
        to widen.
        """
        from nexus.db.migrations import migrate_document_aspects_table
        migrate_document_aspects_table(conn)

    def _insert_row(
        self,
        conn: sqlite3.Connection,
        *,
        collection: str,
        source_path: str,
        extractor: str = "scholarly-paper-v1",
        model_version: str = "claude-haiku-4-5-20251001",
    ) -> None:
        conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, extracted_at, model_version, extractor_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (collection, source_path, "2026-04-27T00:00:00+00:00", model_version, extractor),
        )
        conn.commit()

    def test_migration_adds_source_uri_column(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_document_aspects_source_uri

        db = tmp_path / "p21.db"
        conn = sqlite3.connect(str(db))
        self._seed_legacy_table(conn)
        # Column must not exist yet.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(document_aspects)").fetchall()}
        assert "source_uri" not in cols

        migrate_document_aspects_source_uri(conn)

        cols = {
            r[1]: (r[2], r[3])  # name → (type, notnull)
            for r in conn.execute("PRAGMA table_info(document_aspects)").fetchall()
        }
        conn.close()
        assert "source_uri" in cols
        assert cols["source_uri"] == ("TEXT", 0)  # nullable

    def test_backfill_filesystem_collections_use_file_scheme(
        self, tmp_path: Path,
    ) -> None:
        from nexus.db.migrations import migrate_document_aspects_source_uri

        db = tmp_path / "fs.db"
        conn = sqlite3.connect(str(db))
        self._seed_legacy_table(conn)
        # rdr__ + docs__ + code__: all use file:// with abspath.
        self._insert_row(conn, collection="rdr__nexus", source_path="docs/rdr/rdr-090.md")
        self._insert_row(conn, collection="docs__corpus", source_path="docs/architecture.md")
        self._insert_row(conn, collection="code__nexus", source_path="src/nexus/cli.py")

        migrate_document_aspects_source_uri(conn)

        rows = dict(conn.execute(
            "SELECT collection, source_uri FROM document_aspects",
        ).fetchall())
        conn.close()
        # Each URI is file:// + abspath of the registered path.
        for coll, sp in [
            ("rdr__nexus", "docs/rdr/rdr-090.md"),
            ("docs__corpus", "docs/architecture.md"),
            ("code__nexus", "src/nexus/cli.py"),
        ]:
            assert rows[coll] == "file://" + os.path.abspath(sp)

    def test_backfill_knowledge_collections_use_chroma_scheme(
        self, tmp_path: Path,
    ) -> None:
        from nexus.db.migrations import migrate_document_aspects_source_uri

        db = tmp_path / "kn.db"
        conn = sqlite3.connect(str(db))
        self._seed_legacy_table(conn)
        # knowledge__*: chroma:// with literal source_path (slug-shaped
        # AND filesystem-shaped both work; the reader's identity-field
        # fallback handles it).
        self._insert_row(
            conn, collection="knowledge__knowledge",
            source_path="decision-bfdb-update-capture-rdr005",
        )
        self._insert_row(
            conn, collection="knowledge__delos",
            source_path="/Users/me/papers/aleph-bft.pdf",
        )

        migrate_document_aspects_source_uri(conn)

        rows = dict(conn.execute(
            "SELECT collection, source_uri FROM document_aspects",
        ).fetchall())
        conn.close()
        assert rows["knowledge__knowledge"] == (
            "chroma://knowledge__knowledge/decision-bfdb-update-capture-rdr005"
        )
        assert rows["knowledge__delos"] == (
            "chroma://knowledge__delos//Users/me/papers/aleph-bft.pdf"
        )

    def test_backfill_skips_empty_source_path(self, tmp_path: Path) -> None:
        """Research-2 mitigation (id 1009): one row in
        ``code__int-crossmodel-ba3a85dc`` has empty source_path.
        Cannot be backfilled — left at NULL with a logged warning.
        """
        from nexus.db.migrations import migrate_document_aspects_source_uri

        db = tmp_path / "skip.db"
        conn = sqlite3.connect(str(db))
        self._seed_legacy_table(conn)
        # Empty source_path violates NOT NULL on source_path? Let me
        # use a single space — the migration's "if not source_path"
        # guard treats empty string as the trigger; tests here use ""
        # to simulate the canonical mitigation case.
        # The schema requires non-empty source_path, so insert with a
        # non-empty value but flip the column to "" via UPDATE post-
        # insert (mimicking the corrupt-data shape research-2 found).
        self._insert_row(conn, collection="code__shadow", source_path="placeholder")
        conn.execute(
            "UPDATE document_aspects SET source_path = '' WHERE collection = 'code__shadow'",
        )
        conn.commit()

        migrate_document_aspects_source_uri(conn)

        row = conn.execute(
            "SELECT source_uri FROM document_aspects WHERE collection = 'code__shadow'",
        ).fetchone()
        conn.close()
        # NULL — the migration declined to invent a URI for an empty path.
        assert row[0] is None

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_document_aspects_source_uri

        db = tmp_path / "idem.db"
        conn = sqlite3.connect(str(db))
        self._seed_legacy_table(conn)
        self._insert_row(conn, collection="rdr__nexus", source_path="docs/rdr/rdr-090.md")

        migrate_document_aspects_source_uri(conn)
        migrate_document_aspects_source_uri(conn)
        # Third invocation also no-op.
        migrate_document_aspects_source_uri(conn)

        # Schema still has exactly one source_uri column.
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(document_aspects)",
        ).fetchall()]
        conn.close()
        assert cols.count("source_uri") == 1

    def test_migration_does_not_overwrite_pre_populated_source_uri(
        self, tmp_path: Path,
    ) -> None:
        """``WHERE source_uri IS NULL`` guard: rows that already have
        a source_uri (written by post-P2.1 callers between migration
        passes) are not stomped.
        """
        from nexus.db.migrations import migrate_document_aspects_source_uri

        db = tmp_path / "preserve.db"
        conn = sqlite3.connect(str(db))
        self._seed_legacy_table(conn)
        migrate_document_aspects_source_uri(conn)  # add column first
        # Insert a row with an explicit source_uri (mimicking a
        # post-migration writer).
        explicit_uri = "chroma://knowledge__custom/some-source"
        conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, extracted_at, model_version, "
            " extractor_name, source_uri) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("knowledge__custom", "different-path-on-disk",
             "2026-04-27T00:00:00+00:00", "claude-haiku-4-5-20251001",
             "scholarly-paper-v1", explicit_uri),
        )
        conn.commit()

        # Re-run the migration; the explicit URI must not be replaced
        # with the derived ``chroma://knowledge__custom/different-path-on-disk``.
        migrate_document_aspects_source_uri(conn)

        row = conn.execute(
            "SELECT source_uri FROM document_aspects "
            "WHERE collection = 'knowledge__custom'",
        ).fetchone()
        conn.close()
        assert row[0] == explicit_uri

    def test_migration_no_op_when_table_missing(self, tmp_path: Path) -> None:
        """Defensive: if document_aspects has not been created yet
        (fresh install, migration order swapped), the migration must
        not raise.
        """
        from nexus.db.migrations import migrate_document_aspects_source_uri

        db = tmp_path / "no_table.db"
        conn = sqlite3.connect(str(db))
        # Don't create document_aspects.
        migrate_document_aspects_source_uri(conn)  # must not raise
        conn.close()


# ── catalog documents inline migration ──────────────────────────────────────


class TestCatalogDocumentsSourceUriMigration:
    def test_inline_migration_adds_source_uri_to_existing_db(
        self, tmp_path: Path,
    ) -> None:
        """Open a catalog DB at the pre-4.16.0 schema (no source_uri
        column on documents), then re-open via CatalogDB; the inline
        migration must add the column without losing data.
        """
        from nexus.catalog.catalog_db import CatalogDB

        db = tmp_path / "cat.db"

        # Hand-craft a minimal pre-4.16.0 documents table missing
        # the source_uri column. Mirrors the columns CatalogDB
        # creates EXCEPT for source_uri.
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE documents (
                tumbler TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                author TEXT,
                year INTEGER,
                content_type TEXT,
                file_path TEXT,
                corpus TEXT,
                physical_collection TEXT,
                chunk_count INTEGER,
                head_hash TEXT,
                indexed_at TEXT,
                metadata JSON,
                source_mtime REAL NOT NULL DEFAULT 0,
                alias_of TEXT NOT NULL DEFAULT ''
            );
        """)
        conn.execute(
            "INSERT INTO documents "
            "(tumbler, title, content_type, file_path) "
            "VALUES ('1.1.1', 't', 'rdr', 'docs/rdr/rdr-090.md')",
        )
        conn.commit()
        conn.close()

        # Re-open via CatalogDB — inline migration runs.
        cat = CatalogDB(db)
        cols = {
            r[1] for r in cat._conn.execute(
                "PRAGMA table_info(documents)",
            ).fetchall()
        }
        # Pre-existing row must still be there.
        rows = cat._conn.execute(
            "SELECT tumbler, source_uri FROM documents",
        ).fetchall()
        cat.close()

        assert "source_uri" in cols
        # Pre-migration row gets the default '' for source_uri.
        assert rows == [("1.1.1", "")]

    def test_inline_migration_idempotent_on_reopen(self, tmp_path: Path) -> None:
        from nexus.catalog.catalog_db import CatalogDB

        db = tmp_path / "cat_idem.db"
        # Fresh open creates the table with source_uri.
        c1 = CatalogDB(db)
        c1.close()
        # Re-open must not raise (the SELECT-then-ALTER inline guard
        # short-circuits cleanly).
        c2 = CatalogDB(db)
        cols = {
            r[1] for r in c2._conn.execute(
                "PRAGMA table_info(documents)",
            ).fetchall()
        }
        c2.close()
        assert "source_uri" in cols


# ── P2.2 null-row DELETE migration ──────────────────────────────────────────


class TestNullRowDeleteMigration:
    """RDR-096 P2.2: ``migrate_drop_null_aspect_rows`` removes
    pre-RDR-096 read-failure rows from ``document_aspects`` using the
    JSON-aware seven-clause discriminator from research-3 (id 1010).

    The ``confidence IS NULL`` clause is load-bearing: without it the
    migration silently drops ``rdr-frontmatter-v1`` "structured-zero"
    successes (parser ran, document has no scholarly structure,
    extractor wrote ``confidence=1.0`` with all fields empty).

    The JSON-aware ``(IS NULL OR = '[]')`` clauses on
    ``experimental_datasets`` / ``experimental_baselines`` are also
    load-bearing: the writer stores ``json.dumps([]) == '[]'`` for
    empty lists, NOT NULL. A bare ``IS NULL`` clause would match zero
    rows in production despite the spike's empirical 15-row finding.
    """

    def _seed_legacy_table(self, conn: sqlite3.Connection) -> None:
        from nexus.db.migrations import (
            migrate_document_aspects_source_uri,
            migrate_document_aspects_table,
        )
        migrate_document_aspects_table(conn)
        migrate_document_aspects_source_uri(conn)

    def _insert(
        self,
        conn: sqlite3.Connection,
        *,
        collection: str,
        source_path: str,
        problem_formulation: str | None = None,
        proposed_method: str | None = None,
        experimental_datasets: str = "[]",
        experimental_baselines: str = "[]",
        experimental_results: str | None = None,
        extras: str | None = "{}",
        confidence: float | None = None,
        extractor_name: str = "scholarly-paper-v1",
    ) -> None:
        conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, problem_formulation, proposed_method, "
            " experimental_datasets, experimental_baselines, experimental_results, "
            " extras, confidence, extracted_at, model_version, extractor_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                collection, source_path, problem_formulation, proposed_method,
                experimental_datasets, experimental_baselines, experimental_results,
                extras, confidence,
                "2026-04-26T00:00:00+00:00", "claude-haiku-4-5-20251001",
                extractor_name,
            ),
        )
        conn.commit()

    def _seed_three_categories(self, conn: sqlite3.Connection) -> None:
        """Plant a 4-row fixture mirroring the four production
        categories surfaced by research-3 (compressed for test speed):

        - read-failure null (scholarly-paper-v1 + rdr-frontmatter-v1
          shapes; confidence IS NULL, all aspect fields empty,
          extras='{}'): SHOULD be dropped.
        - structured-zero success (rdr-frontmatter-v1 with
          confidence=1.0, all aspect fields empty): SHOULD be retained.
        - partial (problem_formulation populated, rest empty):
          SHOULD be retained.
        - full (all fields populated): SHOULD be retained.
        """
        # Category 1: read-failure null (target of the migration).
        # Three sub-shapes covering the writer-normalised form
        # (extras='{}'), the legacy/hand-crafted form (extras IS NULL),
        # and the JSON-array writer form (datasets='[]').
        self._insert(
            conn, collection="rdr__nexus", source_path="docs/rdr/missing-1.md",
            extractor_name="rdr-frontmatter-v1",
            problem_formulation=None, proposed_method=None,
            experimental_datasets="[]", experimental_baselines="[]",
            experimental_results=None, extras="{}", confidence=None,
        )
        self._insert(
            conn, collection="knowledge__hybridrag", source_path="ghost-paper",
            extractor_name="scholarly-paper-v1",
            problem_formulation=None, proposed_method=None,
            experimental_datasets="[]", experimental_baselines="[]",
            experimental_results=None, extras="{}", confidence=None,
        )
        # Legacy / hand-crafted ghost: extras stored as SQL NULL,
        # not '{}'. The OR-clause widening must catch this; without
        # it the row would silently survive the migration.
        self._insert(
            conn, collection="rdr__nexus", source_path="docs/rdr/legacy-ghost.md",
            extractor_name="rdr-frontmatter-v1",
            problem_formulation=None, proposed_method=None,
            experimental_datasets="[]", experimental_baselines="[]",
            experimental_results=None, extras=None, confidence=None,
        )

        # Category 2: structured-zero success — confidence=1.0 retains.
        self._insert(
            conn, collection="rdr__nexus", source_path="docs/rdr/readme.md",
            extractor_name="rdr-frontmatter-v1",
            problem_formulation=None, proposed_method=None,
            experimental_datasets="[]", experimental_baselines="[]",
            experimental_results=None, extras="{}", confidence=1.0,
        )

        # Category 3: partial — at least one field populated.
        self._insert(
            conn, collection="knowledge__delos", source_path="aleph-bft.pdf",
            problem_formulation="atomic broadcast",
            proposed_method=None,
            experimental_datasets="[]", experimental_baselines="[]",
            experimental_results=None, extras="{}", confidence=None,
        )

        # Category 4: full success.
        self._insert(
            conn, collection="knowledge__delos", source_path="lightweight-smr.pdf",
            problem_formulation="state machine replication",
            proposed_method="median rule",
            experimental_datasets='["TPC-C"]', experimental_baselines='["raft"]',
            experimental_results="30% improvement",
            extras='{"venue":"OSDI"}', confidence=0.9,
        )

    def test_drops_only_read_failure_nulls(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_drop_null_aspect_rows

        db = tmp_path / "drop.db"
        conn = sqlite3.connect(str(db))
        self._seed_legacy_table(conn)
        self._seed_three_categories(conn)
        # Pre: 6 rows (3 read-failure variants + 1 structured-zero +
        # 1 partial + 1 full).
        assert conn.execute(
            "SELECT COUNT(*) FROM document_aspects",
        ).fetchone()[0] == 6

        migrate_drop_null_aspect_rows(conn)

        # Post: 3 rows (all 3 read-failure variants dropped — including
        # the legacy-ghost with extras IS NULL; structured-zero,
        # partial, and full are retained).
        rows = conn.execute(
            "SELECT collection, source_path FROM document_aspects "
            "ORDER BY collection, source_path",
        ).fetchall()
        conn.close()
        assert rows == [
            ("knowledge__delos", "aleph-bft.pdf"),         # partial
            ("knowledge__delos", "lightweight-smr.pdf"),    # full
            ("rdr__nexus", "docs/rdr/readme.md"),           # structured-zero
        ]

    def test_extras_null_clause_is_load_bearing(self, tmp_path: Path) -> None:
        """``extras IS NULL`` is the third load-bearing widening
        (alongside JSON-array and confidence). The writer always
        emits ``json.dumps({}) == '{}'`` so the OR half is defensive
        against legacy / hand-crafted rows. Without it, a ghost row
        with ``extras=NULL`` silently survives the migration.
        """
        db = tmp_path / "no_extras_null.db"
        conn = sqlite3.connect(str(db))
        self._seed_legacy_table(conn)
        self._seed_three_categories(conn)

        # Run the bare ``extras = '{}'`` form (no ``OR IS NULL``).
        # The legacy-ghost row with extras=NULL should escape.
        conn.execute(
            "DELETE FROM document_aspects "
            "WHERE problem_formulation IS NULL "
            "  AND proposed_method IS NULL "
            "  AND (experimental_datasets IS NULL OR experimental_datasets = '[]') "
            "  AND (experimental_baselines IS NULL OR experimental_baselines = '[]') "
            "  AND experimental_results IS NULL "
            "  AND extras = '{}' "  # bare; missing the IS NULL OR
            "  AND confidence IS NULL"
        )
        conn.commit()
        kept = {
            r[1] for r in conn.execute(
                "SELECT collection, source_path FROM document_aspects",
            ).fetchall()
        }
        conn.close()
        # Without the OR-clause: legacy-ghost survives.
        assert "docs/rdr/legacy-ghost.md" in kept

    def test_confidence_clause_is_load_bearing(self, tmp_path: Path) -> None:
        """Without ``AND confidence IS NULL``, the migration would
        also drop the structured-zero success (51 rows in production
        per research-3). Manually run the SQL with the clause omitted
        against the same fixture and verify the structured-zero
        gets dropped — confirms the clause's load-bearing role.
        """
        db = tmp_path / "no_conf.db"
        conn = sqlite3.connect(str(db))
        self._seed_legacy_table(conn)
        self._seed_three_categories(conn)

        # Run a deliberately-broken SQL omitting confidence IS NULL.
        conn.execute(
            "DELETE FROM document_aspects "
            "WHERE problem_formulation IS NULL "
            "  AND proposed_method IS NULL "
            "  AND (experimental_datasets IS NULL OR experimental_datasets = '[]') "
            "  AND (experimental_baselines IS NULL OR experimental_baselines = '[]') "
            "  AND experimental_results IS NULL "
            "  AND extras = '{}' "
            # Intentionally NO confidence clause.
        )
        conn.commit()
        kept = {
            r[1] for r in conn.execute(
                "SELECT collection, source_path FROM document_aspects",
            ).fetchall()
        }
        conn.close()
        # Without confidence IS NULL: structured-zero readme is also gone.
        assert "docs/rdr/readme.md" not in kept

    def test_json_array_clause_is_load_bearing(self, tmp_path: Path) -> None:
        """The writer stores ``json.dumps([]) == '[]'`` for empty
        list fields, NOT NULL. Without the ``OR = '[]'`` part of the
        clause, the bare ``IS NULL`` predicate matches zero rows
        despite the spike's 15-row empirical finding.
        """
        db = tmp_path / "no_json.db"
        conn = sqlite3.connect(str(db))
        self._seed_legacy_table(conn)
        self._seed_three_categories(conn)

        # Run the bare IS NULL form — would match nothing.
        conn.execute(
            "DELETE FROM document_aspects "
            "WHERE problem_formulation IS NULL "
            "  AND proposed_method IS NULL "
            "  AND experimental_datasets IS NULL "  # bare; no '[]' OR
            "  AND experimental_baselines IS NULL "
            "  AND experimental_results IS NULL "
            "  AND extras = '{}' "
            "  AND confidence IS NULL"
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM document_aspects").fetchone()[0]
        conn.close()
        # Bare IS NULL matched ZERO rows because writer stored '[]' not NULL.
        # Production-shaped data would also see zero matches under this SQL.
        assert count == 6  # original count unchanged

    def test_idempotent_on_re_run(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_drop_null_aspect_rows

        db = tmp_path / "idem.db"
        conn = sqlite3.connect(str(db))
        self._seed_legacy_table(conn)
        self._seed_three_categories(conn)

        migrate_drop_null_aspect_rows(conn)
        first_count = conn.execute(
            "SELECT COUNT(*) FROM document_aspects",
        ).fetchone()[0]
        # Second invocation: 0 read-failure rows remain → no-op.
        migrate_drop_null_aspect_rows(conn)
        second_count = conn.execute(
            "SELECT COUNT(*) FROM document_aspects",
        ).fetchone()[0]
        conn.close()
        assert first_count == second_count == 3

    def test_no_op_when_table_missing(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_drop_null_aspect_rows

        db = tmp_path / "no_table.db"
        conn = sqlite3.connect(str(db))
        # Don't create document_aspects.
        migrate_drop_null_aspect_rows(conn)  # must not raise
        conn.close()


# ── AspectRecord round-trip through the store ───────────────────────────────


class TestAspectRecordSourceUriRoundTrip:
    def test_upsert_and_get_round_trip_source_uri(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from nexus.db.t2.document_aspects import AspectRecord, DocumentAspects

        store = DocumentAspects(tmp_path / "rt.db")
        try:
            store.upsert(AspectRecord(
                collection="knowledge__delos",
                source_path="/Users/me/aleph-bft.pdf",
                problem_formulation="problem",
                proposed_method="method",
                extracted_at=datetime.now(UTC).isoformat(),
                model_version="claude-haiku-4-5-20251001",
                extractor_name="scholarly-paper-v1",
                source_uri="chroma://knowledge__delos//Users/me/aleph-bft.pdf",
            ))
            got = store.get("knowledge__delos", "/Users/me/aleph-bft.pdf")
        finally:
            store.close()

        assert got is not None
        assert got.source_uri == "chroma://knowledge__delos//Users/me/aleph-bft.pdf"

    def test_legacy_row_with_null_source_uri_reads_back_as_none(
        self, tmp_path: Path,
    ) -> None:
        """Backward compat: rows that existed before P2.1 and were
        backfilled to NULL (or never backfilled) read back as
        ``source_uri=None`` on the AspectRecord.
        """
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "legacy.db")
        try:
            # Bypass upsert to simulate a pre-P2.1 row (source_uri NULL).
            with store._lock:
                store.conn.execute(
                    "INSERT INTO document_aspects "
                    "(collection, source_path, extracted_at, "
                    " model_version, extractor_name) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("knowledge__legacy", "old-source",
                     "2026-04-26T00:00:00+00:00",
                     "claude-haiku-4-5-20251001", "scholarly-paper-v1"),
                )
                store.conn.commit()
            got = store.get("knowledge__legacy", "old-source")
        finally:
            store.close()

        assert got is not None
        assert got.source_uri is None
