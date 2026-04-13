# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for persistent topic taxonomy (RDR-061 P3-1, nexus-vk8m; RDR-070 nexus-9k5)."""
from __future__ import annotations

from pathlib import Path

import chromadb
import numpy as np
import pytest

from nexus.db.t2 import T2Database
from nexus.taxonomy import (
    assign_topic,
    get_topic_docs,
    get_topic_tree,
    get_topics,
)


@pytest.fixture()
def chroma_client() -> chromadb.ClientAPI:
    """Ephemeral ChromaDB client for taxonomy centroid tests."""
    return chromadb.EphemeralClient()


# ── schema ──────────────────────────────────────────────────────────────────


def test_topics_table_created(db: T2Database) -> None:
    """topics and topic_assignments tables exist after T2Database init."""
    tables = {
        r[0] for r in db.taxonomy.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "topics" in tables
    assert "topic_assignments" in tables


# ── topic CRUD ──────────────────────────────────────────────────────────────


def test_get_topics_empty(db: T2Database) -> None:
    """No topics initially."""
    assert get_topics(db) == []


def test_discover_topics_creates_topics_and_centroids(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """discover_topics persists topics to T2 and upserts centroids to ChromaDB."""
    rng = np.random.default_rng(42)
    # Two well-separated clusters in 384d
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0

    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = (
        [f"machine learning neural network gradient {i}" for i in range(30)]
        + [f"database query indexing sql schema {i}" for i in range(30)]
    )

    count = db.taxonomy.discover_topics(
        "test__coll", doc_ids, embeddings, texts, chroma_client,
    )
    assert count >= 2

    topics = get_topics(db)
    assert len(topics) >= 2
    # Each topic has a c-TF-IDF label (not empty)
    assert all(t["label"] for t in topics)
    # doc_count populated
    assert all(t["doc_count"] > 0 for t in topics)
    assert sum(t["doc_count"] for t in topics) <= 60

    # Centroids upserted to taxonomy__centroids
    centroid_coll = chroma_client.get_collection(
        "taxonomy__centroids", embedding_function=None,
    )
    result = centroid_coll.get(include=["metadatas", "embeddings"])
    assert len(result["ids"]) >= 2
    # Centroid embeddings are 384d
    assert len(result["embeddings"][0]) == 384
    # Metadata has topic_id, label, collection
    meta = result["metadatas"][0]
    assert "topic_id" in meta
    assert "label" in meta
    assert meta["collection"] == "test__coll"


def test_assign_topic(db: T2Database) -> None:
    """Assign a doc_id to a topic."""
    # Create a topic first
    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("test-topic", "proj", 0, "2026-01-01T00:00:00Z"),
    )
    db.taxonomy.conn.commit()
    topic_id = db.taxonomy.conn.execute("SELECT id FROM topics LIMIT 1").fetchone()[0]

    assign_topic(db, "doc-123", topic_id)

    row = db.taxonomy.conn.execute(
        "SELECT * FROM topic_assignments WHERE doc_id='doc-123'"
    ).fetchone()
    assert row is not None
    assert row[1] == topic_id


def test_assign_topic_idempotent(db: T2Database) -> None:
    """Assigning same doc to same topic twice doesn't error."""
    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("test-topic", "proj", 0, "2026-01-01T00:00:00Z"),
    )
    db.taxonomy.conn.commit()
    topic_id = db.taxonomy.conn.execute("SELECT id FROM topics LIMIT 1").fetchone()[0]

    assign_topic(db, "doc-123", topic_id)
    assign_topic(db, "doc-123", topic_id)  # no error

    count = db.taxonomy.conn.execute(
        "SELECT count(*) FROM topic_assignments WHERE doc_id='doc-123'"
    ).fetchone()[0]
    assert count == 1


def test_rebuild_taxonomy_clears_and_rediscovers(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """rebuild_taxonomy deletes old topics, then re-discovers fresh ones."""
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = (
        [f"machine learning neural network {i}" for i in range(30)]
        + [f"database query sql schema {i}" for i in range(30)]
    )

    count1 = db.taxonomy.rebuild_taxonomy(
        "test__coll", doc_ids, embeddings, texts, chroma_client,
    )
    ids_after_first = {
        t["id"] for t in db.taxonomy.get_topics()
        if t.get("collection") == "test__coll"
    }

    count2 = db.taxonomy.rebuild_taxonomy(
        "test__coll", doc_ids, embeddings, texts, chroma_client,
    )
    ids_after_second = {
        t["id"] for t in db.taxonomy.get_topics()
        if t.get("collection") == "test__coll"
    }

    assert count1 >= 2
    assert count2 >= 2
    # Topic count should match the second run, not be doubled (no accumulation)
    assert len(ids_after_second) == count2
    # Total assignments should be consistent — not doubled
    total_assignments = db.taxonomy.conn.execute(
        "SELECT COUNT(*) FROM topic_assignments WHERE topic_id IN "
        "(SELECT id FROM topics WHERE collection = 'test__coll')"
    ).fetchone()[0]
    assert total_assignments <= 60, "rebuild should replace, not accumulate"


def test_get_topics_filtered_by_parent(db: T2Database) -> None:
    """get_topics(parent_id=None) returns only root topics."""
    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("root-topic", "proj", 5, "2026-01-01T00:00:00Z"),
    )
    root_id = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, parent_id, collection, doc_count, created_at) VALUES (?, ?, ?, ?, ?)",
        ("child-topic", root_id, "proj", 2, "2026-01-01T00:00:00Z"),
    )
    db.taxonomy.conn.commit()

    roots = get_topics(db, parent_id=None)
    assert len(roots) == 1
    assert roots[0]["label"] == "root-topic"

    children = get_topics(db, parent_id=root_id)
    assert len(children) == 1
    assert children[0]["label"] == "child-topic"


# ── tree + docs ─────────────────────────────────────────────────────────────


def test_get_topic_tree_structure(db: T2Database) -> None:
    """get_topic_tree returns nested dicts with children."""
    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("root", "proj", 10, "2026-01-01T00:00:00Z"),
    )
    root_id = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, parent_id, collection, doc_count, created_at) VALUES (?, ?, ?, ?, ?)",
        ("child", root_id, "proj", 3, "2026-01-01T00:00:00Z"),
    )
    db.taxonomy.conn.commit()

    tree = get_topic_tree(db, "proj")
    assert len(tree) == 1
    assert tree[0]["label"] == "root"
    assert len(tree[0]["children"]) == 1
    assert tree[0]["children"][0]["label"] == "child"


def test_get_topic_docs_returns_assigned(db: T2Database) -> None:
    """get_topic_docs returns doc_ids assigned to the topic."""
    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("test", "proj", 2, "2026-01-01T00:00:00Z"),
    )
    topic_id = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.taxonomy.conn.execute("INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)", ("doc-a", topic_id))
    db.taxonomy.conn.execute("INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)", ("doc-b", topic_id))
    db.taxonomy.conn.commit()

    docs = get_topic_docs(db, topic_id)
    assert len(docs) == 2
    assert {d["doc_id"] for d in docs} == {"doc-a", "doc-b"}


def test_discover_topics_all_noise_returns_zero(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """When HDBSCAN assigns all docs to noise (-1), return 0 and skip centroids."""
    rng = np.random.default_rng(42)
    # Too few scattered points — HDBSCAN cannot find clusters
    embeddings = rng.standard_normal((8, 384)).astype(np.float32) * 100
    doc_ids = [f"noise-{i}" for i in range(8)]
    texts = [f"completely unrelated text {i}" for i in range(8)]

    count = db.taxonomy.discover_topics(
        "test__coll", doc_ids, embeddings, texts, chroma_client,
    )
    assert count == 0
    assert db.taxonomy.get_topics() == []


def test_assign_single_returns_nearest_topic(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """assign_single returns the nearest topic_id via centroid ANN lookup."""
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = (
        [f"machine learning neural {i}" for i in range(30)]
        + [f"database query sql {i}" for i in range(30)]
    )

    db.taxonomy.discover_topics("test__coll", doc_ids, embeddings, texts, chroma_client)

    # New embedding near cluster A (dimension 0 shifted)
    new_emb = rng.standard_normal(384).astype(np.float32) * 0.1
    new_emb[0] += 3.0

    topic_id = db.taxonomy.assign_single("test__coll", new_emb, chroma_client)
    assert topic_id is not None
    assert isinstance(topic_id, int)

    # Verify it's assigned to a real topic in T2
    topics = db.taxonomy.get_topics()
    topic_ids = {t["id"] for t in topics}
    assert topic_id in topic_ids


def test_assign_single_no_centroids_returns_none(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """assign_single returns None when no centroids exist for the collection."""
    emb = np.random.default_rng(42).standard_normal(384).astype(np.float32)
    # Use a collection name with no centroids — EphemeralClient shares
    # in-process state, so centroids from other tests may exist.
    result = db.taxonomy.assign_single("nonexistent__coll", emb, chroma_client)
    assert result is None


def test_assign_single_cross_collection_isolation(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """assign_single returns None for collection B when centroids only exist for A."""
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = (
        [f"machine learning neural {i}" for i in range(30)]
        + [f"database query sql {i}" for i in range(30)]
    )

    # Discover for collection A — creates centroids
    db.taxonomy.discover_topics("coll_A", doc_ids, embeddings, texts, chroma_client)

    # Query for collection B — should return None, not a topic from A
    new_emb = rng.standard_normal(384).astype(np.float32) * 0.1
    new_emb[0] += 3.0  # similar to cluster A's centroid
    result = db.taxonomy.assign_single("coll_B", new_emb, chroma_client)
    assert result is None, "assign_single must not cross collection boundaries"


def test_assigned_by_column_populated(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """discover_topics sets assigned_by='hdbscan' on topic_assignment rows."""
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = (
        [f"machine learning neural {i}" for i in range(30)]
        + [f"database query sql {i}" for i in range(30)]
    )

    db.taxonomy.discover_topics("test__coll", doc_ids, embeddings, texts, chroma_client)

    rows = db.taxonomy.conn.execute(
        "SELECT DISTINCT assigned_by FROM topic_assignments"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "hdbscan"


def test_get_topic_docs_resolves_title_via_join(db: T2Database) -> None:
    """get_topic_docs JOINs on memory.title to resolve human-readable titles."""
    # Insert a memory entry — title must match doc_id AND project must match collection
    db.put(project="test", title="my-research-note", content="some content")

    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("topic", "test", 1, "2026-01-01T00:00:00Z"),
    )
    topic_id = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.taxonomy.conn.execute(
        "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
        ("my-research-note", topic_id),
    )
    db.taxonomy.conn.commit()

    docs = get_topic_docs(db, topic_id)
    assert len(docs) == 1
    assert docs[0]["doc_id"] == "my-research-note"
    assert docs[0]["title"] == "my-research-note"


def test_get_topic_docs_known_defect_project_collection_mismatch(db: T2Database) -> None:
    """RDR-063 Known Defect: get_topic_docs() JOIN conflates T3 collection with T2 project.

    This test DOCUMENTS the defect: when a topic's ``collection`` is a T3
    collection name (e.g. ``code__myrepo``) and the memory entry's ``project``
    is the T2 project name (e.g. ``myrepo``), the JOIN fails because the
    project ≠ collection comparison is false.

    The assertion uses a doc_id that does NOT match any memory.title, so
    the JOIN can't short-circuit. When the defect is fixed (JOIN semantics
    changed), this test will fail and must be rewritten.
    """
    # Memory entry with a DIFFERENT title from the topic's doc_id — so even
    # if the project/collection match, the title JOIN cannot find a hit.
    # This ensures we're testing the project-vs-collection mismatch, not
    # accidentally matching via title == doc_id.
    db.put(project="myrepo", title="unrelated-memory-title", content="some notes")

    # Topic associated with a T3 collection name (NOT the T2 project name)
    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("code topic", "code__myrepo", 1, "2026-01-01T00:00:00Z"),
    )
    topic_id = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # doc_id = "t3-chunk-id" — does NOT match any memory.title in the DB
    db.taxonomy.conn.execute(
        "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
        ("t3-chunk-id", topic_id),
    )
    db.taxonomy.conn.commit()

    docs = get_topic_docs(db, topic_id)
    assert len(docs) == 1
    assert docs[0]["doc_id"] == "t3-chunk-id"
    # Because the JOIN fails (project != collection AND title != doc_id),
    # title falls back to doc_id and project falls back to empty string.
    # When the defect is fixed, both fields should carry resolved values
    # that differ from these fallbacks — the test will fail and must be
    # rewritten alongside removing the Known Defect section from RDR-063.
    assert docs[0]["title"] == "t3-chunk-id", (
        "Known defect regression: get_topic_docs() now resolves T3-origin "
        "titles. Update this test to assert the new resolved title value "
        "and remove the Known Defect section from RDR-063."
    )
    assert docs[0]["project"] == "", (
        "Known defect regression: get_topic_docs() now resolves T3-origin "
        "project. Update this test alongside the title assertion."
    )


# ── Cascade on memory delete (v3.8.1) ─────────────────────────────────────


def test_memory_delete_cascades_topic_assignments(db: T2Database) -> None:
    """Deleting a memory entry removes its topic_assignments (v3.8.1 fix).

    Regression pin: pre-v3.8.1, ``nx memory delete`` left dangling
    ``topic_assignments`` rows whose ``doc_id`` referenced the deleted
    entry. Those orphans surfaced in ``nx taxonomy list`` / ``nx
    taxonomy show`` as ghost entries. The fix adds a cascade in the
    facade ``T2Database.delete()`` that calls
    ``CatalogTaxonomy.purge_assignments_for_doc()``.
    """
    # Seed memory entries for a project
    db.put(project="proj", title="doc-a", content="alpha content here")
    db.put(project="proj", title="doc-b", content="beta content here")

    # Seed a topic with both entries assigned
    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("test-topic", "proj", 2, "2026-01-01T00:00:00Z"),
    )
    topic_id = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.taxonomy.conn.executemany(
        "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
        [("doc-a", topic_id), ("doc-b", topic_id)],
    )
    db.taxonomy.conn.commit()

    # Sanity: both assignments present
    pre = db.taxonomy.conn.execute(
        "SELECT COUNT(*) FROM topic_assignments WHERE topic_id = ?", (topic_id,)
    ).fetchone()[0]
    assert pre == 2

    # Delete doc-a via the facade — should cascade-purge its assignment
    assert db.delete(project="proj", title="doc-a") is True

    post = db.taxonomy.conn.execute(
        "SELECT doc_id FROM topic_assignments WHERE topic_id = ? ORDER BY doc_id",
        (topic_id,),
    ).fetchall()
    assert [r[0] for r in post] == ["doc-b"], (
        "cascade should have removed doc-a's assignment but kept doc-b's"
    )

    # Topic still exists because doc-b still references it
    topics_remaining = db.taxonomy.conn.execute(
        "SELECT COUNT(*) FROM topics WHERE collection = 'proj'"
    ).fetchone()[0]
    assert topics_remaining == 1


def test_memory_delete_drops_empty_topics(db: T2Database) -> None:
    """Deleting the last memory entry in a topic also drops the topic."""
    db.put(project="proj", title="solo-doc", content="lonely content")
    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("solo-topic", "proj", 1, "2026-01-01T00:00:00Z"),
    )
    topic_id = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.taxonomy.conn.execute(
        "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
        ("solo-doc", topic_id),
    )
    db.taxonomy.conn.commit()

    assert db.delete(project="proj", title="solo-doc") is True

    # Assignment gone
    ta_count = db.taxonomy.conn.execute(
        "SELECT COUNT(*) FROM topic_assignments WHERE topic_id = ?", (topic_id,)
    ).fetchone()[0]
    assert ta_count == 0

    # Topic also gone (empty after the cascade)
    topic_count = db.taxonomy.conn.execute(
        "SELECT COUNT(*) FROM topics WHERE id = ?", (topic_id,)
    ).fetchone()[0]
    assert topic_count == 0


def test_memory_delete_cascade_scoped_to_project(db: T2Database) -> None:
    """Cascade only touches topics in the deleted entry's project.

    If two projects happen to have a memory entry with the same title,
    deleting one must not cascade-remove the other's topic assignment.
    """
    # Same title under two projects
    db.put(project="proj-a", title="shared-title", content="content under proj-a")
    db.put(project="proj-b", title="shared-title", content="content under proj-b")

    # Two topics, one per project, both assigning the shared title
    db.taxonomy.conn.executemany(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        [
            ("topic-a", "proj-a", 1, "2026-01-01T00:00:00Z"),
            ("topic-b", "proj-b", 1, "2026-01-01T00:00:00Z"),
        ],
    )
    topic_a_id, topic_b_id = [
        r[0] for r in db.taxonomy.conn.execute(
            "SELECT id FROM topics ORDER BY id DESC LIMIT 2"
        ).fetchall()
    ][::-1]
    db.taxonomy.conn.executemany(
        "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
        [("shared-title", topic_a_id), ("shared-title", topic_b_id)],
    )
    db.taxonomy.conn.commit()

    # Delete only the proj-a entry
    assert db.delete(project="proj-a", title="shared-title") is True

    # topic-a's assignment removed, topic-b's assignment untouched
    remaining = db.taxonomy.conn.execute(
        "SELECT topic_id FROM topic_assignments WHERE doc_id = 'shared-title'"
    ).fetchall()
    assert [r[0] for r in remaining] == [topic_b_id]


def test_memory_delete_by_id_cascades(db: T2Database) -> None:
    """Facade resolves project/title from --id before cascading."""
    row_id = db.put(project="proj", title="by-id", content="delete via numeric id")
    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("id-topic", "proj", 1, "2026-01-01T00:00:00Z"),
    )
    topic_id = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.taxonomy.conn.execute(
        "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
        ("by-id", topic_id),
    )
    db.taxonomy.conn.commit()

    assert db.delete(id=row_id) is True

    ta_count = db.taxonomy.conn.execute(
        "SELECT COUNT(*) FROM topic_assignments"
    ).fetchone()[0]
    assert ta_count == 0


def test_cli_taxonomy_list(tmp_path: Path) -> None:
    """CLI taxonomy list outputs topic labels and doc counts."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from nexus.commands.taxonomy_cmd import taxonomy

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("Search Methods", "proj", 5, "2026-01-01T00:00:00Z"),
        )
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("Database Queries", "proj", 3, "2026-01-01T00:00:00Z"),
        )
        db.taxonomy.conn.commit()

    runner = CliRunner()
    with patch(
        "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
    ):
        result = runner.invoke(taxonomy, ["list"])

    assert result.exit_code == 0, result.output
    assert "Search Methods" in result.output
    assert "Database Queries" in result.output
    assert "5 docs" in result.output
    assert "3 docs" in result.output


# ── discover_for_collection + CLI (RDR-070, nexus-2dq) ──────────────────────


def test_discover_for_collection(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """discover_for_collection fetches texts, embeds with MiniLM, runs discover_topics."""
    from nexus.commands.taxonomy_cmd import discover_for_collection
    from nexus.db.local_ef import LocalEmbeddingFunction

    # Seed a ChromaDB collection with documents
    ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    texts_a = [f"machine learning neural network gradient {i}" for i in range(30)]
    texts_b = [f"database query indexing sql schema {i}" for i in range(30)]
    texts = texts_a + texts_b
    doc_ids = [f"doc-{i}" for i in range(60)]

    coll = chroma_client.get_or_create_collection(
        "test__discover", embedding_function=None,
    )
    embeddings = ef(texts)
    coll.add(ids=doc_ids, documents=texts, embeddings=embeddings)

    count = discover_for_collection(
        "test__discover", db.taxonomy, chroma_client, force=False,
    )
    assert count >= 2
    topics = db.taxonomy.get_topics()
    assert len(topics) >= 2


def test_discover_for_collection_force(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """force=True clears existing topics before re-discovering fresh ones."""
    from nexus.commands.taxonomy_cmd import discover_for_collection
    from nexus.db.local_ef import LocalEmbeddingFunction

    ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    texts = (
        [f"machine learning neural {i}" for i in range(30)]
        + [f"database query sql {i}" for i in range(30)]
    )
    doc_ids = [f"doc-{i}" for i in range(60)]

    coll = chroma_client.get_or_create_collection(
        "test__force", embedding_function=None,
    )
    coll.add(ids=doc_ids, documents=texts, embeddings=ef(texts))

    count1 = discover_for_collection(
        "test__force", db.taxonomy, chroma_client, force=False,
    )
    ids_after_first = {
        t["id"] for t in db.taxonomy.get_topics()
        if t.get("collection") == "test__force"
    }

    count2 = discover_for_collection(
        "test__force", db.taxonomy, chroma_client, force=True,
    )
    ids_after_second = {
        t["id"] for t in db.taxonomy.get_topics()
        if t.get("collection") == "test__force"
    }

    assert count1 >= 2
    assert count2 >= 2
    # force=True should replace topics, not accumulate
    assert len(ids_after_second) == count2
    # Assignments should be consistent — not doubled
    total_assignments = db.taxonomy.conn.execute(
        "SELECT COUNT(*) FROM topic_assignments WHERE topic_id IN "
        "(SELECT id FROM topics WHERE collection = 'test__force')"
    ).fetchone()[0]
    assert total_assignments <= 60, "force rebuild should replace, not accumulate"


def test_discover_cli_invocation() -> None:
    """nx taxonomy discover --collection <name> exits 0 in local mode."""
    from unittest.mock import patch

    from click.testing import CliRunner
    from nexus.commands.taxonomy_cmd import taxonomy

    runner = CliRunner()
    with patch("nexus.commands.taxonomy_cmd.discover_for_collection", return_value=3) as mock_fn:
        result = runner.invoke(taxonomy, ["discover", "--collection", "test__coll"])

    assert result.exit_code == 0, result.output
    assert "3 topics" in result.output
    mock_fn.assert_called_once()


def test_rebuild_cli_is_discover_force_alias() -> None:
    """nx taxonomy rebuild --collection <name> delegates to discover --force."""
    from unittest.mock import patch

    from click.testing import CliRunner
    from nexus.commands.taxonomy_cmd import taxonomy

    runner = CliRunner()
    with patch("nexus.commands.taxonomy_cmd.discover_for_collection", return_value=2) as mock_fn:
        result = runner.invoke(taxonomy, ["rebuild", "--collection", "test__coll"])

    assert result.exit_code == 0, result.output
    mock_fn.assert_called_once()
    _, kwargs = mock_fn.call_args
    assert kwargs.get("force") is True


# ── MiniLM topic quality validation (RDR-070, nexus-7m8) ─────────────────────


class TestMiniLMTopicQuality:
    """Validate HDBSCAN topic quality on code-representative chunks.

    Uses LocalEmbeddingFunction (MiniLM 384d) for real semantic embeddings
    rather than random vectors — validates that the clustering pipeline
    produces coherent topics from identifier-heavy code text.
    """

    @pytest.fixture()
    def ef(self):
        from nexus.db.local_ef import LocalEmbeddingFunction
        return LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")

    @pytest.fixture()
    def chroma(self):
        return chromadb.EphemeralClient()

    def test_code_chunk_topic_quality(
        self, db: T2Database, ef, chroma,
    ) -> None:
        """Topics from code-like chunks show recognizable structural patterns."""
        # Three domains of code-like text
        http_chunks = [
            f"def handle_request(request): response = json_response(status={200+i}) return response"
            for i in range(20)
        ] + [
            f"@app.route('/api/v{i}') def endpoint(): return jsonify(data)"
            for i in range(10)
        ]
        db_chunks = [
            f"cursor.execute('SELECT id, name FROM users WHERE age > {i}') rows = cursor.fetchall()"
            for i in range(20)
        ] + [
            f"conn.execute('INSERT INTO logs (event, ts) VALUES (?, ?)', (event_{i}, now()))"
            for i in range(10)
        ]
        test_chunks = [
            f"def test_create_user_{i}(db): user = db.put(name='test') assert user.id is not None"
            for i in range(20)
        ] + [
            f"@pytest.fixture def mock_client_{i}(): return MockClient(timeout={i})"
            for i in range(10)
        ]

        texts = http_chunks + db_chunks + test_chunks
        doc_ids = [f"chunk-{i}" for i in range(len(texts))]
        embeddings = np.array(ef(texts), dtype=np.float32)

        count = db.taxonomy.discover_topics(
            "code__test", doc_ids, embeddings, texts, chroma,
        )

        # Should find at least 2 distinct clusters (ideally 3)
        assert count >= 2, f"Expected >=2 topics from 3 code domains, got {count}"

        topics = db.taxonomy.get_topics()
        # Each topic label should be non-empty (c-TF-IDF produced terms)
        for t in topics:
            assert t["label"].strip(), f"Topic {t['id']} has empty label"
            assert t["doc_count"] > 0

    def test_nearest_centroid_agreement(
        self, db: T2Database, ef, chroma,
    ) -> None:
        """Hold-out agreement: nearest-centroid assigns consistently with batch.

        Hold out 10% of embeddings, run discover on 90%, then check that
        assign_single maps held-out docs to the same cluster HDBSCAN would
        have assigned them to. Reports agreement percentage.
        """
        from sklearn.cluster import HDBSCAN as SklearnHDBSCAN

        # Same code domains as above for reproducibility
        http_chunks = [f"def handle_request(r): return json_response({i})" for i in range(30)]
        db_chunks = [f"cursor.execute('SELECT * FROM table_{i}') rows = cursor.fetchall()" for i in range(30)]
        test_chunks = [f"def test_feature_{i}(db): result = db.get({i}) assert result" for i in range(30)]

        texts = http_chunks + db_chunks + test_chunks
        doc_ids = [f"chunk-{i}" for i in range(len(texts))]
        embeddings = np.array(ef(texts), dtype=np.float32)

        # Hold out last 10% from each domain (3 per domain = 9 total)
        holdout_indices = list(range(27, 30)) + list(range(57, 60)) + list(range(87, 90))
        train_mask = np.ones(len(texts), dtype=bool)
        train_mask[holdout_indices] = False

        train_ids = [doc_ids[i] for i in range(len(doc_ids)) if train_mask[i]]
        train_texts = [texts[i] for i in range(len(texts)) if train_mask[i]]
        train_embs = embeddings[train_mask]

        # Discover on training set
        count = db.taxonomy.discover_topics(
            "code__agreement", train_ids, train_embs, train_texts, chroma,
        )
        assert count >= 2

        # Also run batch HDBSCAN on FULL set to get "ground truth" labels
        min_cs = max(5, len(embeddings) // 15)
        full_labels = SklearnHDBSCAN(
            min_cluster_size=min_cs, store_centers="centroid", copy=True,
        ).fit_predict(embeddings)

        # For each held-out doc, check that assign_single assigns it to a
        # topic whose members come from the SAME domain. The three domains
        # are http (indices 0-29), db (30-59), test (60-89). We check that
        # the held-out doc and the majority of the topic's training docs
        # share the same domain.
        def _domain(i: int) -> str:
            if i < 30:
                return "http"
            if i < 60:
                return "db"
            return "test"

        agreements = 0
        total = 0
        for idx in holdout_indices:
            if full_labels[idx] < 0:
                continue  # skip noise in full batch
            topic_id = db.taxonomy.assign_single(
                "code__agreement", embeddings[idx], chroma,
            )
            if topic_id is None:
                continue
            total += 1

            # Check domain coherence: get docs assigned to this topic
            # and verify most share the same domain as the held-out doc
            topic_docs = db.taxonomy.get_all_topic_doc_ids(topic_id)
            doc_domain = _domain(idx)
            topic_domains = [
                _domain(int(did.split("-")[1]))
                for did in topic_docs
                if did.startswith("chunk-")
            ]
            if topic_domains:
                majority_domain = max(set(topic_domains), key=topic_domains.count)
                if majority_domain == doc_domain:
                    agreements += 1

        # Agreement threshold: domain coherence should hold for most
        assert total >= len(holdout_indices) // 2, (
            f"Too few held-out docs assigned: {total}/{len(holdout_indices)}"
        )
        if total > 0:
            agreement_rate = agreements / total
            assert agreement_rate >= 0.8, (
                f"Domain coherence too low: {agreements}/{total} = {agreement_rate:.0%}"
            )


# ── sklearn HDBSCAN smoke tests (RDR-070, nexus-86v) ────────────────────────
#
# scikit-learn>=1.3 is a core dep. sklearn.cluster.HDBSCAN replaces BERTopic
# for topic discovery — same HDBSCAN algorithm, c-TF-IDF labels via
# CountVectorizer+TfidfTransformer, incremental assignment via nearest centroid.
# No torch, no sentence-transformers, no optional extra needed.


class TestSklearnHdbscanSmoke:
    """Verify sklearn HDBSCAN + TF-IDF topic pipeline works on 384d embeddings."""

    def test_hdbscan_finds_clusters(self) -> None:
        """HDBSCAN discovers clusters from well-separated 384d embeddings."""
        from sklearn.cluster import HDBSCAN

        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0

        clusterer = HDBSCAN(min_cluster_size=5, store_centers="centroid", copy=True)
        labels = clusterer.fit_predict(embeddings)

        assert len(labels) == 60
        real_topics = {t for t in labels if t >= 0}
        assert len(real_topics) >= 2, f"Expected >=2 clusters, got {real_topics}"
        assert clusterer.centroids_.shape[1] == 384

    def test_tfidf_topic_labels(self) -> None:
        """c-TF-IDF produces meaningful per-cluster labels from doc text."""
        from sklearn.cluster import HDBSCAN
        from sklearn.feature_extraction.text import CountVectorizer, TfidfTransformer

        rng = np.random.default_rng(42)
        docs_a = [f"machine learning neural network gradient {i}" for i in range(30)]
        docs_b = [f"database query indexing sql schema {i}" for i in range(30)]
        docs = docs_a + docs_b

        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0

        labels = HDBSCAN(min_cluster_size=5, copy=True).fit_predict(embeddings)

        vectorizer = CountVectorizer(stop_words="english")
        tfidf = TfidfTransformer().fit_transform(vectorizer.fit_transform(docs))
        feature_names = vectorizer.get_feature_names_out()

        real_clusters = {t for t in labels if t >= 0}
        for cid in real_clusters:
            mask = labels == cid
            cluster_tfidf = tfidf[mask].mean(axis=0).A1
            top_idx = cluster_tfidf.argsort()[-3:][::-1]
            top_terms = [feature_names[i] for i in top_idx]
            assert len(top_terms) == 3
            # Each cluster's top terms should come from its own domain
            assert any(t in top_terms for t in
                       ["machine", "learning", "neural", "network", "gradient",
                        "database", "query", "indexing", "sql", "schema"])

    def test_incremental_nearest_centroid(self) -> None:
        """New docs assigned to nearest cluster centroid via cosine similarity."""
        from sklearn.cluster import HDBSCAN
        from sklearn.metrics.pairwise import cosine_similarity

        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0

        clusterer = HDBSCAN(min_cluster_size=5, store_centers="centroid", copy=True)
        labels = clusterer.fit_predict(embeddings)

        # New embedding near cluster A
        new_emb = rng.standard_normal((1, 384)).astype(np.float32) * 0.1
        new_emb[0, 0] += 3.0

        sims = cosine_similarity(new_emb, clusterer.centroids_)
        assigned = int(sims.argmax())

        # Should assign to same cluster as the first 30 docs
        cluster_a = labels[0]
        assert assigned == cluster_a
        assert sims[0, assigned] > 0.5


# ── Review infrastructure (RDR-070, nexus-lbu) ────────────────────────────────


class TestReviewSchema:
    """Schema migrations for review_status and terms columns."""

    def test_review_status_column_exists(self, db: T2Database) -> None:
        """review_status column added to topics table via migration."""
        cols = {
            row[1]
            for row in db.taxonomy.conn.execute("PRAGMA table_info(topics)").fetchall()
        }
        assert "review_status" in cols

    def test_terms_column_exists(self, db: T2Database) -> None:
        """terms column added to topics table via migration."""
        cols = {
            row[1]
            for row in db.taxonomy.conn.execute("PRAGMA table_info(topics)").fetchall()
        }
        assert "terms" in cols

    def test_review_status_default_pending(self, db: T2Database) -> None:
        """New topics default to review_status='pending'."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("test", "proj", 1, "2026-01-01T00:00:00Z"),
        )
        db.taxonomy.conn.commit()
        row = db.taxonomy.conn.execute(
            "SELECT review_status FROM topics LIMIT 1"
        ).fetchone()
        assert row[0] == "pending"


class TestReviewMethods:
    """CatalogTaxonomy methods for review workflow."""

    def test_get_unreviewed_topics(self, db: T2Database) -> None:
        """get_unreviewed_topics returns only pending topics."""
        db.taxonomy.conn.executemany(
            "INSERT INTO topics (label, collection, doc_count, created_at, review_status) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("pending-topic", "proj", 5, "2026-01-01T00:00:00Z", "pending"),
                ("accepted-topic", "proj", 3, "2026-01-01T00:00:00Z", "accepted"),
                ("deleted-topic", "proj", 1, "2026-01-01T00:00:00Z", "deleted"),
            ],
        )
        db.taxonomy.conn.commit()

        unreviewed = db.taxonomy.get_unreviewed_topics(collection="proj")
        assert len(unreviewed) == 1
        assert unreviewed[0]["label"] == "pending-topic"

    def test_get_unreviewed_topics_limit(self, db: T2Database) -> None:
        """get_unreviewed_topics respects limit."""
        for i in range(10):
            db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at) "
                "VALUES (?, ?, ?, ?)",
                (f"topic-{i}", "proj", i + 1, "2026-01-01T00:00:00Z"),
            )
        db.taxonomy.conn.commit()

        result = db.taxonomy.get_unreviewed_topics(collection="proj", limit=3)
        assert len(result) == 3

    def test_get_unreviewed_topics_all_collections(self, db: T2Database) -> None:
        """get_unreviewed_topics with empty collection returns all."""
        db.taxonomy.conn.executemany(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            [
                ("topic-a", "coll-a", 5, "2026-01-01T00:00:00Z"),
                ("topic-b", "coll-b", 3, "2026-01-01T00:00:00Z"),
            ],
        )
        db.taxonomy.conn.commit()

        result = db.taxonomy.get_unreviewed_topics()
        assert len(result) == 2

    def test_mark_topic_reviewed(self, db: T2Database) -> None:
        """mark_topic_reviewed updates review_status."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("test", "proj", 5, "2026-01-01T00:00:00Z"),
        )
        topic_id = db.taxonomy.conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        db.taxonomy.conn.commit()

        db.taxonomy.mark_topic_reviewed(topic_id, "accepted")

        row = db.taxonomy.conn.execute(
            "SELECT review_status FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()
        assert row[0] == "accepted"

    def test_rename_topic(self, db: T2Database) -> None:
        """rename_topic updates label and sets review_status='accepted'."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("old-label", "proj", 5, "2026-01-01T00:00:00Z"),
        )
        topic_id = db.taxonomy.conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        db.taxonomy.conn.commit()

        db.taxonomy.rename_topic(topic_id, "new-label")

        row = db.taxonomy.conn.execute(
            "SELECT label, review_status FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()
        assert row[0] == "new-label"
        assert row[1] == "accepted"

    def test_delete_topic(self, db: T2Database) -> None:
        """delete_topic removes topic and its assignments."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("doomed", "proj", 1, "2026-01-01T00:00:00Z"),
        )
        topic_id = db.taxonomy.conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        db.taxonomy.conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
            ("doc-1", topic_id),
        )
        db.taxonomy.conn.commit()

        db.taxonomy.delete_topic(topic_id)

        assert (
            db.taxonomy.conn.execute(
                "SELECT COUNT(*) FROM topics WHERE id = ?", (topic_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.taxonomy.conn.execute(
                "SELECT COUNT(*) FROM topic_assignments WHERE topic_id = ?",
                (topic_id,),
            ).fetchone()[0]
            == 0
        )

    def test_merge_topics(self, db: T2Database) -> None:
        """merge_topics moves assignments from source to target, deletes source."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("source", "proj", 2, "2026-01-01T00:00:00Z"),
        )
        source_id = db.taxonomy.conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("target", "proj", 3, "2026-01-01T00:00:00Z"),
        )
        target_id = db.taxonomy.conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

        db.taxonomy.conn.executemany(
            "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
            [("doc-a", source_id), ("doc-b", source_id), ("doc-c", target_id)],
        )
        db.taxonomy.conn.commit()

        db.taxonomy.merge_topics(source_id, target_id)

        # Source topic deleted
        assert (
            db.taxonomy.conn.execute(
                "SELECT COUNT(*) FROM topics WHERE id = ?", (source_id,)
            ).fetchone()[0]
            == 0
        )
        # Target doc_count = actual assignment count (3 distinct docs)
        target_row = db.taxonomy.conn.execute(
            "SELECT doc_count FROM topics WHERE id = ?", (target_id,)
        ).fetchone()
        assert target_row[0] == 3
        # All assignments on target
        docs = db.taxonomy.conn.execute(
            "SELECT doc_id FROM topic_assignments WHERE topic_id = ? ORDER BY doc_id",
            (target_id,),
        ).fetchall()
        assert [r[0] for r in docs] == ["doc-a", "doc-b", "doc-c"]

    def test_merge_topics_dedup(self, db: T2Database) -> None:
        """merge_topics handles docs assigned to both source and target."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("source", "proj", 1, "2026-01-01T00:00:00Z"),
        )
        source_id = db.taxonomy.conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("target", "proj", 1, "2026-01-01T00:00:00Z"),
        )
        target_id = db.taxonomy.conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

        # Same doc assigned to both topics
        db.taxonomy.conn.executemany(
            "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
            [("shared-doc", source_id), ("shared-doc", target_id)],
        )
        db.taxonomy.conn.commit()

        db.taxonomy.merge_topics(source_id, target_id)

        # Only one assignment for the shared doc on target
        count = db.taxonomy.conn.execute(
            "SELECT COUNT(*) FROM topic_assignments WHERE doc_id = 'shared-doc' AND topic_id = ?",
            (target_id,),
        ).fetchone()[0]
        assert count == 1

    def test_get_topic_by_id(self, db: T2Database) -> None:
        """get_topic_by_id returns a single topic dict or None."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("my-topic", "proj", 7, "2026-01-01T00:00:00Z"),
        )
        topic_id = db.taxonomy.conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        db.taxonomy.conn.commit()

        result = db.taxonomy.get_topic_by_id(topic_id)
        assert result is not None
        assert result["label"] == "my-topic"
        assert result["doc_count"] == 7

        assert db.taxonomy.get_topic_by_id(99999) is None

    def test_get_topic_doc_ids(self, db: T2Database) -> None:
        """get_topic_doc_ids returns limited doc_ids for a topic."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("test", "proj", 5, "2026-01-01T00:00:00Z"),
        )
        topic_id = db.taxonomy.conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        for i in range(5):
            db.taxonomy.conn.execute(
                "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                (f"doc-{i}", topic_id),
            )
        db.taxonomy.conn.commit()

        result = db.taxonomy.get_topic_doc_ids(topic_id, limit=3)
        assert len(result) == 3
        assert all(isinstance(d, str) for d in result)


class TestDiscoverStoresTerms:
    """discover_topics stores c-TF-IDF terms in the terms column."""

    @pytest.fixture()
    def chroma_client(self) -> chromadb.ClientAPI:
        return chromadb.EphemeralClient()

    def test_terms_stored_as_json(
        self, db: T2Database, chroma_client: chromadb.ClientAPI,
    ) -> None:
        """discover_topics persists top c-TF-IDF terms as JSON."""
        import json

        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0
        doc_ids = [f"doc-{i}" for i in range(60)]
        texts = (
            [f"machine learning neural network gradient {i}" for i in range(30)]
            + [f"database query indexing sql schema {i}" for i in range(30)]
        )

        db.taxonomy.discover_topics(
            "test__coll", doc_ids, embeddings, texts, chroma_client,
        )

        rows = db.taxonomy.conn.execute(
            "SELECT terms FROM topics WHERE terms IS NOT NULL"
        ).fetchall()
        assert len(rows) >= 2
        for row in rows:
            terms = json.loads(row[0])
            assert isinstance(terms, list)
            assert len(terms) >= 3


class TestReviewCLI:
    """CLI tests for nx taxonomy review."""

    def _seed_topics(self, db_path: Path) -> int:
        """Insert test topics and return the first topic_id."""
        import json

        with T2Database(db_path) as db:
            db.taxonomy.conn.execute(
                "INSERT INTO topics "
                "(label, collection, doc_count, created_at, review_status, terms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "machine learning",
                    "proj",
                    5,
                    "2026-01-01T00:00:00Z",
                    "pending",
                    json.dumps(["neural", "network", "gradient", "loss", "model"]),
                ),
            )
            topic_id = db.taxonomy.conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            for i in range(3):
                db.taxonomy.conn.execute(
                    "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                    (f"src/model_{i}.py", topic_id),
                )
            db.taxonomy.conn.commit()
        return topic_id

    def test_review_accept(self, tmp_path: Path) -> None:
        """Accept action marks topic as accepted."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        self._seed_topics(db_path)

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                taxonomy, ["review", "--collection", "proj"], input="a\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            status = db.taxonomy.conn.execute(
                "SELECT review_status FROM topics LIMIT 1"
            ).fetchone()[0]
        assert status == "accepted"

    def test_review_skip(self, tmp_path: Path) -> None:
        """Skip action leaves topic as pending."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        self._seed_topics(db_path)

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                taxonomy, ["review", "--collection", "proj"], input="S\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            status = db.taxonomy.conn.execute(
                "SELECT review_status FROM topics LIMIT 1"
            ).fetchone()[0]
        assert status == "pending"

    def test_review_rename(self, tmp_path: Path) -> None:
        """Rename action updates the topic label."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        self._seed_topics(db_path)

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                taxonomy,
                ["review", "--collection", "proj"],
                input="r\ndeep learning\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            row = db.taxonomy.conn.execute(
                "SELECT label, review_status FROM topics LIMIT 1"
            ).fetchone()
        assert row[0] == "deep learning"
        assert row[1] == "accepted"

    def test_review_delete(self, tmp_path: Path) -> None:
        """Delete action removes topic and assignments."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        self._seed_topics(db_path)

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                taxonomy, ["review", "--collection", "proj"], input="d\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            count = db.taxonomy.conn.execute(
                "SELECT COUNT(*) FROM topics"
            ).fetchone()[0]
        assert count == 0

    def test_review_no_unreviewed(self, tmp_path: Path) -> None:
        """Shows 'all done' message when no topics need review."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        # Create DB but don't add any topics
        with T2Database(db_path):
            pass

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                taxonomy, ["review", "--collection", "proj"],
            )

        assert result.exit_code == 0
        assert "no unreviewed topics" in result.output.lower() or "all done" in result.output.lower()

    def test_review_merge(self, tmp_path: Path) -> None:
        """Merge action moves docs to target topic."""
        import json
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            # Source topic (pending)
            db.taxonomy.conn.execute(
                "INSERT INTO topics "
                "(label, collection, doc_count, created_at, review_status, terms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "source topic",
                    "proj",
                    2,
                    "2026-01-01T00:00:00Z",
                    "pending",
                    json.dumps(["a", "b", "c"]),
                ),
            )
            source_id = db.taxonomy.conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            # Target topic (already accepted)
            db.taxonomy.conn.execute(
                "INSERT INTO topics "
                "(label, collection, doc_count, created_at, review_status, terms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "target topic",
                    "proj",
                    3,
                    "2026-01-01T00:00:00Z",
                    "accepted",
                    json.dumps(["d", "e", "f"]),
                ),
            )
            target_id = db.taxonomy.conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            db.taxonomy.conn.executemany(
                "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                [("doc-a", source_id), ("doc-b", source_id), ("doc-c", target_id)],
            )
            db.taxonomy.conn.commit()

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            # m = merge, then enter target topic ID
            result = runner.invoke(
                taxonomy,
                ["review", "--collection", "proj"],
                input=f"m\n{target_id}\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            # Source deleted
            assert (
                db.taxonomy.conn.execute(
                    "SELECT COUNT(*) FROM topics WHERE id = ?", (source_id,)
                ).fetchone()[0]
                == 0
            )
            # All docs on target
            docs = db.taxonomy.conn.execute(
                "SELECT doc_id FROM topic_assignments WHERE topic_id = ? ORDER BY doc_id",
                (target_id,),
            ).fetchall()
            assert {r[0] for r in docs} == {"doc-a", "doc-b", "doc-c"}


# ── Manual taxonomy operations CLI (RDR-070, nexus-c3w) ───────────────────


class TestResolveLabel:
    """resolve_label looks up topic_id by label."""

    def test_resolve_existing(self, db: T2Database) -> None:
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("my-topic", "proj", 5, "2026-01-01T00:00:00Z"),
        )
        db.taxonomy.conn.commit()
        result = db.taxonomy.resolve_label("my-topic", collection="proj")
        assert result is not None
        assert isinstance(result, int)

    def test_resolve_missing(self, db: T2Database) -> None:
        assert db.taxonomy.resolve_label("nonexistent") is None

    def test_resolve_scoped_by_collection(self, db: T2Database) -> None:
        db.taxonomy.conn.executemany(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            [
                ("shared-label", "coll-a", 3, "2026-01-01T00:00:00Z"),
                ("shared-label", "coll-b", 2, "2026-01-01T00:00:00Z"),
            ],
        )
        db.taxonomy.conn.commit()
        result = db.taxonomy.resolve_label("shared-label", collection="coll-b")
        assert result is not None
        topic = db.taxonomy.get_topic_by_id(result)
        assert topic["collection"] == "coll-b"


class TestSplitTopic:
    """split_topic creates child topics via KMeans sub-clustering."""

    @pytest.fixture()
    def chroma(self) -> chromadb.ClientAPI:
        return chromadb.EphemeralClient()

    def test_split_creates_children(
        self, db: T2Database, chroma: chromadb.ClientAPI,
    ) -> None:
        """Split a parent topic into k children via KMeans."""
        from nexus.db.local_ef import LocalEmbeddingFunction

        # Create parent topic with mixed docs
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("mixed-topic", "test__split", 30, "2026-01-01T00:00:00Z"),
        )
        parent_id = db.taxonomy.conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

        # Two domains — split should separate them
        texts_a = [f"machine learning gradient descent {i}" for i in range(15)]
        texts_b = [f"database query sql index {i}" for i in range(15)]
        texts = texts_a + texts_b
        doc_ids = [f"doc-{i}" for i in range(30)]

        for did in doc_ids:
            db.taxonomy.conn.execute(
                "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                (did, parent_id),
            )
        db.taxonomy.conn.commit()

        # Seed the T3 collection with docs
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        coll = chroma.get_or_create_collection(
            "test__split", embedding_function=None,
        )
        emb_list = ef(texts)
        coll.add(ids=doc_ids, documents=texts, embeddings=emb_list)

        # Seed a parent centroid in taxonomy__centroids
        centroid_coll = chroma.get_or_create_collection(
            "taxonomy__centroids",
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )
        import numpy as _np
        parent_centroid = _np.array(emb_list).mean(axis=0).tolist()
        centroid_coll.add(
            ids=[f"test__split:{parent_id}"],
            embeddings=[parent_centroid],
            metadatas=[{
                "topic_id": parent_id,
                "label": "mixed-topic",
                "collection": "test__split",
                "doc_count": 30,
            }],
        )

        child_count = db.taxonomy.split_topic(
            parent_id, k=2, chroma_client=chroma,
        )
        assert child_count == 2

        # Children exist with parent_id set
        children = db.taxonomy.get_topics(parent_id=parent_id)
        assert len(children) == 2
        assert all(c["doc_count"] > 0 for c in children)

        # Parent has no direct assignments (all moved to children)
        parent_docs = db.taxonomy.get_topic_doc_ids(parent_id)
        assert len(parent_docs) == 0

        # Centroids: parent removed, children added
        centroid_coll = chroma.get_collection(
            "taxonomy__centroids", embedding_function=None,
        )
        centroid_data = centroid_coll.get(
            where={"collection": "test__split"},
            include=["metadatas"],
        )
        centroid_topic_ids = {m["topic_id"] for m in centroid_data["metadatas"]}
        child_ids = {c["id"] for c in children}
        # Parent centroid should be gone, child centroids should exist
        assert parent_id not in centroid_topic_ids
        assert child_ids == centroid_topic_ids

    def test_split_too_few_docs(self, db: T2Database) -> None:
        """Split with fewer docs than k returns 0."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("tiny", "proj", 2, "2026-01-01T00:00:00Z"),
        )
        parent_id = db.taxonomy.conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        db.taxonomy.conn.executemany(
            "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
            [("doc-0", parent_id), ("doc-1", parent_id)],
        )
        db.taxonomy.conn.commit()

        result = db.taxonomy.split_topic(
            parent_id, k=3, chroma_client=chromadb.EphemeralClient(),
        )
        assert result == 0


class TestManualOpsCLI:
    """CLI tests for nx taxonomy assign/merge/split/rename commands."""

    def test_assign_cli(self, tmp_path: Path) -> None:
        """nx taxonomy assign sets assigned_by='manual'."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("target-topic", "proj", 5, "2026-01-01T00:00:00Z"),
            )
            db.taxonomy.conn.commit()

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                taxonomy,
                ["assign", "my-doc-id", "target-topic", "--collection", "proj"],
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            row = db.taxonomy.conn.execute(
                "SELECT assigned_by FROM topic_assignments WHERE doc_id = 'my-doc-id'"
            ).fetchone()
        assert row is not None
        assert row[0] == "manual"

    def test_assign_cli_unknown_label(self, tmp_path: Path) -> None:
        """nx taxonomy assign with unknown label prints error."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path):
            pass

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                taxonomy, ["assign", "doc-x", "nonexistent"],
            )

        assert result.exit_code == 0
        assert "not found" in result.output.lower()

    def test_rename_cli(self, tmp_path: Path) -> None:
        """nx taxonomy rename updates the label."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("old-name", "proj", 5, "2026-01-01T00:00:00Z"),
            )
            db.taxonomy.conn.commit()

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                taxonomy,
                ["rename", "old-name", "new-name", "--collection", "proj"],
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            row = db.taxonomy.conn.execute(
                "SELECT label FROM topics LIMIT 1"
            ).fetchone()
        assert row[0] == "new-name"

    def test_merge_cli(self, tmp_path: Path) -> None:
        """nx taxonomy merge moves docs and deletes source."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("source", "proj", 2, "2026-01-01T00:00:00Z"),
            )
            source_id = db.taxonomy.conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("target", "proj", 1, "2026-01-01T00:00:00Z"),
            )
            db.taxonomy.conn.executemany(
                "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                [("doc-a", source_id), ("doc-b", source_id)],
            )
            db.taxonomy.conn.commit()

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                taxonomy,
                ["merge", "source", "target", "--collection", "proj"],
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            # Source deleted
            assert (
                db.taxonomy.conn.execute(
                    "SELECT COUNT(*) FROM topics WHERE label = 'source'"
                ).fetchone()[0]
                == 0
            )
            # Target has the docs
            target_id = db.taxonomy.conn.execute(
                "SELECT id FROM topics WHERE label = 'target'"
            ).fetchone()[0]
            docs = db.taxonomy.conn.execute(
                "SELECT doc_id FROM topic_assignments WHERE topic_id = ?",
                (target_id,),
            ).fetchall()
            assert {r[0] for r in docs} == {"doc-a", "doc-b"}

    def test_split_cli(self, tmp_path: Path) -> None:
        """nx taxonomy split invokes split_topic with correct args."""
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("big-topic", "test__split_cli", 30, "2026-01-01T00:00:00Z"),
            )
            parent_id = db.taxonomy.conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            for i in range(30):
                db.taxonomy.conn.execute(
                    "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                    (f"doc-{i}", parent_id),
                )
            db.taxonomy.conn.commit()

        mock_t3 = MagicMock()
        mock_split = MagicMock(return_value=2)
        runner = CliRunner()
        with (
            patch(
                "nexus.commands.taxonomy_cmd._default_db_path",
                return_value=db_path,
            ),
            patch(
                "nexus.db.make_t3",
                return_value=mock_t3,
            ),
            patch(
                "nexus.db.t2.catalog_taxonomy.CatalogTaxonomy.split_topic",
                mock_split,
            ),
        ):
            result = runner.invoke(
                taxonomy,
                ["split", "big-topic", "--k", "2", "--collection", "test__split_cli"],
            )

        assert result.exit_code == 0, result.output
        assert "2 sub-topics" in result.output
        # Verify split_topic was called with correct topic_id and k
        mock_split.assert_called_once()
        call_args = mock_split.call_args
        assert call_args[1]["k"] == 2 or call_args[0][1] == 2


# ── Rebalance trigger + merge strategy (RDR-070, nexus-1im) ───────────────


class TestRebalanceTrigger:
    """needs_rebalance detects 2x corpus growth."""

    def test_no_prior_discover_needs_rebalance(self, db: T2Database) -> None:
        """First discover always proceeds (no prior count)."""
        assert db.taxonomy.needs_rebalance("test__coll", current_count=100) is True

    def test_below_threshold_no_rebalance(self, db: T2Database) -> None:
        """Under 2x growth does not trigger rebalance."""
        db.taxonomy.record_discover_count("test__coll", 100)
        assert db.taxonomy.needs_rebalance("test__coll", current_count=150) is False

    def test_at_2x_triggers_rebalance(self, db: T2Database) -> None:
        """At 2x growth triggers rebalance."""
        db.taxonomy.record_discover_count("test__coll", 100)
        assert db.taxonomy.needs_rebalance("test__coll", current_count=200) is True

    def test_above_2x_triggers_rebalance(self, db: T2Database) -> None:
        """Above 2x growth triggers rebalance."""
        db.taxonomy.record_discover_count("test__coll", 100)
        assert db.taxonomy.needs_rebalance("test__coll", current_count=300) is True

    def test_record_updates_count(self, db: T2Database) -> None:
        """record_discover_count updates the stored count."""
        db.taxonomy.record_discover_count("test__coll", 50)
        assert db.taxonomy.needs_rebalance("test__coll", current_count=80) is False
        db.taxonomy.record_discover_count("test__coll", 80)
        assert db.taxonomy.needs_rebalance("test__coll", current_count=160) is True


class TestMergeStrategy:
    """_merge_labels transfers operator labels by centroid similarity."""

    def test_high_similarity_transfers_label(self, db: T2Database) -> None:
        """Old label transferred when cosine similarity > 0.8."""
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

        # Near-identical centroids (high cosine similarity)
        old_centroids = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        old_labels = ["operator-approved-label"]
        old_review_statuses = ["accepted"]
        new_centroids = np.array([[0.99, 0.1, 0.0]], dtype=np.float32)

        merged = CatalogTaxonomy._merge_labels(
            old_centroids, old_labels, old_review_statuses, new_centroids,
        )
        assert len(merged) == 1
        assert merged[0]["label"] == "operator-approved-label"
        assert merged[0]["review_status"] == "accepted"

    def test_low_similarity_uses_new_label(self, db: T2Database) -> None:
        """New c-TF-IDF label used when cosine similarity <= 0.8."""
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

        old_centroids = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        old_labels = ["old-label"]
        old_review_statuses = ["accepted"]
        # Orthogonal -> cosine similarity ~0
        new_centroids = np.array([[0.0, 1.0, 0.0]], dtype=np.float32)

        merged = CatalogTaxonomy._merge_labels(
            old_centroids, old_labels, old_review_statuses, new_centroids,
        )
        assert len(merged) == 1
        assert merged[0]["label"] is None  # caller uses c-TF-IDF
        assert merged[0]["review_status"] == "pending"

    def test_n1_dedup_highest_wins(self, db: T2Database) -> None:
        """When two new centroids match the same old centroid, highest similarity wins."""
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

        old_centroids = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        old_labels = ["shared-label"]
        old_review_statuses = ["accepted"]
        # Two new centroids, both close to old but one closer
        new_centroids = np.array([
            [0.95, 0.3, 0.0],  # similarity ~0.95
            [0.85, 0.5, 0.0],  # similarity ~0.86
        ], dtype=np.float32)

        merged = CatalogTaxonomy._merge_labels(
            old_centroids, old_labels, old_review_statuses, new_centroids,
        )
        assert len(merged) == 2
        # Only the higher-similarity centroid gets the label
        labels = [m["label"] for m in merged]
        assert labels.count("shared-label") == 1
        assert labels.count(None) == 1

    def test_no_old_centroids(self, db: T2Database) -> None:
        """With no old centroids, all new centroids get None labels."""
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

        new_centroids = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        merged = CatalogTaxonomy._merge_labels(
            np.empty((0, 2), dtype=np.float32), [], [], new_centroids,
        )
        assert len(merged) == 2
        assert all(m["label"] is None for m in merged)


class TestManualPreservation:
    """Manual assignments preserved across re-discovery."""

    @pytest.fixture()
    def chroma(self) -> chromadb.ClientAPI:
        return chromadb.EphemeralClient()

    def test_manual_assignments_survive_rebuild(
        self, db: T2Database, chroma: chromadb.ClientAPI,
    ) -> None:
        """Rebuild with merge strategy preserves manual assignments."""
        from nexus.db.local_ef import LocalEmbeddingFunction

        # Initial discovery
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        texts_a = [f"machine learning gradient descent {i}" for i in range(30)]
        texts_b = [f"database query sql index {i}" for i in range(30)]
        texts = texts_a + texts_b
        doc_ids = [f"doc-{i}" for i in range(60)]
        embeddings = np.array(ef(texts), dtype=np.float32)

        count = db.taxonomy.discover_topics(
            "test__preserve", doc_ids, embeddings, texts, chroma,
        )
        assert count >= 2

        # Manually assign a doc and rename a topic
        topics = db.taxonomy.get_topics()
        topic = topics[0]
        db.taxonomy.assign_topic("manual-doc", topic["id"], assigned_by="manual")
        db.taxonomy.rename_topic(topic["id"], "operator-approved")

        # Rebuild (with merge strategy)
        new_count = db.taxonomy.rebuild_taxonomy(
            "test__preserve", doc_ids, embeddings, texts, chroma,
        )
        assert new_count >= 2

        # Check that operator label survived via merge strategy
        new_topics = db.taxonomy.get_topics()
        new_labels = [t["label"] for t in new_topics]
        # The operator-approved label should be transferred to the
        # nearest matching centroid (same data -> high similarity)
        assert "operator-approved" in new_labels

        # Manual assignment should be preserved
        rows = db.taxonomy.conn.execute(
            "SELECT assigned_by FROM topic_assignments WHERE doc_id = 'manual-doc'"
        ).fetchall()
        assert len(rows) >= 1
        assert any(r[0] == "manual" for r in rows)


class TestRediscoveryCentroidLifecycle:
    """Centroid lifecycle: clear before re-upsert on --force."""

    @pytest.fixture()
    def chroma(self) -> chromadb.ClientAPI:
        return chromadb.EphemeralClient()

    def test_force_clears_old_centroids(
        self, db: T2Database, chroma: chromadb.ClientAPI,
    ) -> None:
        """rebuild_taxonomy clears old centroids before upserting new."""
        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0
        doc_ids = [f"doc-{i}" for i in range(60)]
        texts = (
            [f"machine learning neural {i}" for i in range(30)]
            + [f"database query sql {i}" for i in range(30)]
        )

        # First discovery
        db.taxonomy.discover_topics(
            "test__lifecycle", doc_ids, embeddings, texts, chroma,
        )
        centroid_coll = chroma.get_collection(
            "taxonomy__centroids", embedding_function=None,
        )
        first_ids = set(centroid_coll.get()["ids"])
        assert len(first_ids) >= 2

        # Rebuild (clears old, creates new)
        db.taxonomy.rebuild_taxonomy(
            "test__lifecycle", doc_ids, embeddings, texts, chroma,
        )
        second_ids = set(centroid_coll.get()["ids"])
        assert len(second_ids) >= 2

        # Old centroid IDs should be replaced (not accumulated)
        # The IDs use format "collection:topic_id" — topic_ids change on rebuild
        # so old IDs should not persist
        all_ids = centroid_coll.get(
            where={"collection": "test__lifecycle"},
        )["ids"]
        # Count should match number of current topics, not 2x
        current_topics = [
            t for t in db.taxonomy.get_topics()
            if t.get("collection") == "test__lifecycle"
        ]
        assert len(all_ids) == len(current_topics)


# ── Topic-aware links (RDR-070, nexus-40f) ────────────────────────────────


class TestComputeTopicLinks:
    """compute_topic_links derives inter-topic relationships from catalog links."""

    def test_basic_topic_link(self, db: T2Database) -> None:
        """Two docs in different topics, linked in catalog, produce a topic link."""
        from unittest.mock import MagicMock

        from nexus.commands.taxonomy_cmd import compute_topic_links

        # Set up two topics with docs
        db.taxonomy.conn.executemany(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            [
                ("networking", "code__proj", 2, "2026-01-01T00:00:00Z"),
                ("database", "code__proj", 2, "2026-01-01T00:00:00Z"),
            ],
        )
        t1_id = db.taxonomy.conn.execute(
            "SELECT id FROM topics WHERE label = 'networking'"
        ).fetchone()[0]
        t2_id = db.taxonomy.conn.execute(
            "SELECT id FROM topics WHERE label = 'database'"
        ).fetchone()[0]
        db.taxonomy.conn.executemany(
            "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
            [
                ("src/net/server.py", t1_id),
                ("src/net/client.py", t1_id),
                ("src/db/store.py", t2_id),
                ("src/db/query.py", t2_id),
            ],
        )
        db.taxonomy.conn.commit()

        # Mock catalog with one link between docs in different topics
        mock_catalog = MagicMock()
        mock_entry_a = MagicMock(
            file_path="src/net/server.py",
            physical_collection="code__proj",
        )
        mock_entry_a.tumbler = MagicMock()
        mock_entry_a.tumbler.__str__ = lambda s: "1.1"
        mock_entry_b = MagicMock(
            file_path="src/db/store.py",
            physical_collection="code__proj",
        )
        mock_entry_b.tumbler = MagicMock()
        mock_entry_b.tumbler.__str__ = lambda s: "1.2"

        mock_link = MagicMock(
            link_type="cites",
        )
        mock_link.from_tumbler = mock_entry_a.tumbler
        mock_link.to_tumbler = mock_entry_b.tumbler

        mock_catalog.link_query.return_value = [mock_link]
        mock_catalog.resolve.side_effect = lambda t: (
            mock_entry_a if str(t) == "1.1" else mock_entry_b
        )

        result = compute_topic_links(db.taxonomy, mock_catalog)
        assert len(result) >= 1
        pair = result[0]
        assert {"networking", "database"} == {pair["from_topic"], pair["to_topic"]}
        assert pair["link_count"] == 1
        assert "cites" in pair["link_types"]

    def test_no_links_returns_empty(self, db: T2Database) -> None:
        """No catalog links → empty result."""
        from unittest.mock import MagicMock

        from nexus.commands.taxonomy_cmd import compute_topic_links

        mock_catalog = MagicMock()
        mock_catalog.link_query.return_value = []

        assert compute_topic_links(db.taxonomy, mock_catalog) == []

    def test_same_topic_link_excluded(self, db: T2Database) -> None:
        """Links between docs in the same topic are excluded."""
        from unittest.mock import MagicMock

        from nexus.commands.taxonomy_cmd import compute_topic_links

        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("single-topic", "code__proj", 2, "2026-01-01T00:00:00Z"),
        )
        tid = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.taxonomy.conn.executemany(
            "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
            [("src/a.py", tid), ("src/b.py", tid)],
        )
        db.taxonomy.conn.commit()

        mock_catalog = MagicMock()
        entry_a = MagicMock(file_path="src/a.py", physical_collection="code__proj")
        entry_a.tumbler.__str__ = lambda s: "1.1"
        entry_b = MagicMock(file_path="src/b.py", physical_collection="code__proj")
        entry_b.tumbler.__str__ = lambda s: "1.2"
        link = MagicMock(link_type="relates")
        link.from_tumbler = entry_a.tumbler
        link.to_tumbler = entry_b.tumbler
        mock_catalog.link_query.return_value = [link]
        mock_catalog.resolve.side_effect = lambda t: (
            entry_a if str(t) == "1.1" else entry_b
        )

        result = compute_topic_links(db.taxonomy, mock_catalog)
        assert result == []

    def test_multiple_link_types_aggregated(self, db: T2Database) -> None:
        """Multiple links between same topic pair aggregate counts and types."""
        from unittest.mock import MagicMock

        from nexus.commands.taxonomy_cmd import compute_topic_links

        db.taxonomy.conn.executemany(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            [
                ("api", "code__proj", 1, "2026-01-01T00:00:00Z"),
                ("model", "code__proj", 1, "2026-01-01T00:00:00Z"),
            ],
        )
        t1 = db.taxonomy.conn.execute(
            "SELECT id FROM topics WHERE label = 'api'"
        ).fetchone()[0]
        t2 = db.taxonomy.conn.execute(
            "SELECT id FROM topics WHERE label = 'model'"
        ).fetchone()[0]
        db.taxonomy.conn.executemany(
            "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
            [("src/api.py", t1), ("src/model.py", t2)],
        )
        db.taxonomy.conn.commit()

        mock_catalog = MagicMock()
        ea = MagicMock(file_path="src/api.py", physical_collection="code__proj")
        ea.tumbler.__str__ = lambda s: "1.1"
        eb = MagicMock(file_path="src/model.py", physical_collection="code__proj")
        eb.tumbler.__str__ = lambda s: "1.2"

        link1 = MagicMock(link_type="cites")
        link1.from_tumbler = ea.tumbler
        link1.to_tumbler = eb.tumbler
        link2 = MagicMock(link_type="implements")
        link2.from_tumbler = ea.tumbler
        link2.to_tumbler = eb.tumbler

        mock_catalog.link_query.return_value = [link1, link2]
        mock_catalog.resolve.side_effect = lambda t: (
            ea if str(t) == "1.1" else eb
        )

        result = compute_topic_links(db.taxonomy, mock_catalog)
        assert len(result) == 1
        assert result[0]["link_count"] == 2
        assert set(result[0]["link_types"]) == {"cites", "implements"}


class TestTopicLinksCLI:
    """CLI tests for nx taxonomy links."""

    def test_links_no_catalog(self, tmp_path: Path) -> None:
        """Links command gracefully handles missing catalog."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path):
            pass

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(taxonomy, ["links"])

        assert result.exit_code == 0
        assert "no topic links" in result.output.lower() or "catalog" in result.output.lower()

    def test_links_with_data(self, tmp_path: Path) -> None:
        """Links command shows topic relationships."""
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path):
            pass

        mock_result = [
            {
                "from_topic": "api",
                "to_topic": "database",
                "link_count": 5,
                "link_types": ["cites", "implements"],
            },
        ]

        runner = CliRunner()
        with (
            patch(
                "nexus.commands.taxonomy_cmd._default_db_path",
                return_value=db_path,
            ),
            patch(
                "nexus.commands.taxonomy_cmd.compute_topic_links",
                return_value=mock_result,
            ),
            patch(
                "nexus.commands.taxonomy_cmd._try_load_catalog",
                return_value=MagicMock(),
            ),
        ):
            result = runner.invoke(taxonomy, ["links"])

        assert result.exit_code == 0
        assert "api" in result.output
        assert "database" in result.output


# ── Coverage gap tests (deep review, nexus-kwx) ──────────────────────────────


class TestQueryMethodCoverage:
    """Direct unit tests for query methods on CatalogTaxonomy."""

    def test_get_doc_ids_for_topic(self, db: T2Database) -> None:
        """get_doc_ids_for_topic resolves label -> doc_ids via JOIN."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("search-methods", "proj", 3, "2026-01-01T00:00:00Z"),
        )
        tid = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.taxonomy.conn.executemany(
            "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
            [("doc-x", tid), ("doc-y", tid), ("doc-z", tid)],
        )
        db.taxonomy.conn.commit()

        result = db.taxonomy.get_doc_ids_for_topic("search-methods")
        assert set(result) == {"doc-x", "doc-y", "doc-z"}

    def test_get_doc_ids_for_topic_unknown_label(self, db: T2Database) -> None:
        """get_doc_ids_for_topic returns empty list for unknown label."""
        assert db.taxonomy.get_doc_ids_for_topic("nonexistent") == []

    def test_get_assignments_for_docs(self, db: T2Database) -> None:
        """get_assignments_for_docs returns {doc_id: topic_id} mapping."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("topic-a", "proj", 2, "2026-01-01T00:00:00Z"),
        )
        tid = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.taxonomy.conn.executemany(
            "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
            [("doc-1", tid), ("doc-2", tid)],
        )
        db.taxonomy.conn.commit()

        result = db.taxonomy.get_assignments_for_docs(["doc-1", "doc-2", "doc-3"])
        assert result == {"doc-1": tid, "doc-2": tid}

    def test_get_assignments_for_docs_empty(self, db: T2Database) -> None:
        """get_assignments_for_docs with empty list returns empty dict."""
        assert db.taxonomy.get_assignments_for_docs([]) == {}

    def test_get_labels_for_ids(self, db: T2Database) -> None:
        """get_labels_for_ids returns scoped {id: label} map."""
        db.taxonomy.conn.executemany(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            [
                ("alpha", "proj", 1, "2026-01-01T00:00:00Z"),
                ("beta", "proj", 1, "2026-01-01T00:00:00Z"),
                ("gamma", "proj", 1, "2026-01-01T00:00:00Z"),
            ],
        )
        db.taxonomy.conn.commit()
        all_ids = [
            r[0] for r in db.taxonomy.conn.execute("SELECT id FROM topics").fetchall()
        ]

        result = db.taxonomy.get_labels_for_ids(all_ids[:2])
        assert len(result) == 2
        assert set(result.values()) <= {"alpha", "beta", "gamma"}

    def test_get_labels_for_ids_empty(self, db: T2Database) -> None:
        """get_labels_for_ids with empty list returns empty dict."""
        assert db.taxonomy.get_labels_for_ids([]) == {}

    def test_get_all_topic_doc_ids(self, db: T2Database) -> None:
        """get_all_topic_doc_ids returns all assigned doc_ids without limit."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("big-topic", "proj", 10, "2026-01-01T00:00:00Z"),
        )
        tid = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for i in range(10):
            db.taxonomy.conn.execute(
                "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                (f"doc-{i}", tid),
            )
        db.taxonomy.conn.commit()

        result = db.taxonomy.get_all_topic_doc_ids(tid)
        assert len(result) == 10


class TestEdgeCases:
    """Edge cases identified in deep review."""

    def test_discover_topics_below_minimum(self, db: T2Database) -> None:
        """discover_topics with n < 5 returns 0 without crashing."""
        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((3, 384)).astype(np.float32)
        doc_ids = ["doc-0", "doc-1", "doc-2"]
        texts = ["hello world", "foo bar", "baz qux"]
        chroma = chromadb.EphemeralClient()

        result = db.taxonomy.discover_topics(
            "tiny__coll", doc_ids, embeddings, texts, chroma,
        )
        assert result == 0

    def test_rebuild_with_few_docs_clears_and_records(
        self, db: T2Database,
    ) -> None:
        """Rebuild with n < 5 clears old topics and records the count."""
        chroma = chromadb.EphemeralClient()

        # Seed existing topics for the collection
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("old-topic", "shrunk__coll", 50, "2026-01-01T00:00:00Z"),
        )
        db.taxonomy.conn.commit()

        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((3, 384)).astype(np.float32)

        result = db.taxonomy.rebuild_taxonomy(
            "shrunk__coll",
            ["doc-0", "doc-1", "doc-2"],
            embeddings,
            ["a", "b", "c"],
            chroma,
        )
        assert result == 0

        # Old topics should be cleared
        count = db.taxonomy.conn.execute(
            "SELECT COUNT(*) FROM topics WHERE collection = 'shrunk__coll'"
        ).fetchone()[0]
        assert count == 0

        # Doc count should be recorded
        assert db.taxonomy.needs_rebalance("shrunk__coll", current_count=2) is False

    def test_split_topic_collection_not_found(self, db: T2Database) -> None:
        """split_topic returns 0 when T3 collection doesn't exist."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("orphan", "nonexistent__coll", 5, "2026-01-01T00:00:00Z"),
        )
        tid = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for i in range(5):
            db.taxonomy.conn.execute(
                "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                (f"doc-{i}", tid),
            )
        db.taxonomy.conn.commit()

        chroma = chromadb.EphemeralClient()
        result = db.taxonomy.split_topic(tid, k=2, chroma_client=chroma)
        assert result == 0

    def test_discover_skip_existing_topics(self, db: T2Database) -> None:
        """discover_topics skips if topics already exist for collection."""
        chroma = chromadb.EphemeralClient()
        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0
        doc_ids = [f"doc-{i}" for i in range(60)]
        texts = (
            [f"machine learning {i}" for i in range(30)]
            + [f"database query {i}" for i in range(30)]
        )

        # First discover succeeds
        count1 = db.taxonomy.discover_topics(
            "dup__coll", doc_ids, embeddings, texts, chroma,
        )
        assert count1 >= 2

        # Second discover skips (returns 0, doesn't duplicate)
        count2 = db.taxonomy.discover_topics(
            "dup__coll", doc_ids, embeddings, texts, chroma,
        )
        assert count2 == 0

        # Still only the original topics
        topics = [
            t for t in db.taxonomy.get_topics()
            if t.get("collection") == "dup__coll"
        ]
        assert len(topics) == count1

    def test_show_cmd(self, tmp_path: Path) -> None:
        """nx taxonomy show <id> displays documents."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("test-topic", "proj", 2, "2026-01-01T00:00:00Z"),
            )
            tid = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.taxonomy.conn.executemany(
                "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                [("doc-a", tid), ("doc-b", tid)],
            )
            db.taxonomy.conn.commit()

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(taxonomy, ["show", str(tid)])

        assert result.exit_code == 0, result.output
        assert "doc-a" in result.output
        assert "doc-b" in result.output
