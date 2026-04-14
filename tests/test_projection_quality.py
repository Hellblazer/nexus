# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-077 projection-quality columns + ICF hub detection.

Phase 1 (nexus-nsh): migration adds ``similarity``, ``assigned_at``,
``source_collection`` columns and ``idx_topic_assignments_source`` index to
``topic_assignments``.
Phase 2 (nexus-uti): write-path atomic commit — AssignResult, prefer-higher
UPSERT, 3-tuple tuple shape across all five call sites.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import chromadb
import numpy as np
import pytest

from nexus.db.t2 import T2Database


def _make_taxonomy_db() -> sqlite3.Connection:
    """Return an in-memory DB with the pre-4.3.0 taxonomy schema.

    Matches the schema that existed before the RDR-077 migration:
    legacy ``topic_assignments`` with only ``doc_id``, ``topic_id``,
    ``assigned_by``.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE topics (
            id            INTEGER PRIMARY KEY,
            label         TEXT NOT NULL,
            parent_id     INTEGER REFERENCES topics(id),
            collection    TEXT NOT NULL,
            centroid_hash TEXT,
            doc_count     INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'pending',
            terms         TEXT
        );
        CREATE TABLE topic_assignments (
            doc_id      TEXT NOT NULL,
            topic_id    INTEGER NOT NULL REFERENCES topics(id),
            assigned_by TEXT NOT NULL DEFAULT 'hdbscan',
            PRIMARY KEY (doc_id, topic_id)
        );
        """
    )
    return conn


class TestAddProjectionQualityColumns:
    """RDR-077 Phase 1 migration: three new columns + one new index."""

    def test_migration_adds_columns(self) -> None:
        from nexus.db.migrations import _add_projection_quality_columns

        conn = _make_taxonomy_db()
        _add_projection_quality_columns(conn)

        cols = {
            r[1]: r[2]
            for r in conn.execute("PRAGMA table_info(topic_assignments)").fetchall()
        }
        assert "similarity" in cols
        assert cols["similarity"] == "REAL"
        assert "assigned_at" in cols
        assert cols["assigned_at"] == "TEXT"
        assert "source_collection" in cols
        assert cols["source_collection"] == "TEXT"

    def test_migration_adds_index(self) -> None:
        from nexus.db.migrations import _add_projection_quality_columns

        conn = _make_taxonomy_db()
        _add_projection_quality_columns(conn)

        indexes = {
            r[1] for r in conn.execute(
                "PRAGMA index_list(topic_assignments)"
            ).fetchall()
        }
        assert "idx_topic_assignments_source" in indexes

        # Verify the index covers (source_collection, assigned_by)
        index_cols = [
            r[2] for r in conn.execute(
                "PRAGMA index_info(idx_topic_assignments_source)"
            ).fetchall()
        ]
        assert index_cols == ["source_collection", "assigned_by"]

    def test_migration_idempotent(self) -> None:
        from nexus.db.migrations import _add_projection_quality_columns

        conn = _make_taxonomy_db()
        _add_projection_quality_columns(conn)
        # Second call must be a no-op, not raise.
        _add_projection_quality_columns(conn)

        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(topic_assignments)").fetchall()
        }
        assert {"similarity", "assigned_at", "source_collection"}.issubset(cols)

    def test_migration_noop_when_columns_present(self) -> None:
        """If columns already exist (fresh install), migration is no-op."""
        from nexus.db.migrations import _add_projection_quality_columns

        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE topics (id INTEGER PRIMARY KEY, label TEXT NOT NULL,
                collection TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE topic_assignments (
                doc_id            TEXT NOT NULL,
                topic_id          INTEGER NOT NULL REFERENCES topics(id),
                assigned_by       TEXT NOT NULL DEFAULT 'hdbscan',
                similarity        REAL,
                assigned_at       TEXT,
                source_collection TEXT,
                PRIMARY KEY (doc_id, topic_id)
            );
            CREATE INDEX idx_topic_assignments_source
                ON topic_assignments(source_collection, assigned_by);
            """
        )
        _add_projection_quality_columns(conn)  # must not raise

    def test_migration_noop_when_table_missing(self) -> None:
        """If ``topic_assignments`` doesn't exist yet, migration is a no-op."""
        from nexus.db.migrations import _add_projection_quality_columns

        conn = sqlite3.connect(":memory:")
        _add_projection_quality_columns(conn)  # must not raise

    def test_registered_in_migrations_list(self) -> None:
        """The new migration must be in MIGRATIONS at version 4.3.0."""
        from nexus.db.migrations import MIGRATIONS

        hits = [
            m for m in MIGRATIONS
            if m.fn.__name__ == "_add_projection_quality_columns"
        ]
        assert len(hits) == 1
        assert hits[0].introduced == "4.3.0"

    def test_preserves_existing_rows(self) -> None:
        """Legacy rows keep NULLs for new columns (no backfill)."""
        from nexus.db.migrations import _add_projection_quality_columns

        conn = _make_taxonomy_db()
        conn.execute(
            "INSERT INTO topics (id, label, collection, created_at) "
            "VALUES (1, 'foo', 'code__repo', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by) "
            "VALUES ('docA', 1, 'hdbscan')"
        )
        conn.commit()

        _add_projection_quality_columns(conn)

        row = conn.execute(
            "SELECT doc_id, topic_id, assigned_by, "
            "similarity, assigned_at, source_collection "
            "FROM topic_assignments"
        ).fetchone()
        assert row == ("docA", 1, "hdbscan", None, None, None)


# ── Phase 2 (nexus-uti) — write-path atomic commit ──────────────────────────


@pytest.fixture()
def chroma_client() -> chromadb.ClientAPI:
    """Ephemeral ChromaDB client per test."""
    return chromadb.EphemeralClient()


@pytest.fixture()
def db(tmp_path: Path) -> T2Database:
    database = T2Database(tmp_path / "memory.db")
    yield database
    database.close()


def _seed_topic(db: T2Database, *, topic_id: int = 1, collection: str = "code__src") -> None:
    """Create a single topic row for assignment tests."""
    db.taxonomy.conn.execute(
        "INSERT INTO topics (id, label, collection, created_at) "
        "VALUES (?, 'seed', ?, '2026-04-14')",
        (topic_id, collection),
    )
    db.taxonomy.conn.commit()


def _read_assignment(db: T2Database, doc_id: str, topic_id: int) -> dict | None:
    row = db.taxonomy.conn.execute(
        "SELECT doc_id, topic_id, assigned_by, similarity, "
        "assigned_at, source_collection "
        "FROM topic_assignments WHERE doc_id = ? AND topic_id = ?",
        (doc_id, topic_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "doc_id": row[0],
        "topic_id": row[1],
        "assigned_by": row[2],
        "similarity": row[3],
        "assigned_at": row[4],
        "source_collection": row[5],
    }


class TestUpsertPreferHigher:
    """SC-2 prefer-higher UPSERT for projection rows."""

    def test_upsert_prefer_higher_descending(self, db: T2Database) -> None:
        """Insert 0.9 then 0.7 — stored remains 0.9, source/at NOT refreshed."""
        _seed_topic(db)
        db.taxonomy.assign_topic(
            "docA", 1, assigned_by="projection",
            similarity=0.9, source_collection="code__src_a",
            assigned_at="2026-04-14T10:00:00",
        )
        db.taxonomy.assign_topic(
            "docA", 1, assigned_by="projection",
            similarity=0.7, source_collection="code__src_b",
            assigned_at="2026-04-14T11:00:00",
        )
        row = _read_assignment(db, "docA", 1)
        assert row["similarity"] == pytest.approx(0.9)
        assert row["source_collection"] == "code__src_a"
        assert row["assigned_at"] == "2026-04-14T10:00:00"

    def test_upsert_prefer_higher_ascending(self, db: T2Database) -> None:
        """Insert 0.7 then 0.9 — stored becomes 0.9, source/at refreshed."""
        _seed_topic(db)
        db.taxonomy.assign_topic(
            "docA", 1, assigned_by="projection",
            similarity=0.7, source_collection="code__src_b",
            assigned_at="2026-04-14T11:00:00",
        )
        db.taxonomy.assign_topic(
            "docA", 1, assigned_by="projection",
            similarity=0.9, source_collection="code__src_a",
            assigned_at="2026-04-14T10:00:00",
        )
        row = _read_assignment(db, "docA", 1)
        assert row["similarity"] == pytest.approx(0.9)
        assert row["source_collection"] == "code__src_a"
        assert row["assigned_at"] == "2026-04-14T10:00:00"

    def test_upsert_promotes_null_legacy_row(self, db: T2Database) -> None:
        """Pre-migration NULL row is promoted on re-projection (COALESCE(-1.0))."""
        _seed_topic(db)
        # Simulate a legacy row: assigned_by='projection' but NULL quality.
        db.taxonomy.conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by) "
            "VALUES ('docLegacy', 1, 'projection')"
        )
        db.taxonomy.conn.commit()

        db.taxonomy.assign_topic(
            "docLegacy", 1, assigned_by="projection",
            similarity=0.6, source_collection="code__promote",
            assigned_at="2026-04-14T12:00:00",
        )
        row = _read_assignment(db, "docLegacy", 1)
        assert row["similarity"] == pytest.approx(0.6)
        assert row["source_collection"] == "code__promote"
        assert row["assigned_at"] == "2026-04-14T12:00:00"


class TestHdbscanPathPreserved:
    def test_hdbscan_keeps_insert_or_ignore(self, db: T2Database) -> None:
        """HDBSCAN assignments stay idempotent, NULL similarity/source."""
        _seed_topic(db)
        db.taxonomy.assign_topic("docH", 1)  # default assigned_by='hdbscan'
        db.taxonomy.assign_topic("docH", 1)  # second call is a no-op
        row = _read_assignment(db, "docH", 1)
        assert row["assigned_by"] == "hdbscan"
        assert row["similarity"] is None
        assert row["assigned_at"] is None
        assert row["source_collection"] is None

    def test_manual_assigned_by_also_ignores(self, db: T2Database) -> None:
        _seed_topic(db)
        db.taxonomy.assign_topic("docM", 1, assigned_by="manual")
        row = _read_assignment(db, "docM", 1)
        assert row["assigned_by"] == "manual"
        assert row["similarity"] is None


def _build_two_clusters_in_chroma(
    client: chromadb.ClientAPI, collection_name: str = "coll_A",
) -> list[dict]:
    """Seed ``collection_name`` centroids for two well-separated clusters."""
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    return embeddings


class TestAssignSingleReturnsNamedTuple:
    """SC-2 case 4: AssignResult shape + distance→similarity inversion."""

    def test_assign_single_returns_namedtuple(
        self, db: T2Database, chroma_client: chromadb.ClientAPI,
    ) -> None:
        from nexus.db.t2.catalog_taxonomy import AssignResult

        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0
        doc_ids = [f"d-{i}" for i in range(60)]
        texts = [f"text {i}" for i in range(60)]
        db.taxonomy.discover_topics(
            "nt_coll", doc_ids, embeddings, texts, chroma_client,
        )

        # Query with an embedding close to cluster A.
        new_emb = rng.standard_normal(384).astype(np.float32) * 0.1
        new_emb[0] += 3.0
        result = db.taxonomy.assign_single("nt_coll", new_emb, chroma_client)
        assert result is not None
        assert isinstance(result, AssignResult)
        assert isinstance(result.topic_id, int)
        assert isinstance(result.similarity, float)
        # Raw cosine ∈ [-1, 1]; near-cluster-A query should give a positive sim.
        assert -1.0 <= result.similarity <= 1.0


class TestAssignBatchCrossCollectionSimilarity:
    """C-1 (auditor): cross-collection batch must propagate per-row similarity."""

    def test_assign_batch_cross_collection_populates_similarity(
        self, db: T2Database, chroma_client: chromadb.ClientAPI,
    ) -> None:
        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0
        doc_ids = [f"d-{i}" for i in range(60)]
        texts = [f"text {i}" for i in range(60)]
        # Collection A owns the centroids.
        db.taxonomy.discover_topics(
            "coll_A_c1", doc_ids, embeddings, texts, chroma_client,
        )

        # Collection B sends a batch with cross_collection=True.
        b_ids = [f"b-{i}" for i in range(5)]
        b_embs = (rng.standard_normal((5, 384)).astype(np.float32) * 0.1)
        b_embs[:, 0] += 3.0
        assigned = db.taxonomy.assign_batch(
            "coll_B_c1", b_ids, b_embs.tolist(), chroma_client,
            cross_collection=True,
        )
        assert assigned > 0

        rows = db.taxonomy.conn.execute(
            "SELECT doc_id, assigned_by, similarity, source_collection "
            "FROM topic_assignments WHERE doc_id LIKE 'b-%'"
        ).fetchall()
        assert rows, "batch must write at least one assignment"
        for doc_id, by, sim, src in rows:
            assert by == "projection"
            assert sim is not None, f"similarity not populated for {doc_id}"
            assert -1.0 <= sim <= 1.0
            assert src == "coll_B_c1"


class TestBackfillProjectionRegression:
    """SC-8: ``backfill_projection`` consumes 3-tuples without crashing."""

    def test_backfill_projection_3tuple(
        self, db: T2Database, chroma_client: chromadb.ClientAPI,
    ) -> None:
        from nexus.db.migrations import backfill_projection

        class _StubT3:
            def __init__(self, client: chromadb.ClientAPI) -> None:
                self._client = client

        # Seed two collections with distinct clusters so projection has targets.
        # Upload source docs to the T3 collection too — project_against fetches
        # source embeddings from the source collection, not the centroid store.
        rng = np.random.default_rng(42)
        for name in ("code__cA", "code__cB"):
            embs = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
            embs[:30, 0] += 3.0
            embs[30:, 1] += 3.0
            doc_ids = [f"{name}-d{i}" for i in range(60)]
            texts = [f"text for {name} {i}" for i in range(60)]
            db.taxonomy.discover_topics(
                name, doc_ids, embs, texts, chroma_client,
            )
            src_coll = chroma_client.get_or_create_collection(
                name, embedding_function=None,
            )
            src_coll.add(
                ids=doc_ids,
                embeddings=embs.tolist(),
                documents=texts,
            )

        # Must not raise — previously would ValueError on tuple unpack.
        backfill_projection(_StubT3(chroma_client), db.taxonomy)

        rows = db.taxonomy.conn.execute(
            "SELECT similarity, source_collection FROM topic_assignments "
            "WHERE assigned_by = 'projection'"
        ).fetchall()
        assert rows, "backfill should persist projection rows"
        for sim, src in rows:
            assert sim is not None
            assert src in ("code__cA", "code__cB")


class TestProjectAgainst3Tuple:
    """``project_against`` emits 3-tuples with raw cosine similarity."""

    def test_chunk_assignments_carry_similarity(
        self, db: T2Database, chroma_client: chromadb.ClientAPI,
    ) -> None:
        rng = np.random.default_rng(42)
        for name in ("code__pA", "code__pB"):
            embs = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
            embs[:30, 0] += 3.0
            embs[30:, 1] += 3.0
            doc_ids = [f"{name}-d{i}" for i in range(60)]
            texts = [f"text {name} {i}" for i in range(60)]
            db.taxonomy.discover_topics(
                name, doc_ids, embs, texts, chroma_client,
            )
            src_coll = chroma_client.get_or_create_collection(
                name, embedding_function=None,
            )
            src_coll.add(
                ids=doc_ids,
                embeddings=embs.tolist(),
                documents=texts,
            )

        result = db.taxonomy.project_against(
            "code__pA", ["code__pB"], chroma_client, threshold=-1.0,
        )
        chunk_assignments = result["chunk_assignments"]
        assert chunk_assignments, "expected at least one projection match"
        for item in chunk_assignments:
            assert len(item) == 3, "each assignment must be a 3-tuple"
            doc_id, topic_id, similarity = item
            assert isinstance(doc_id, str)
            assert isinstance(topic_id, int)
            assert isinstance(similarity, float)
