# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end taxonomy pipeline tests (RDR-070).

Full pipeline with real ChromaDB (EphemeralClient + PersistentClient),
real MiniLM embeddings, real HDBSCAN clustering. No mocks. Tests the
complete flow: index → discover → review → manual ops → rebuild →
search → boost → links.

Both HNSW spaces tested: cosine (taxonomy centroids) and L2 (default
ChromaDB collections). PersistentClient tested alongside EphemeralClient
to verify the on-disk path behaves identically.
"""
from __future__ import annotations

from pathlib import Path

import chromadb
import numpy as np
import pytest

from nexus.db.local_ef import LocalEmbeddingFunction
from nexus.db.t2 import T2Database
from nexus.types import SearchResult


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def ef() -> LocalEmbeddingFunction:
    return LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")


@pytest.fixture()
def ephemeral_chroma() -> chromadb.ClientAPI:
    return chromadb.EphemeralClient()


@pytest.fixture()
def persistent_chroma(tmp_path: Path) -> chromadb.ClientAPI:
    return chromadb.PersistentClient(path=str(tmp_path / "chroma"))


# ── Test corpus ───────────────────────────────────────────────────────────────

def _build_corpus() -> tuple[list[str], list[str]]:
    """Return (doc_ids, texts) for a 60-doc test corpus.

    Three domains (HTTP, database, test) with 20 docs each. Enough
    for HDBSCAN to find clusters with real MiniLM embeddings.
    """
    http = [
        f"def handle_request(request): response = json_response(status={200 + i}); return response"
        for i in range(10)
    ] + [
        f"@app.route('/api/v{i}') def endpoint(): return jsonify(data)"
        for i in range(10)
    ]
    db = [
        f"cursor.execute('SELECT id, name FROM users WHERE age > {i}') rows = cursor.fetchall()"
        for i in range(10)
    ] + [
        f"conn.execute('INSERT INTO logs (event, ts) VALUES (?, ?)', (event_{i}, now()))"
        for i in range(10)
    ]
    test = [
        f"def test_create_user_{i}(db): user = db.put(name='test') assert user.id is not None"
        for i in range(10)
    ] + [
        f"@pytest.fixture def mock_client_{i}(): return MockClient(timeout={i})"
        for i in range(10)
    ]

    texts = http + db + test
    doc_ids = (
        [f"src/http/{i}.py" for i in range(20)]
        + [f"src/db/{i}.py" for i in range(20)]
        + [f"tests/test_{i}.py" for i in range(20)]
    )
    return doc_ids, texts


def _domain(doc_id: str) -> str:
    if doc_id.startswith("src/http"):
        return "http"
    if doc_id.startswith("src/db"):
        return "db"
    return "test"


# ── E2E pipeline tests ───────────────────────────────────────────────────────


class TestFullPipelineEphemeral:
    """Complete taxonomy pipeline with EphemeralClient (in-memory HNSW)."""

    def test_discover_produces_coherent_topics(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: chromadb.ClientAPI,
    ) -> None:
        """Discover → topics have coherent c-TF-IDF labels from real embeddings."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            count = db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            assert count >= 2, f"Expected >=2 topics from 3 domains, got {count}"

            topics = db.taxonomy.get_topics()
            assert len(topics) >= 2

            # Topics should have non-empty labels and positive doc counts
            for t in topics:
                assert t["label"].strip()
                assert t["doc_count"] > 0

            # Most docs should be assigned (noise excluded)
            total_assigned = sum(t["doc_count"] for t in topics)
            assert total_assigned >= 30, f"Expected >=30 assigned, got {total_assigned}"

    def test_incremental_assignment_correct_topic(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: chromadb.ClientAPI,
    ) -> None:
        """assign_single routes a new doc to the semantically nearest topic."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            # New HTTP-like doc should be assigned to the HTTP cluster
            new_text = "def handle_post_request(request): data = request.json(); return created(data)"
            new_emb = np.array(ef([new_text])[0], dtype=np.float32)

            topic_id = db.taxonomy.assign_single(
                "code__e2e", new_emb, ephemeral_chroma,
            )
            assert topic_id is not None

            # Check that the assigned topic's docs are mostly HTTP
            topic_docs = db.taxonomy.get_all_topic_doc_ids(topic_id)
            http_count = sum(1 for d in topic_docs if d.startswith("src/http"))
            assert http_count >= len(topic_docs) // 2, (
                f"Expected HTTP-dominant topic, got {http_count}/{len(topic_docs)} HTTP docs"
            )

    def test_rebuild_preserves_operator_label(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: chromadb.ClientAPI,
    ) -> None:
        """Rebuild with merge strategy preserves renamed labels."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            # Rename a topic
            topics = db.taxonomy.get_topics()
            db.taxonomy.rename_topic(topics[0]["id"], "operator-approved-name")

            # Rebuild (same data → same clusters → should match centroid)
            new_count = db.taxonomy.rebuild_taxonomy(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )
            assert new_count >= 2

            new_topics = db.taxonomy.get_topics()
            new_labels = [t["label"] for t in new_topics]
            assert "operator-approved-name" in new_labels, (
                f"Operator label lost after rebuild. Labels: {new_labels}"
            )

    def test_manual_assign_preserved_across_rebuild(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: chromadb.ClientAPI,
    ) -> None:
        """Manual assignment survives rebuild via old→new topic mapping."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            topics = db.taxonomy.get_topics()
            db.taxonomy.assign_topic("manual-doc", topics[0]["id"], assigned_by="manual")

            db.taxonomy.rebuild_taxonomy(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            # Manual assignment should be preserved
            rows = db.taxonomy.conn.execute(
                "SELECT assigned_by FROM topic_assignments WHERE doc_id = 'manual-doc'"
            ).fetchall()
            assert any(r[0] == "manual" for r in rows), "Manual assignment lost"

    def test_merge_and_split_roundtrip(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: chromadb.ClientAPI,
    ) -> None:
        """Merge two topics → split result → verify doc assignment integrity."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        # Seed T3 collection for split
        coll = ephemeral_chroma.get_or_create_collection(
            "code__e2e", embedding_function=None,
        )
        coll.add(ids=doc_ids, documents=texts, embeddings=ef(texts))

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            topics = db.taxonomy.get_topics()
            assert len(topics) >= 2
            t1, t2 = topics[0], topics[1]
            t1_docs = set(db.taxonomy.get_all_topic_doc_ids(t1["id"]))
            t2_docs = set(db.taxonomy.get_all_topic_doc_ids(t2["id"]))
            all_docs_before = t1_docs | t2_docs

            # Merge t2 into t1
            db.taxonomy.merge_topics(
                t2["id"], t1["id"], chroma_client=ephemeral_chroma,
            )

            merged_docs = set(db.taxonomy.get_all_topic_doc_ids(t1["id"]))
            assert merged_docs == all_docs_before, "Merge lost documents"

            # Split t1 into 2 children
            child_count = db.taxonomy.split_topic(
                t1["id"], k=2, chroma_client=ephemeral_chroma,
            )
            assert child_count == 2

            # All docs should be in children, none in parent
            children = db.taxonomy.get_topics(parent_id=t1["id"])
            child_docs = set()
            for c in children:
                child_docs |= set(db.taxonomy.get_all_topic_doc_ids(c["id"]))
            assert child_docs == all_docs_before, "Split lost documents"
            assert len(db.taxonomy.get_topic_doc_ids(t1["id"])) == 0

    def test_topic_boost_reduces_distance(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: chromadb.ClientAPI,
    ) -> None:
        """apply_topic_boost reduces distance for same-topic results."""
        from nexus.scoring import _TOPIC_SAME_BOOST, apply_topic_boost

        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            # Simulate search results from the HTTP domain
            results = [
                SearchResult(
                    id=f"src/http/{i}.py", content=f"http handler {i}",
                    distance=0.3 + i * 0.01, collection="code__e2e",
                )
                for i in range(5)
            ]
            assignments = db.taxonomy.get_assignments_for_docs(
                [r.id for r in results],
            )

            if not assignments:
                pytest.skip("No assignments for test docs")

            original_distances = [r.distance for r in results]
            apply_topic_boost(results, assignments)

            # At least some results should have reduced distance
            boosted = sum(
                1 for orig, r in zip(original_distances, results)
                if r.distance < orig
            )
            assert boosted >= 2, f"Expected >=2 boosted results, got {boosted}"

            # Boost amount should be exactly _TOPIC_SAME_BOOST for same-topic pairs
            for orig, r in zip(original_distances, results):
                if r.distance < orig:
                    assert abs((orig - r.distance) - _TOPIC_SAME_BOOST) < 0.001

    def test_review_workflow(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: chromadb.ClientAPI,
    ) -> None:
        """Review → accept/rename/delete → verify state changes."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            # All topics start as pending
            unreviewed = db.taxonomy.get_unreviewed_topics(collection="code__e2e")
            assert len(unreviewed) >= 2

            # Accept first topic
            db.taxonomy.mark_topic_reviewed(unreviewed[0]["id"], "accepted")
            remaining = db.taxonomy.get_unreviewed_topics(collection="code__e2e")
            assert len(remaining) == len(unreviewed) - 1

            # Rename second topic
            db.taxonomy.rename_topic(unreviewed[1]["id"], "custom-name")
            topic = db.taxonomy.get_topic_by_id(unreviewed[1]["id"])
            assert topic["label"] == "custom-name"
            assert topic["review_status"] == "accepted"

    def test_rebalance_trigger(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: chromadb.ClientAPI,
    ) -> None:
        """Rebalance detects 2x growth after discover."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            # After discover with 60 docs, doc count is recorded as 60
            # No rebalance at same count or 1.5x
            assert db.taxonomy.needs_rebalance("code__e2e", current_count=60) is False
            assert db.taxonomy.needs_rebalance("code__e2e", current_count=90) is False
            # Rebalance triggers at 2x (120)
            assert db.taxonomy.needs_rebalance("code__e2e", current_count=120) is True

    def test_topic_links_persist_and_read(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: chromadb.ClientAPI,
    ) -> None:
        """topic_links table persists and is readable at search time."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            topics = db.taxonomy.get_topics()
            if len(topics) < 2:
                pytest.skip("Need >=2 topics for link test")

            # Manually insert topic links
            t1, t2 = topics[0], topics[1]
            db.taxonomy.upsert_topic_links([
                {
                    "from_topic_id": t1["id"],
                    "to_topic_id": t2["id"],
                    "link_count": 5,
                    "link_types": ["cites", "implements"],
                },
            ])

            # Read back at search time
            pairs = db.taxonomy.get_topic_link_pairs([t1["id"], t2["id"]])
            assert (t1["id"], t2["id"]) in pairs
            assert pairs[(t1["id"], t2["id"])] == 5


class TestFullPipelinePersistent:
    """Same pipeline with PersistentClient (on-disk HNSW)."""

    def test_discover_and_assign_persistent(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, persistent_chroma: chromadb.ClientAPI,
    ) -> None:
        """Full discover + incremental assign works with PersistentClient."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            count = db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, persistent_chroma,
            )
            assert count >= 2

            # Incremental assignment
            new_text = "cursor.execute('SELECT * FROM orders WHERE total > 100')"
            new_emb = np.array(ef([new_text])[0], dtype=np.float32)

            topic_id = db.taxonomy.assign_single(
                "code__e2e", new_emb, persistent_chroma,
            )
            assert topic_id is not None

            # Verify centroid collection uses cosine space
            centroid_coll = persistent_chroma.get_collection(
                "taxonomy__centroids", embedding_function=None,
            )
            meta = centroid_coll.metadata
            assert meta.get("hnsw:space") == "cosine"

    def test_rebuild_persistent(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, persistent_chroma: chromadb.ClientAPI,
    ) -> None:
        """Rebuild with merge strategy works on persistent storage."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, persistent_chroma,
            )
            db.taxonomy.rename_topic(
                db.taxonomy.get_topics()[0]["id"], "persistent-label",
            )

            count = db.taxonomy.rebuild_taxonomy(
                "code__e2e", doc_ids, embeddings, texts, persistent_chroma,
            )
            assert count >= 2

            labels = [t["label"] for t in db.taxonomy.get_topics()]
            assert "persistent-label" in labels


class TestCentroidSpaceConsistency:
    """Verify centroid collection uses cosine space across all operations."""

    def test_discover_creates_cosine_centroids(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: chromadb.ClientAPI,
    ) -> None:
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

        coll = ephemeral_chroma.get_collection(
            "taxonomy__centroids", embedding_function=None,
        )
        assert coll.metadata.get("hnsw:space") == "cosine"

    def test_split_creates_cosine_child_centroids(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: chromadb.ClientAPI,
    ) -> None:
        doc_ids, texts = _build_corpus()
        coll = ephemeral_chroma.get_or_create_collection(
            "code__e2e", embedding_function=None,
        )
        emb_list = ef(texts)
        coll.add(ids=doc_ids, documents=texts, embeddings=emb_list)

        with T2Database(tmp_path / "e2e.db") as db:
            embeddings = np.array(emb_list, dtype=np.float32)
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            topic = db.taxonomy.get_topics()[0]
            db.taxonomy.split_topic(topic["id"], k=2, chroma_client=ephemeral_chroma)

        # Centroid collection should still be cosine
        centroid_coll = ephemeral_chroma.get_collection(
            "taxonomy__centroids", embedding_function=None,
        )
        assert centroid_coll.metadata.get("hnsw:space") == "cosine"

        # Child centroids should exist
        data = centroid_coll.get(
            where={"collection": "code__e2e"}, include=["metadatas"],
        )
        assert len(data["ids"]) >= 2  # at least the 2 children


class TestCrossCollectionIsolation:
    """Topics from different collections must not leak into each other."""

    def test_assign_single_isolated(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: chromadb.ClientAPI,
    ) -> None:
        """assign_single for collection B returns None when only A has centroids."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__project_a", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            # New doc aimed at project_b — should NOT get a project_a topic
            new_emb = np.array(ef(["some random text"])[0], dtype=np.float32)
            result = db.taxonomy.assign_single(
                "code__project_b", new_emb, ephemeral_chroma,
            )
            assert result is None

    def test_discover_separate_collections(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: chromadb.ClientAPI,
    ) -> None:
        """Two collections get independent topic sets."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            count_a = db.taxonomy.discover_topics(
                "code__alpha", doc_ids, embeddings, texts, ephemeral_chroma,
            )
            count_b = db.taxonomy.discover_topics(
                "code__beta", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            assert count_a >= 2
            assert count_b >= 2

            topics_a = db.taxonomy.get_topics_for_collection("code__alpha")
            topics_b = db.taxonomy.get_topics_for_collection("code__beta")

            # Each collection has its own topics
            assert len(topics_a) == count_a
            assert len(topics_b) == count_b
            ids_a = {t["id"] for t in topics_a}
            ids_b = {t["id"] for t in topics_b}
            assert ids_a.isdisjoint(ids_b)
