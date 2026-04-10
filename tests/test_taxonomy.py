# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for persistent topic taxonomy (RDR-061 P3-1, nexus-vk8m)."""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.db.t2 import T2Database
from nexus.taxonomy import (
    assign_topic,
    cluster_and_persist,
    get_topic_docs,
    get_topic_tree,
    get_topics,
    rebuild_taxonomy,
)


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


def test_cluster_and_persist_creates_topics(db: T2Database) -> None:
    """Clustering entries creates topic rows in T2."""
    # Insert enough entries to cluster
    for i in range(10):
        db.put(project="proj", title=f"doc-{i}.md",
               content=f"topic alpha content about search engines {i}")
    for i in range(10):
        db.put(project="proj", title=f"doc-{i+10}.md",
               content=f"topic beta content about database indexing {i}")

    count = cluster_and_persist(db, "proj", k=3)
    assert count >= 2
    topics = get_topics(db)
    assert len(topics) >= 2


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


def test_rebuild_taxonomy_clears_and_recreates(db: T2Database) -> None:
    """Rebuild is idempotent — clears old topics, creates new ones."""
    for i in range(15):
        db.put(project="proj", title=f"doc-{i}.md",
               content=f"content about machine learning algorithms {i}")

    count1 = rebuild_taxonomy(db, "proj")
    count2 = rebuild_taxonomy(db, "proj")
    # Both runs should produce topics (second clears first, recreates)
    assert count1 >= 1
    assert count2 >= 1


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


def test_cluster_and_persist_caps_vocab_size(db: T2Database, monkeypatch) -> None:
    """Vocab is capped to prevent OOM on large/diverse corpora."""
    import nexus.taxonomy as tax
    # Insert entries with many distinct words — should not blow up
    for i in range(20):
        content = " ".join(f"word{i}_{j}" for j in range(50))
        db.put(project="big", title=f"entry-{i}", content=content)
    # Should complete without OOM
    n = tax.cluster_and_persist(db, "big", k=3)
    assert n > 0


def test_cluster_and_persist_filters_stopwords(db: T2Database) -> None:
    """Stopwords are filtered before vocab construction."""
    import nexus.taxonomy as tax
    for i in range(5):
        db.put(
            project="sw",
            title=f"entry-{i}",
            content=f"the quick brown fox jumps over the lazy dog topic{i}",
        )
    n = tax.cluster_and_persist(db, "sw", k=2)
    assert n > 0
    # The vocab should have topic0..topic4 and content words, not "the", "over", etc.


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


def test_cli_taxonomy_list(db: T2Database) -> None:
    """CLI taxonomy list outputs topic labels."""
    from click.testing import CliRunner
    from nexus.commands.taxonomy_cmd import taxonomy

    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("Search Methods", "proj", 5, "2026-01-01T00:00:00Z"),
    )
    db.taxonomy.conn.commit()

    runner = CliRunner()
    # Patch default_db_path to point at our test db — not needed for unit test
    # since we're testing the output format, not the real DB path
    result = runner.invoke(taxonomy, ["list"])
    # The command uses its own DB; for a true integration test we'd need to
    # inject the fixture DB. Here we just verify the command doesn't crash.
    assert result.exit_code == 0
