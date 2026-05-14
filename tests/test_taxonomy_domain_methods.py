# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for CatalogTaxonomy command-layer domain methods (RDR-112 P0.5, nexus-49bw).

Each method here replaces a previous ``db.taxonomy.conn.execute``
reach-through from ``src/nexus/commands/taxonomy_cmd.py``. The tests
seed rows directly via the underlying connection so behaviour is
verified independently of the discovery / projection pipelines.
"""
from __future__ import annotations

import json

import pytest

from nexus.db.t2 import T2Database


def _seed_topic(
    db: T2Database, *, label: str, collection: str,
    doc_count: int = 0, review_status: str = "pending",
) -> int:
    cur = db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, review_status, terms, created_at) "
        "VALUES (?, ?, ?, ?, '[]', datetime('now'))",
        (label, collection, doc_count, review_status),
    )
    db.taxonomy.conn.commit()
    return cur.lastrowid


# ── get_collection_topic_stats ─────────────────────────────────────────────


def test_collection_topic_stats_empty(db: T2Database) -> None:
    assert db.taxonomy.get_collection_topic_stats() == []


def test_collection_topic_stats_grouped_and_ordered(db: T2Database) -> None:
    _seed_topic(db, label="a", collection="docs", doc_count=10, review_status="accepted")
    _seed_topic(db, label="b", collection="docs", doc_count=5, review_status="pending")
    _seed_topic(db, label="c", collection="code", doc_count=20, review_status="pending")
    rows = db.taxonomy.get_collection_topic_stats()
    # code has 20 docs, docs has 15 — code should come first.
    assert rows[0] == ("code", 1, 20, 1, 0)
    assert rows[1] == ("docs", 2, 15, 1, 1)


# ── count_topic_links ──────────────────────────────────────────────────────


def test_count_topic_links_zero(db: T2Database) -> None:
    assert db.taxonomy.count_topic_links() == 0


def test_count_topic_links_nonzero(db: T2Database) -> None:
    a = _seed_topic(db, label="a", collection="x")
    b = _seed_topic(db, label="b", collection="x")
    db.taxonomy.conn.execute(
        "INSERT INTO topic_links (from_topic_id, to_topic_id, link_count, link_types) "
        "VALUES (?, ?, ?, ?)",
        (a, b, 3, json.dumps(["projection"])),
    )
    db.taxonomy.conn.commit()
    assert db.taxonomy.count_topic_links() == 1


# ── get_taxonomy_meta ──────────────────────────────────────────────────────


def test_get_taxonomy_meta_missing(db: T2Database) -> None:
    assert db.taxonomy.get_taxonomy_meta("never-seen") is None


def test_get_taxonomy_meta_present(db: T2Database) -> None:
    db.taxonomy.conn.execute(
        "INSERT INTO taxonomy_meta (collection, last_discover_doc_count, last_discover_at) "
        "VALUES (?, ?, ?)",
        ("docs", 42, "2026-05-14T00:00:00"),
    )
    db.taxonomy.conn.commit()
    assert db.taxonomy.get_taxonomy_meta("docs") == (42, "2026-05-14T00:00:00")


# ── count_assigned_docs ────────────────────────────────────────────────────


def test_count_assigned_docs_empty(db: T2Database) -> None:
    assert db.taxonomy.count_assigned_docs() == 0
    assert db.taxonomy.count_assigned_docs("docs") == 0


def test_count_assigned_docs_total_and_filtered(db: T2Database) -> None:
    a = _seed_topic(db, label="a", collection="docs")
    b = _seed_topic(db, label="b", collection="code")
    for tid, doc in [(a, "d1"), (a, "d2"), (b, "d3"), (b, "d3")]:
        db.taxonomy.conn.execute(
            "INSERT OR IGNORE INTO topic_assignments (topic_id, doc_id) VALUES (?, ?)",
            (tid, doc),
        )
    db.taxonomy.conn.commit()
    assert db.taxonomy.count_assigned_docs() == 3
    assert db.taxonomy.count_assigned_docs("docs") == 2
    assert db.taxonomy.count_assigned_docs("code") == 1


# ── get_recent_hook_failures ───────────────────────────────────────────────


def test_get_recent_hook_failures_empty(db: T2Database) -> None:
    assert db.taxonomy.get_recent_hook_failures() == []


def test_get_recent_hook_failures_modern_schema(db: T2Database) -> None:
    db.taxonomy.conn.execute(
        "INSERT INTO hook_failures (hook_name, is_batch, batch_doc_ids) VALUES (?, ?, ?)",
        ("topic_assign", 1, json.dumps(["d1", "d2"])),
    )
    db.taxonomy.conn.execute(
        "INSERT INTO hook_failures (hook_name, is_batch) VALUES (?, ?)",
        ("centroid_update", 0),
    )
    db.taxonomy.conn.commit()
    rows = db.taxonomy.get_recent_hook_failures()
    assert len(rows) == 2
    by_name = {r[0]: r for r in rows}
    assert by_name["topic_assign"][1] == 1
    assert json.loads(by_name["topic_assign"][2]) == ["d1", "d2"]
    assert by_name["centroid_update"] == ("centroid_update", 0, None)


def test_get_recent_hook_failures_outside_window(db: T2Database) -> None:
    db.taxonomy.conn.execute(
        "INSERT INTO hook_failures (hook_name, occurred_at) "
        "VALUES (?, datetime('now', '-5 days'))",
        ("ancient",),
    )
    db.taxonomy.conn.commit()
    assert db.taxonomy.get_recent_hook_failures() == []


def test_get_recent_hook_failures_no_table(db: T2Database) -> None:
    db.taxonomy.conn.execute("DROP TABLE hook_failures")
    db.taxonomy.conn.commit()
    assert db.taxonomy.get_recent_hook_failures() == []


# ── list_topic_link_rows_with_labels ───────────────────────────────────────


def test_list_topic_link_rows_with_labels_filtered_and_unfiltered(db: T2Database) -> None:
    a = _seed_topic(db, label="alpha", collection="docs")
    b = _seed_topic(db, label="beta", collection="code")
    c = _seed_topic(db, label="gamma", collection="docs")
    for f, t, n in [(a, b, 7), (a, c, 3)]:
        db.taxonomy.conn.execute(
            "INSERT INTO topic_links (from_topic_id, to_topic_id, link_count, link_types) "
            "VALUES (?, ?, ?, ?)",
            (f, t, n, json.dumps(["projection"])),
        )
    db.taxonomy.conn.commit()

    all_rows = db.taxonomy.list_topic_link_rows_with_labels()
    assert len(all_rows) == 2
    # Order: descending by link_count.
    assert all_rows[0][:5] == ("alpha", "docs", "beta", "code", 7)
    assert all_rows[1][:5] == ("alpha", "docs", "gamma", "docs", 3)

    code_only = db.taxonomy.list_topic_link_rows_with_labels(collection="code")
    assert len(code_only) == 1
    assert code_only[0][:5] == ("alpha", "docs", "beta", "code", 7)
