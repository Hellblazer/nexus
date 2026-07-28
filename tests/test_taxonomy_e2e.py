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

import numpy as np
import pytest

from nexus.db.local_ef import LocalEmbeddingFunction
from nexus.db.t2 import T2Database
from nexus.types import SearchResult
from tests.conftest import engine_substrate_selected, make_vector_test_client
from typing import Any

# Full e2e pipeline: real ChromaDB clients (Ephemeral + Persistent), real
# MiniLM embeddings, real HDBSCAN clustering. ~2.9s/test average on CI,
# 43s total. Belongs under the ``integration`` marker per project
# convention so default ``pytest`` deselects it; run explicitly with
# ``uv run pytest -m integration``.
pytestmark = pytest.mark.integration


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def ef() -> LocalEmbeddingFunction:
    return LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")


@pytest.fixture()
def ephemeral_chroma() -> Any:
    return make_vector_test_client()


@pytest.fixture()
def isolated_vector_client() -> Any:
    """A vector client isolated from every other test's.

    Was ``chromadb.PersistentClient(tmp_path)`` — on-disk purely to dodge
    EphemeralClient's process-wide shared backend, never because these tests
    need durability. ``make_vector_test_client()`` returns a fresh
    InMemoryVectorClient per call, so isolation is now by construction and the
    tmp_path is no longer needed.
    """
    return make_vector_test_client()


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


def _assert_centroid_space_is_cosine(
    db: T2Database, collection: str, probe: np.ndarray, client: Any,
) -> None:
    """Assert the centroid space is COSINE, through the public assign surface.

    Replaces a read of the chroma centroid collection's ``hnsw:space``
    metadata. That read lost its subject at RDR-155 P4b: centroids no longer
    land in the caller's vector client at all on the Http twin — they route
    through the engine's pgvector centroid port, whose cosine-ness is declared
    by ``taxonomy-002-centroids.xml`` (``hnsw (embedding vector_cosine_ops)``)
    and the ``<=>`` operator in ``TaxonomyCentroidRepository``. So the
    assertion moves from the storage metadata to the behaviour it existed to
    protect, and holds on both twins.

    DISCRIMINATING, not descriptive: cosine similarity ignores the query
    vector's magnitude, L2 distance and inner product do not. Neither twin's
    ``assign_single`` normalizes its input (both hand the vector straight to
    the index), so scaling the probe by 10x must leave both the winning topic
    and the similarity unchanged under cosine, and would move at least the
    similarity under either alternative.
    """
    base = db.taxonomy.assign_single(collection, probe, client)
    scaled = db.taxonomy.assign_single(
        collection, (probe * 10.0).astype(np.float32), client,
    )
    assert base is not None, "no centroid to probe — discover produced none"
    assert scaled is not None
    assert scaled.topic_id == base.topic_id, (
        "centroid ranking changed under a pure rescale of the query — "
        "the centroid space is not cosine"
    )
    assert scaled.similarity == pytest.approx(base.similarity, abs=1e-5), (
        f"similarity moved under a pure rescale ({base.similarity} -> "
        f"{scaled.similarity}) — the centroid space is not cosine"
    )


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
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: Any,
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
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: Any,
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

            result = db.taxonomy.assign_single(
                "code__e2e", new_emb, ephemeral_chroma,
            )
            assert result is not None
            topic_id = result.topic_id

            # Check that the assigned topic's docs are mostly HTTP
            topic_docs = db.taxonomy.get_all_topic_doc_ids(topic_id)
            http_count = sum(1 for d in topic_docs if d.startswith("src/http"))
            assert http_count >= len(topic_docs) // 2, (
                f"Expected HTTP-dominant topic, got {http_count}/{len(topic_docs)} HTTP docs"
            )

    def test_rebuild_preserves_operator_label(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: Any,
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
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: Any,
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

            # Manual assignment should be preserved — read through the
            # public rebuild-state surface (manual_assignments is exactly
            # the assigned_by='manual' row set; RDR-155 P4b P0a').
            # centroid_coll: required positionally by the SQLite oracle,
            # ignored by the Http twin (centroids route through the port).
            manual = db.taxonomy.read_rebuild_old_state(
                "code__e2e", ephemeral_chroma.get_or_create_collection("taxonomy__centroids"),
            )["manual_assignments"]
            assert "manual-doc" in manual, "Manual assignment lost"

    def test_merge_and_split_roundtrip(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: Any,
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
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: Any,
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
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: Any,
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
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: Any,
    ) -> None:
        """Rebalance detects growth after discover, on whichever twin is live.

        The two twins use DIFFERENT thresholds and that divergence is
        deliberate: ``CatalogTaxonomy`` triggers at >= 2x, ``HttpTaxonomyStore``
        at > 5% growth, and the 2x semantics is on the RDR-155 P4b dies-roster
        (see ``tests/test_taxonomy.py::TestRebalanceTrigger::
        test_below_threshold_no_rebalance``). This e2e copy asserted the 2x
        contract unconditionally and so began failing at the nexus-aqbrk
        substrate flip, when ``T2Database`` started handing out the Http twin.

        The no-growth and 2x-growth answers agree on both twins; only the 1.5x
        answer discriminates, so that one is asserted per substrate rather than
        dropped — a shared-answers-only test would pass against either
        threshold and pin neither.
        """
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            # After discover with 60 docs, doc count is recorded as 60.
            # No growth: no rebalance on either twin.
            assert db.taxonomy.needs_rebalance("code__e2e", current_count=60) is False
            # 1.5x: under the oracle's 2x bar, over the engine's 5% bar.
            assert db.taxonomy.needs_rebalance(
                "code__e2e", current_count=90,
            ) is engine_substrate_selected()
            # 2x: rebalance on either twin.
            assert db.taxonomy.needs_rebalance("code__e2e", current_count=120) is True

    def test_topic_links_persist_and_read(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: Any,
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
        self, tmp_path: Path, ef: LocalEmbeddingFunction, isolated_vector_client: Any,
    ) -> None:
        """Full discover + incremental assign works with PersistentClient."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            count = db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, isolated_vector_client,
            )
            assert count >= 2

            # Incremental assignment
            new_text = "cursor.execute('SELECT * FROM orders WHERE total > 100')"
            new_emb = np.array(ef([new_text])[0], dtype=np.float32)

            result = db.taxonomy.assign_single(
                "code__e2e", new_emb, isolated_vector_client,
            )
            assert result is not None

            _assert_centroid_space_is_cosine(
                db, "code__e2e", new_emb, isolated_vector_client,
            )

    def test_rebuild_persistent(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, isolated_vector_client: Any,
    ) -> None:
        """Rebuild with merge strategy works on persistent storage."""
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, isolated_vector_client,
            )
            db.taxonomy.rename_topic(
                db.taxonomy.get_topics()[0]["id"], "persistent-label",
            )

            count = db.taxonomy.rebuild_taxonomy(
                "code__e2e", doc_ids, embeddings, texts, isolated_vector_client,
            )
            assert count >= 2

            labels = [t["label"] for t in db.taxonomy.get_topics()]
            assert "persistent-label" in labels


class TestCentroidSpaceConsistency:
    """Verify centroid similarity stays cosine across all operations.

    Asserted through the public assign surface rather than through the chroma
    centroid collection's ``hnsw:space`` metadata — see
    :func:`_assert_centroid_space_is_cosine` for why that read no longer has a
    subject and how the replacement discriminates cosine from L2.
    """

    def test_discover_creates_cosine_centroids(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: Any,
    ) -> None:
        doc_ids, texts = _build_corpus()
        embeddings = np.array(ef(texts), dtype=np.float32)

        with T2Database(tmp_path / "e2e.db") as db:
            db.taxonomy.discover_topics(
                "code__e2e", doc_ids, embeddings, texts, ephemeral_chroma,
            )

            _assert_centroid_space_is_cosine(
                db, "code__e2e", embeddings[0], ephemeral_chroma,
            )

    def test_split_creates_cosine_child_centroids(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: Any,
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
            assert db.taxonomy.split_topic(
                topic["id"], k=2, chroma_client=ephemeral_chroma,
            ) == 2

            # Child centroids exist and are still probed in cosine space.
            children = db.taxonomy.get_topics(parent_id=topic["id"])
            assert len(children) >= 2
            _assert_centroid_space_is_cosine(
                db, "code__e2e", embeddings[0], ephemeral_chroma,
            )


class TestCrossCollectionIsolation:
    """Topics from different collections must not leak into each other."""

    def test_assign_single_isolated(
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: Any,
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
        self, tmp_path: Path, ef: LocalEmbeddingFunction, ephemeral_chroma: Any,
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
