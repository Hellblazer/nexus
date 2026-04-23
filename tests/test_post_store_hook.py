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


def test_fire_post_store_hooks_persists_failure_to_t2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GH #251: hook failures are persisted to T2 hook_failures for status surfacing.

    The migration is registered at 4.9.10; the running package is still 4.9.9
    so T2Database's automatic ``apply_pending`` does not create the table.
    We apply the migration directly so the write path has a target.
    """
    import sqlite3

    import nexus.mcp_infra as mod
    from nexus.db.migrations import migrate_hook_failures
    from nexus.mcp_infra import fire_post_store_hooks, register_post_store_hook

    db_path = tmp_path / "hook_failures.db"
    T2Database(db_path).close()  # run base migrations first

    conn = sqlite3.connect(str(db_path))
    migrate_hook_failures(conn)
    conn.close()

    monkeypatch.setattr(mod, "t2_ctx", lambda: T2Database(db_path))

    def bad_hook(doc_id, collection, content):
        raise RuntimeError("simulated centroid failure")

    register_post_store_hook(bad_hook)
    fire_post_store_hooks("doc-xyz", "knowledge__thing", "content")

    with T2Database(db_path) as db:
        rows = db.taxonomy.conn.execute(
            "SELECT doc_id, collection, hook_name, error FROM hook_failures"
        ).fetchall()

    assert len(rows) == 1
    doc_id, coll, hook_name, error = rows[0]
    assert doc_id == "doc-xyz"
    assert coll == "knowledge__thing"
    assert hook_name == "bad_hook"
    assert "simulated centroid failure" in error


def test_fire_post_store_hooks_persist_swallowed_when_table_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GH #251: if hook_failures table is absent (pre-4.9.10 DB), store_put
    is never blocked — the insert failure is caught silently."""
    import nexus.mcp_infra as mod
    from nexus.mcp_infra import fire_post_store_hooks, register_post_store_hook

    db_path = tmp_path / "no_hook_table.db"
    T2Database(db_path).close()  # base schema only — no hook_failures table
    monkeypatch.setattr(mod, "t2_ctx", lambda: T2Database(db_path))

    def bad_hook(doc_id, collection, content):
        raise RuntimeError("primary failure")

    register_post_store_hook(bad_hook)
    # Must not raise even though the persist path will hit "no such table".
    fire_post_store_hooks("d", "c", "content")


def test_fire_post_store_hooks_persist_failure_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GH #251: if the persist path itself raises, the hook contract still holds."""
    import nexus.mcp_infra as mod
    from nexus.mcp_infra import fire_post_store_hooks, register_post_store_hook

    def _broken_ctx():
        raise RuntimeError("t2 offline")

    monkeypatch.setattr(mod, "t2_ctx", _broken_ctx)

    def bad_hook(doc_id, collection, content):
        raise RuntimeError("original failure")

    register_post_store_hook(bad_hook)
    # Must not raise even though both the hook AND the persist path failed.
    fire_post_store_hooks("d", "c", "content")


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

        # Check same-collection centroid assignment exists in T2.
        # RDR-075: the hook may also create cross-collection projection
        # assignments (assigned_by='projection') if centroids exist in
        # other collections — we only assert the centroid assignment here.
        row = db.taxonomy.conn.execute(
            "SELECT topic_id FROM topic_assignments "
            "WHERE doc_id = 'new-doc-1' AND assigned_by = 'centroid'"
        ).fetchone()
        assert row is not None, "Same-collection centroid assignment missing"

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
    tmp_path: Path,
) -> None:
    """taxonomy_assign_hook is a no-op when taxonomy__centroids doesn't exist.

    RDR-075: when cross-collection projection is active, centroids from any
    collection would trigger assignment. This test uses a first-in-process
    client state to verify the no-centroids path. When run after other tests
    that populate centroids via the shared ephemeral-client process state,
    it correctly assigns via cross-collection projection (tested separately
    in tests/test_taxonomy.py::test_assign_single_cross_collection_finds_foreign_topic).
    """
    from nexus.mcp_infra import taxonomy_assign_hook

    # Use a dedicated chroma client created fresh for this test only.
    # If a prior test has already created the shared "ephemeral" instance,
    # skip — this test's semantics only hold on a truly empty state.
    try:
        client = chromadb.EphemeralClient()
    except ValueError:
        pytest.skip("EphemeralClient already initialized by prior test")

    db = T2Database(tmp_path / "hook_empty.db")
    try:
        # Delete any pre-existing taxonomy__centroids to ensure clean state
        try:
            client.delete_collection("taxonomy__centroids")
        except Exception:
            pass

        taxonomy_assign_hook(
            "orphan-doc", "nonexistent__coll", "some content",
            taxonomy=db.taxonomy, chroma_client=client,
        )

        # No assignment created (no centroids exist at all)
        row = db.taxonomy.conn.execute(
            "SELECT COUNT(*) FROM topic_assignments WHERE doc_id = 'orphan-doc'"
        ).fetchone()[0]
        assert row == 0
    finally:
        db.close()
