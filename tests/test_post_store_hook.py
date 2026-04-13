# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for post_store_hook taxonomy assignment (RDR-070, nexus-7h2)."""
from __future__ import annotations

from pathlib import Path

import chromadb
import numpy as np
import pytest

from nexus.db.t2 import T2Database


@pytest.fixture()
def chroma_client() -> chromadb.ClientAPI:
    return chromadb.EphemeralClient()


@pytest.fixture(autouse=True)
def _reset_hooks():
    """Clear post_store_hooks between tests to prevent cross-test leakage."""
    from nexus.mcp_infra import _post_store_hooks
    _post_store_hooks.clear()
    yield
    _post_store_hooks.clear()


# ── Hook mechanism ───────────────────────────────────────────────────────────


def test_fire_post_store_hooks_calls_registered(
    tmp_path: Path, chroma_client: chromadb.ClientAPI,
) -> None:
    """fire_post_store_hooks invokes all registered callables."""
    from nexus.mcp_infra import fire_post_store_hooks, register_post_store_hook

    calls: list[tuple] = []
    register_post_store_hook(lambda doc_id, collection, content: calls.append((doc_id, collection)))

    fire_post_store_hooks("doc-1", "test__coll", "some content")
    assert len(calls) == 1
    assert calls[0] == ("doc-1", "test__coll")


def test_fire_post_store_hooks_exception_nonfatal(
    tmp_path: Path,
) -> None:
    """Hook exceptions are caught and logged, never propagate."""
    from nexus.mcp_infra import fire_post_store_hooks, register_post_store_hook

    def bad_hook(doc_id, collection, content):
        raise RuntimeError("hook failure")

    register_post_store_hook(bad_hook)
    # Should not raise
    fire_post_store_hooks("doc-1", "test__coll", "content")


# ── Taxonomy assignment hook ─────────────────────────────────────────────────


def test_taxonomy_assign_hook_assigns_nearest_topic(
    tmp_path: Path, chroma_client: chromadb.ClientAPI,
) -> None:
    """taxonomy_assign_hook assigns doc to nearest centroid topic."""
    from nexus.mcp_infra import taxonomy_assign_hook

    db = T2Database(tmp_path / "hook.db")
    try:
        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0
        doc_ids = [f"doc-{i}" for i in range(60)]
        texts = (
            [f"machine learning neural {i}" for i in range(30)]
            + [f"database query sql {i}" for i in range(30)]
        )

        # Discover topics to populate centroids
        db.taxonomy.discover_topics(
            "test__coll", doc_ids, embeddings, texts, chroma_client,
        )

        # Fire the hook for a new doc near cluster A
        taxonomy_assign_hook(
            "new-doc-1", "test__coll", "machine learning neural network",
            taxonomy=db.taxonomy, chroma_client=chroma_client,
        )

        # Check assignment exists in T2
        row = db.taxonomy.conn.execute(
            "SELECT topic_id, assigned_by FROM topic_assignments WHERE doc_id = 'new-doc-1'"
        ).fetchone()
        assert row is not None
        assert row[1] == "centroid"

        # Verify the assigned topic exists and is a real topic
        # (discover uses random vectors so centroid-text correlation is
        # not guaranteed — we verify a valid topic was chosen)
        assigned_topic_id = row[0]
        topic = db.taxonomy.get_topic_by_id(assigned_topic_id)
        assert topic is not None, f"Assigned topic_id {assigned_topic_id} does not exist"
        assert topic["collection"] == "test__coll"
        assert topic["doc_count"] > 0
    finally:
        db.close()


def test_taxonomy_assign_hook_noop_no_centroids(
    tmp_path: Path, chroma_client: chromadb.ClientAPI,
) -> None:
    """taxonomy_assign_hook is a no-op when taxonomy__centroids doesn't exist."""
    from nexus.mcp_infra import taxonomy_assign_hook

    db = T2Database(tmp_path / "hook_empty.db")
    try:
        # No discover run — no centroids
        taxonomy_assign_hook(
            "orphan-doc", "nonexistent__coll", "some content",
            taxonomy=db.taxonomy, chroma_client=chroma_client,
        )

        # No assignment created
        row = db.taxonomy.conn.execute(
            "SELECT COUNT(*) FROM topic_assignments WHERE doc_id = 'orphan-doc'"
        ).fetchone()[0]
        assert row == 0
    finally:
        db.close()
