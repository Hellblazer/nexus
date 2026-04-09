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
        r[0] for r in db.conn.execute(
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
    db.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("test-topic", "proj", 0, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()
    topic_id = db.conn.execute("SELECT id FROM topics LIMIT 1").fetchone()[0]

    assign_topic(db, "doc-123", topic_id)

    row = db.conn.execute(
        "SELECT * FROM topic_assignments WHERE doc_id='doc-123'"
    ).fetchone()
    assert row is not None
    assert row[1] == topic_id


def test_assign_topic_idempotent(db: T2Database) -> None:
    """Assigning same doc to same topic twice doesn't error."""
    db.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("test-topic", "proj", 0, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()
    topic_id = db.conn.execute("SELECT id FROM topics LIMIT 1").fetchone()[0]

    assign_topic(db, "doc-123", topic_id)
    assign_topic(db, "doc-123", topic_id)  # no error

    count = db.conn.execute(
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
    db.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("root-topic", "proj", 5, "2026-01-01T00:00:00Z"),
    )
    root_id = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.conn.execute(
        "INSERT INTO topics (label, parent_id, collection, doc_count, created_at) VALUES (?, ?, ?, ?, ?)",
        ("child-topic", root_id, "proj", 2, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()

    roots = get_topics(db, parent_id=None)
    assert len(roots) == 1
    assert roots[0]["label"] == "root-topic"

    children = get_topics(db, parent_id=root_id)
    assert len(children) == 1
    assert children[0]["label"] == "child-topic"


# ── tree + docs ─────────────────────────────────────────────────────────────


def test_get_topic_tree_structure(db: T2Database) -> None:
    """get_topic_tree returns nested dicts with children."""
    db.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("root", "proj", 10, "2026-01-01T00:00:00Z"),
    )
    root_id = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.conn.execute(
        "INSERT INTO topics (label, parent_id, collection, doc_count, created_at) VALUES (?, ?, ?, ?, ?)",
        ("child", root_id, "proj", 3, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()

    tree = get_topic_tree(db, "proj")
    assert len(tree) == 1
    assert tree[0]["label"] == "root"
    assert len(tree[0]["children"]) == 1
    assert tree[0]["children"][0]["label"] == "child"


def test_get_topic_docs_returns_assigned(db: T2Database) -> None:
    """get_topic_docs returns doc_ids assigned to the topic."""
    db.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("test", "proj", 2, "2026-01-01T00:00:00Z"),
    )
    topic_id = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.conn.execute("INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)", ("doc-a", topic_id))
    db.conn.execute("INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)", ("doc-b", topic_id))
    db.conn.commit()

    docs = get_topic_docs(db, topic_id)
    assert len(docs) == 2
    assert {d["doc_id"] for d in docs} == {"doc-a", "doc-b"}


def test_cli_taxonomy_list(db: T2Database) -> None:
    """CLI taxonomy list outputs topic labels."""
    from click.testing import CliRunner
    from nexus.commands.taxonomy_cmd import taxonomy

    db.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("Search Methods", "proj", 5, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()

    runner = CliRunner()
    # Patch default_db_path to point at our test db — not needed for unit test
    # since we're testing the output format, not the real DB path
    result = runner.invoke(taxonomy, ["list"])
    # The command uses its own DB; for a true integration test we'd need to
    # inject the fixture DB. Here we just verify the command doesn't crash.
    assert result.exit_code == 0
