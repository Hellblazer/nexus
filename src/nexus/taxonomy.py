# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Persistent topic taxonomy via Ward hierarchical clustering (RDR-061 P3-1).

Pure-function module: all I/O via T2Database parameter. Uses
search_clusterer.cluster_results() as the clustering engine.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import numpy as np
import structlog

if TYPE_CHECKING:
    from nexus.db.t2 import T2Database

_log = structlog.get_logger()

_TOPIC_COLUMNS = ("id", "label", "parent_id", "collection", "centroid_hash", "doc_count", "created_at")


def get_topics(
    db: "T2Database",
    *,
    parent_id: int | None = ...,  # sentinel: ... means "filter roots", None means no filter
) -> list[dict[str, Any]]:
    """Return topics filtered by parent.

    - ``parent_id=None`` (default) or ``...``: return root topics (parent_id IS NULL).
    - ``parent_id=<int>``: return children of that topic.
    """
    with db._lock:
        if parent_id is ... or parent_id is None:
            rows = db.conn.execute(
                "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at "
                "FROM topics WHERE parent_id IS NULL ORDER BY doc_count DESC"
            ).fetchall()
        else:
            rows = db.conn.execute(
                "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at "
                "FROM topics WHERE parent_id = ? ORDER BY doc_count DESC",
                (parent_id,),
            ).fetchall()
    return [dict(zip(_TOPIC_COLUMNS, row)) for row in rows]


def assign_topic(db: "T2Database", doc_id: str, topic_id: int) -> None:
    """Assign a document to a topic (idempotent via INSERT OR IGNORE)."""
    with db._lock:
        db.conn.execute(
            "INSERT OR IGNORE INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
            (doc_id, topic_id),
        )
        db.conn.commit()


def get_topic_docs(
    db: "T2Database",
    topic_id: int,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return doc_ids and titles assigned to a topic."""
    with db._lock:
        rows = db.conn.execute(
            """
            SELECT ta.doc_id, m.title, m.project
            FROM topic_assignments ta
            LEFT JOIN memory m ON m.title = ta.doc_id AND m.project = (
                SELECT collection FROM topics WHERE id = ta.topic_id
            )
            WHERE ta.topic_id = ?
            LIMIT ?
            """,
            (topic_id, limit),
        ).fetchall()
    return [{"doc_id": r[0], "title": r[1] or r[0], "project": r[2] or ""} for r in rows]


def get_topic_tree(
    db: "T2Database",
    collection: str = "",
    *,
    max_depth: int = 2,
) -> list[dict[str, Any]]:
    """Return topics as a nested tree structure.

    Each node: {id, label, doc_count, children: [...]}.
    Filtered by collection when provided.
    """
    with db._lock:
        if collection:
            roots = db.conn.execute(
                "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at "
                "FROM topics WHERE parent_id IS NULL AND collection = ? ORDER BY doc_count DESC",
                (collection,),
            ).fetchall()
        else:
            roots = db.conn.execute(
                "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at "
                "FROM topics WHERE parent_id IS NULL ORDER BY doc_count DESC"
            ).fetchall()

    def _build_node(row: tuple, depth: int) -> dict[str, Any]:
        node = {"id": row[0], "label": row[1], "collection": row[3], "doc_count": row[5]}
        if depth < max_depth:
            with db._lock:
                children = db.conn.execute(
                    "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at "
                    "FROM topics WHERE parent_id = ? ORDER BY doc_count DESC",
                    (row[0],),
                ).fetchall()
            node["children"] = [_build_node(c, depth + 1) for c in children]
        else:
            node["children"] = []
        return node

    return [_build_node(r, 0) for r in roots]


def cluster_and_persist(
    db: "T2Database",
    project: str,
    *,
    k: int | None = None,
) -> int:
    """Cluster memory entries by content word vectors, persist topics to T2.

    Uses simple word-frequency vectors (no external embeddings required)
    and the existing search_clusterer.cluster_results() engine.

    Returns number of topics created.
    """
    from nexus.search_clusterer import cluster_results

    entries = db.get_all(project)
    if len(entries) < 3:
        return 0

    # Build simple word-frequency vectors for clustering
    vocab: dict[str, int] = {}
    for e in entries:
        for word in e.get("content", "").lower().split():
            if word not in vocab and len(word) > 2:
                vocab[word] = len(vocab)

    if not vocab:
        return 0

    dim = len(vocab)
    embeddings = np.zeros((len(entries), dim), dtype=np.float32)
    for i, e in enumerate(entries):
        for word in e.get("content", "").lower().split():
            if word in vocab:
                embeddings[i, vocab[word]] += 1.0

    # Normalize rows
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    embeddings = embeddings / np.maximum(norms, 1e-9)

    # Build result dicts for cluster_results API
    result_dicts = [
        {"id": e["title"], "content": e.get("content", ""),
         "distance": 0.0, "metadata": {"title": e["title"]}}
        for e in entries
    ]

    if k is None:
        k = max(2, math.ceil(len(entries) / 5))

    clusters = cluster_results(result_dicts, embeddings, k=k)

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    count = 0
    with db._lock:
        for cluster in clusters:
            if not cluster:
                continue
            label = cluster[0].get("_cluster_label", f"topic-{count}")
            db.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
                (label, project, len(cluster), now),
            )
            topic_id = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for r in cluster:
                db.conn.execute(
                    "INSERT OR IGNORE INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                    (r["id"], topic_id),
                )
            count += 1
        db.conn.commit()

    return count


def rebuild_taxonomy(db: "T2Database", project: str, *, k: int | None = None) -> int:
    """Full rebuild: delete existing topics for project, recluster."""
    with db._lock:
        db.conn.execute("DELETE FROM topic_assignments WHERE topic_id IN "
                        "(SELECT id FROM topics WHERE collection = ?)", (project,))
        db.conn.execute("DELETE FROM topics WHERE collection = ?", (project,))
        db.conn.commit()
    return cluster_and_persist(db, project, k=k)
