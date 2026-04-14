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


# ── Phase 3 (nexus-qab) — ICF computation ───────────────────────────────────


def _seed_projection_rows(
    db: T2Database, rows: list[tuple[str, int, str]],
) -> None:
    """Seed projection topic_assignments with ``(doc_id, topic_id, source_collection)``.

    Creates referenced ``topics`` rows on demand so FKs resolve.
    """
    topic_ids = {tid for _, tid, _ in rows}
    for tid in topic_ids:
        db.taxonomy.conn.execute(
            "INSERT OR IGNORE INTO topics (id, label, collection, created_at) "
            "VALUES (?, 'seed', 'code__any', '2026-04-14')",
            (tid,),
        )
    for doc_id, tid, src in rows:
        db.taxonomy.assign_topic(
            doc_id, tid, assigned_by="projection",
            similarity=0.9, source_collection=src,
            assigned_at="2026-04-14T00:00:00",
        )
    db.taxonomy.clear_icf_cache()


class TestICF:
    """RDR-077 Phase 3 — ``compute_icf_map`` SC-3 + SC-8."""

    def test_icf_log2_base(self, db: T2Database) -> None:
        """N=4, DF=2 → ICF = log2(4/2) = 1.0 exactly."""
        import math

        # Topic 1 appears in 2 of 4 collections; topics 2-4 each in 1.
        _seed_projection_rows(db, [
            ("docA", 1, "code__c1"),
            ("docB", 1, "code__c2"),
            ("docC", 2, "code__c1"),
            ("docD", 3, "code__c3"),
            ("docE", 4, "code__c4"),
        ])
        icf = db.taxonomy.compute_icf_map()
        # N_effective = 4 distinct source_collections.
        assert icf[1] == pytest.approx(math.log2(4 / 2))
        assert icf[2] == pytest.approx(math.log2(4 / 1))
        assert icf[3] == pytest.approx(math.log2(4 / 1))
        assert icf[4] == pytest.approx(math.log2(4 / 1))

    def test_icf_df_equals_n_yields_zero(self, db: T2Database) -> None:
        """Ubiquitous topic (appears in every collection) → ICF = 0."""
        _seed_projection_rows(db, [
            ("docA", 1, "code__c1"),
            ("docB", 1, "code__c2"),
            ("docC", 1, "code__c3"),
        ])
        icf = db.taxonomy.compute_icf_map()
        assert icf[1] == pytest.approx(0.0)

    def test_icf_n_effective_excludes_null_source(self, db: T2Database) -> None:
        """Legacy NULL ``source_collection`` rows don't inflate N or DF."""
        _seed_projection_rows(db, [
            ("docA", 1, "code__c1"),
            ("docB", 1, "code__c2"),
            ("docC", 2, "code__c1"),
        ])
        # Insert a legacy NULL row directly (simulate pre-migration state).
        db.taxonomy.conn.execute(
            "INSERT OR IGNORE INTO topics (id, label, collection, created_at) "
            "VALUES (99, 'legacy', 'code__any', '2026-04-14')"
        )
        db.taxonomy.conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by) "
            "VALUES ('docLegacy', 99, 'projection')"
        )
        db.taxonomy.conn.commit()
        db.taxonomy.clear_icf_cache()

        icf = db.taxonomy.compute_icf_map()
        # Legacy NULL row excluded: N_effective stays 2 (c1, c2); topic 99 absent.
        assert 99 not in icf
        # Topic 1 in both collections → ICF = 0.
        assert icf[1] == pytest.approx(0.0)

    def test_icf_disabled_when_n_lt_2(self, db: T2Database) -> None:
        """Single-collection corpus → empty map (ICF undefined)."""
        _seed_projection_rows(db, [
            ("docA", 1, "code__only"),
            ("docB", 2, "code__only"),
        ])
        icf = db.taxonomy.compute_icf_map()
        assert icf == {}

    def test_icf_disabled_when_no_projection_rows(self, db: T2Database) -> None:
        """Empty taxonomy → empty map, no SQL error."""
        icf = db.taxonomy.compute_icf_map()
        assert icf == {}

    def test_icf_cache_lifecycle(self, db: T2Database) -> None:
        """Cache populated once, survives multiple calls, cleared on demand."""
        _seed_projection_rows(db, [
            ("docA", 1, "code__c1"),
            ("docB", 1, "code__c2"),
            ("docC", 2, "code__c1"),
        ])

        first = db.taxonomy.compute_icf_map(use_cache=True)
        assert first, "expected populated ICF map"
        second = db.taxonomy.compute_icf_map(use_cache=True)
        assert second is first, "cached object identity preserved"

        # Mutating the DB does not invalidate the cache until we ask it to.
        _seed_projection_rows(db, [("docX", 1, "code__c3")])
        # _seed_projection_rows already calls clear_icf_cache — verify that.
        third = db.taxonomy.compute_icf_map(use_cache=True)
        assert third is not first, "cache must refresh after clear_icf_cache"

    def test_icf_log2_scalar_registered(self, db: T2Database) -> None:
        """``log2`` is available to arbitrary SQL on CatalogTaxonomy.conn."""
        row = db.taxonomy.conn.execute("SELECT log2(8.0)").fetchone()
        assert row[0] == pytest.approx(3.0)
        # Null-safe: non-positive input → NULL (prevents ValueError).
        row_zero = db.taxonomy.conn.execute("SELECT log2(0)").fetchone()
        assert row_zero[0] is None
        row_neg = db.taxonomy.conn.execute("SELECT log2(-1.0)").fetchone()
        assert row_neg[0] is None


# ── Phase 4a (nexus-jt1) — ICF-weighted projection + CLI defaults ───────────


class TestDefaultProjectionThreshold:
    """RDR-077 Phase 4a: per-corpus-type threshold defaults."""

    def test_default_threshold_code_prefix(self) -> None:
        from nexus.corpus import default_projection_threshold
        assert default_projection_threshold("code__foo") == 0.70

    def test_default_threshold_knowledge_prefix(self) -> None:
        from nexus.corpus import default_projection_threshold
        assert default_projection_threshold("knowledge__bar") == 0.50

    def test_default_threshold_docs_and_rdr(self) -> None:
        from nexus.corpus import default_projection_threshold
        assert default_projection_threshold("docs__mix") == 0.55
        assert default_projection_threshold("rdr__alpha") == 0.55

    def test_default_threshold_unknown_prefix_fallback(self) -> None:
        from nexus.corpus import default_projection_threshold
        # Unknown prefix → safe under-match bias at 0.70.
        assert default_projection_threshold("other__weird") == 0.70


class TestProjectAgainstIcf:
    """``project_against(icf_map=...)`` — weighting at filter time only."""

    def _seed_two_corpora(
        self, db: T2Database, chroma_client: chromadb.ClientAPI,
    ) -> None:
        rng = np.random.default_rng(42)
        for name in ("code__icfA", "code__icfB"):
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

    def test_icf_suppresses_hub_topics_below_threshold(
        self, db: T2Database, chroma_client: chromadb.ClientAPI,
    ) -> None:
        """A topic with ICF=0 must fail threshold regardless of raw cosine."""
        self._seed_two_corpora(db, chroma_client)
        # Without ICF: at low threshold we get matches.
        baseline = db.taxonomy.project_against(
            "code__icfA", ["code__icfB"], chroma_client, threshold=0.1,
        )
        assert baseline["chunk_assignments"], "baseline should match"

        # Craft an ICF map that zeros out every target topic — equivalent
        # to every topic being ubiquitous. Filter drops everything.
        zero_icf = {
            m["topic_id"]: 0.0 for m in baseline["matched_topics"]
        }
        result = db.taxonomy.project_against(
            "code__icfA", ["code__icfB"], chroma_client,
            threshold=0.1, icf_map=zero_icf,
        )
        assert not result["chunk_assignments"], (
            "zero-ICF topics must be filtered out before persistence"
        )
        assert len(result["novel_chunks"]) == result["total_chunks"]

    def test_stored_similarity_is_raw_cosine_even_with_icf(
        self, db: T2Database, chroma_client: chromadb.ClientAPI,
    ) -> None:
        """Raw cosine stored; ICF only affects what gets through the filter."""
        self._seed_two_corpora(db, chroma_client)
        baseline = db.taxonomy.project_against(
            "code__icfA", ["code__icfB"], chroma_client, threshold=0.1,
        )
        raw_lookup = {(d, t): s for d, t, s in baseline["chunk_assignments"]}

        # High-ICF map (2.0 everywhere) — doubles the adjusted score but the
        # raw cosine returned in chunk_assignments must be unchanged.
        high_icf = {m["topic_id"]: 2.0 for m in baseline["matched_topics"]}
        weighted = db.taxonomy.project_against(
            "code__icfA", ["code__icfB"], chroma_client,
            threshold=0.1, icf_map=high_icf,
        )
        for d, t, s in weighted["chunk_assignments"]:
            if (d, t) in raw_lookup:
                assert s == pytest.approx(raw_lookup[(d, t)]), (
                    "icf_map must not mutate stored raw cosine"
                )

    def test_missing_topic_in_icf_map_defaults_to_one(
        self, db: T2Database, chroma_client: chromadb.ClientAPI,
    ) -> None:
        """ICF map lookup missing entries → weight 1.0 (no suppression)."""
        self._seed_two_corpora(db, chroma_client)
        # Empty ICF map — every target topic defaults to 1.0, result matches
        # the baseline no-icf case.
        baseline = db.taxonomy.project_against(
            "code__icfA", ["code__icfB"], chroma_client, threshold=0.1,
        )
        with_empty_icf = db.taxonomy.project_against(
            "code__icfA", ["code__icfB"], chroma_client,
            threshold=0.1, icf_map={},
        )
        assert (
            len(baseline["chunk_assignments"])
            == len(with_empty_icf["chunk_assignments"])
        )


class TestProjectCmdFlag:
    """CLI flag wiring for ``nx taxonomy project --use-icf``."""

    def test_project_cmd_has_use_icf_flag(self) -> None:
        from click.testing import CliRunner

        from nexus.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["taxonomy", "project", "--help"])
        assert result.exit_code == 0
        assert "--use-icf" in result.output
        assert "ICF" in result.output

    def test_project_cmd_help_mentions_corpus_defaults(self) -> None:
        from click.testing import CliRunner

        from nexus.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["taxonomy", "project", "--help"])
        assert "code__*" in result.output or "0.70" in result.output
        assert "knowledge__*" in result.output or "0.50" in result.output
        # Reference to tuning doc for operators. Click wraps at hyphens,
        # inserting "- " breaks, so normalise before the containment check.
        collapsed = " ".join(result.output.split()).replace("- ", "-")
        assert "taxonomy-projection-tuning.md" in collapsed


# ── Phase 5 (nexus-84v) — nx taxonomy hubs ──────────────────────────────────


@pytest.fixture()
def fixture_hub_synthetic(db: T2Database) -> T2Database:
    """5 collections × 100 docs — half assigned to a stopword-labeled hub,
    half spread across 5 distinct domain topics (one per collection).

    Deterministic: fixed doc_ids and assigned_at values so tests don't
    depend on wall-clock drift.
    """
    topics = [
        (1, "assert helpers",            "code__c0", "2026-04-01"),
        (2, "ingest-pipeline",           "code__c0", "2026-04-01"),
        (3, "member-proposal-workflow",  "code__c1", "2026-04-01"),
        (4, "payroll-audit",             "code__c2", "2026-04-01"),
        (5, "ballot-scanner",            "code__c3", "2026-04-01"),
        (6, "treasury-reconciliation",   "code__c4", "2026-04-01"),
    ]
    for tid, label, collection, created in topics:
        db.taxonomy.conn.execute(
            "INSERT INTO topics (id, label, collection, created_at) "
            "VALUES (?, ?, ?, ?)",
            (tid, label, collection, created),
        )

    # Hub topic 1: 50 docs from every one of 5 collections.
    for col_idx in range(5):
        col = f"code__c{col_idx}"
        for d in range(50):
            db.taxonomy.assign_topic(
                f"{col}-hub-d{d}", 1,
                assigned_by="projection",
                similarity=0.85,
                source_collection=col,
                assigned_at=f"2026-04-10T12:{col_idx:02d}:00",
            )
    # Five domain topics: topic 2..6 each gets 50 docs from one collection.
    for col_idx, tid in enumerate((2, 3, 4, 5, 6)):
        col = f"code__c{col_idx}"
        for d in range(50):
            db.taxonomy.assign_topic(
                f"{col}-dom-d{d}", tid,
                assigned_by="projection",
                similarity=0.82,
                source_collection=col,
                assigned_at=f"2026-04-10T13:{col_idx:02d}:00",
            )
    db.taxonomy.clear_icf_cache()
    return db


class TestHubs:
    def test_hubs_detects_stopword_topic(
        self, fixture_hub_synthetic: T2Database,
    ) -> None:
        hubs = fixture_hub_synthetic.taxonomy.detect_hubs(min_collections=2)
        topic_ids = [h.topic_id for h in hubs]
        # Only topic 1 spans all 5 collections.
        assert 1 in topic_ids
        assert hubs[0].topic_id == 1  # sorted by score desc
        assert "assert" in hubs[0].matched_stopwords

    def test_hubs_excludes_single_collection_domain_topics(
        self, fixture_hub_synthetic: T2Database,
    ) -> None:
        hubs = fixture_hub_synthetic.taxonomy.detect_hubs(min_collections=2)
        topic_ids = {h.topic_id for h in hubs}
        # Domain topics each live in a single source collection → DF=1,
        # excluded by min_collections=2.
        for domain_topic in (2, 3, 4, 5, 6):
            assert domain_topic not in topic_ids

    def test_hubs_max_icf_threshold(
        self, fixture_hub_synthetic: T2Database,
    ) -> None:
        # With N_effective=5 and the hub topic at DF=5, ICF=log2(1)=0.
        hubs = fixture_hub_synthetic.taxonomy.detect_hubs(
            min_collections=2, max_icf=0.5,
        )
        assert [h.topic_id for h in hubs] == [1]

        # No ICF filter ever, but also no label stopword filter — so every
        # DF≥2 topic shows up. In this fixture only topic 1 has DF≥2.
        hubs_none = fixture_hub_synthetic.taxonomy.detect_hubs(
            min_collections=2, max_icf=None,
        )
        assert [h.topic_id for h in hubs_none] == [1]

    def test_hubs_min_collections_threshold(
        self, fixture_hub_synthetic: T2Database,
    ) -> None:
        # Asking for DF>=6 → nothing (we only have 5 collections).
        hubs = fixture_hub_synthetic.taxonomy.detect_hubs(min_collections=6)
        assert hubs == []

    def test_hubs_warn_stale_compares_to_last_discover(
        self, fixture_hub_synthetic: T2Database,
    ) -> None:
        """MAX(last_discover_at) across source collections, not single row."""
        # Mark each source collection as discovered BEFORE the hub's latest
        # assignment → stale should fire.
        for col_idx in range(5):
            fixture_hub_synthetic.taxonomy.conn.execute(
                "INSERT INTO taxonomy_meta "
                "(collection, last_discover_doc_count, last_discover_at) "
                "VALUES (?, 100, ?)",
                (f"code__c{col_idx}", "2026-04-09T00:00:00"),
            )
        fixture_hub_synthetic.taxonomy.conn.commit()

        hubs = fixture_hub_synthetic.taxonomy.detect_hubs(
            min_collections=2, warn_stale=True,
        )
        assert hubs[0].is_stale is True
        # The MAX across all 5 rows is the largest of the identical values.
        assert hubs[0].max_last_discover_at == "2026-04-09T00:00:00"

        # Now update ONE collection to post-date the hub's latest
        # assigned_at. MAX() aggregation must pick that up across ALL
        # contributing collections (C-2 correctness), not stay stuck on a
        # single-row lookup.
        fixture_hub_synthetic.taxonomy.conn.execute(
            "UPDATE taxonomy_meta SET last_discover_at = ? "
            "WHERE collection = 'code__c0'",
            ("2026-04-11T00:00:00",),
        )
        fixture_hub_synthetic.taxonomy.conn.commit()

        hubs2 = fixture_hub_synthetic.taxonomy.detect_hubs(
            min_collections=2, warn_stale=True,
        )
        # Hub's latest assigned_at is 2026-04-10T13:04:00 (<
        # 2026-04-11T00:00:00 after the update). Not stale anymore.
        assert hubs2[0].is_stale is False
        assert hubs2[0].max_last_discover_at == "2026-04-11T00:00:00"

    def test_hubs_warn_stale_null_handling(
        self, fixture_hub_synthetic: T2Database,
    ) -> None:
        """Never-discovered collections count as stale via never_discovered_count."""
        # Insert NULL rows for some collections; leave others absent entirely.
        for col_idx in range(3):
            fixture_hub_synthetic.taxonomy.conn.execute(
                "INSERT INTO taxonomy_meta "
                "(collection, last_discover_doc_count, last_discover_at) "
                "VALUES (?, 100, NULL)",
                (f"code__c{col_idx}",),
            )
        fixture_hub_synthetic.taxonomy.conn.commit()

        hubs = fixture_hub_synthetic.taxonomy.detect_hubs(
            min_collections=2, warn_stale=True,
        )
        # 3 explicit NULL rows + 2 collections with no taxonomy_meta row at
        # all = 5 never-discovered source collections.
        assert hubs[0].never_discovered_count == 5
        assert hubs[0].max_last_discover_at is None
        assert hubs[0].is_stale is True

    def test_hubs_warn_stale_without_flag_leaves_fields_default(
        self, fixture_hub_synthetic: T2Database,
    ) -> None:
        hubs = fixture_hub_synthetic.taxonomy.detect_hubs(min_collections=2)
        assert hubs[0].max_last_discover_at is None
        assert hubs[0].never_discovered_count == 0
        assert hubs[0].is_stale is False

    def test_hubs_cli_flag_wiring(self) -> None:
        from click.testing import CliRunner

        from nexus.cli import main

        result = CliRunner().invoke(main, ["taxonomy", "hubs", "--help"])
        assert result.exit_code == 0
        for flag in ("--min-collections", "--max-icf", "--warn-stale", "--explain"):
            assert flag in result.output
        # Points the operator at the tuning doc.
        collapsed = " ".join(result.output.split()).replace("- ", "-")
        assert "taxonomy-projection-tuning.md" in collapsed
