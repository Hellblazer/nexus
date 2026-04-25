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
    from nexus.mcp_infra import _post_store_batch_hooks, _post_store_hooks
    _post_store_hooks.clear()
    _post_store_batch_hooks.clear()
    yield
    _post_store_hooks.clear()
    _post_store_batch_hooks.clear()


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


# ── Batch hook mechanism (RDR-095, nexus-wxcb) ───────────────────────────────


def test_register_post_store_batch_hook_appends() -> None:
    """register_post_store_batch_hook appends to the module-level list."""
    from nexus.mcp_infra import _post_store_batch_hooks, register_post_store_batch_hook

    def probe(doc_ids, collection, contents, embeddings, metadatas):
        return None

    assert _post_store_batch_hooks == []
    register_post_store_batch_hook(probe)
    assert _post_store_batch_hooks == [probe]


def test_fire_post_store_batch_hooks_invokes_registered() -> None:
    """fire forwards all five parameters unchanged to each registered hook."""
    from nexus.mcp_infra import (
        fire_post_store_batch_hooks,
        register_post_store_batch_hook,
    )

    seen: list[tuple] = []

    def hook_a(doc_ids, collection, contents, embeddings, metadatas):
        seen.append(("a", tuple(doc_ids), collection,
                     tuple(contents), embeddings, metadatas))

    def hook_b(doc_ids, collection, contents, embeddings, metadatas):
        seen.append(("b", tuple(doc_ids), collection,
                     tuple(contents), embeddings, metadatas))

    register_post_store_batch_hook(hook_a)
    register_post_store_batch_hook(hook_b)

    embeddings = [[0.1, 0.2], [0.3, 0.4]]
    metadatas = [{"k": "v1"}, {"k": "v2"}]
    fire_post_store_batch_hooks(
        ["d1", "d2"], "code__nexus", ["c1", "c2"], embeddings, metadatas,
    )

    assert seen == [
        ("a", ("d1", "d2"), "code__nexus", ("c1", "c2"), embeddings, metadatas),
        ("b", ("d1", "d2"), "code__nexus", ("c1", "c2"), embeddings, metadatas),
    ]


def test_fire_post_store_batch_hooks_empty_doc_ids_early_return() -> None:
    """Empty doc_ids returns early — no hooks fire on empty batches."""
    from nexus.mcp_infra import (
        fire_post_store_batch_hooks,
        register_post_store_batch_hook,
    )

    calls: list = []
    register_post_store_batch_hook(
        lambda doc_ids, collection, contents, embeddings, metadatas: calls.append(1)
    )

    fire_post_store_batch_hooks([], "x", [], None, None)
    assert calls == []


def test_fire_post_store_batch_hooks_isolation(tmp_path: Path, monkeypatch) -> None:
    """First hook raising must not block the second hook from firing.

    Uses a synthetic raising probe — the real taxonomy_assign_batch_hook
    body wraps everything in its own try/except and so cannot exercise the
    framework's failure-capture path.
    """
    import nexus.mcp_infra as mod
    from nexus.db.migrations import (
        migrate_hook_failures,
        migrate_hook_failures_batch_columns,
    )
    from nexus.mcp_infra import (
        fire_post_store_batch_hooks,
        register_post_store_batch_hook,
    )
    import sqlite3

    db_path = tmp_path / "batch_hook_failures.db"
    T2Database(db_path).close()
    conn = sqlite3.connect(str(db_path))
    migrate_hook_failures(conn)
    migrate_hook_failures_batch_columns(conn)
    conn.close()
    monkeypatch.setattr(mod, "t2_ctx", lambda: T2Database(db_path))

    second_calls: list = []

    def raising_probe(doc_ids, collection, contents, embeddings, metadatas):
        raise RuntimeError("simulated batch failure")

    def survivor(doc_ids, collection, contents, embeddings, metadatas):
        second_calls.append(tuple(doc_ids))

    register_post_store_batch_hook(raising_probe)
    register_post_store_batch_hook(survivor)

    fire_post_store_batch_hooks(
        ["doc-1", "doc-2", "doc-3"], "code__nexus", ["c1", "c2", "c3"], None, None,
    )

    assert second_calls == [("doc-1", "doc-2", "doc-3")]

    with T2Database(db_path) as db:
        row = db.taxonomy.conn.execute(
            "SELECT doc_id, collection, hook_name, error, batch_doc_ids, is_batch "
            "FROM hook_failures"
        ).fetchone()

    assert row is not None
    doc_id, collection, hook_name, error, batch_doc_ids, is_batch = row
    assert doc_id == "doc-1"  # representative scalar
    assert collection == "code__nexus"
    assert hook_name == "raising_probe"
    assert "simulated batch failure" in error
    assert batch_doc_ids == '["doc-1", "doc-2", "doc-3"]'
    assert is_batch == 1


def test_fire_post_store_batch_hooks_partial_commit_failure_mode(
    tmp_path: Path, monkeypatch,
) -> None:
    """A batch hook may commit sub-step A, then raise on sub-step B.

    Validates the documented contract: framework writes one hook_failures
    row capturing the full doc_id list and exception text. Per-sub-step
    capture is hook-internal, not framework-level.
    """
    import nexus.mcp_infra as mod
    from nexus.db.migrations import (
        migrate_hook_failures,
        migrate_hook_failures_batch_columns,
    )
    from nexus.mcp_infra import (
        fire_post_store_batch_hooks,
        register_post_store_batch_hook,
    )
    import sqlite3

    db_path = tmp_path / "partial_commit.db"
    T2Database(db_path).close()
    conn = sqlite3.connect(str(db_path))
    migrate_hook_failures(conn)
    migrate_hook_failures_batch_columns(conn)
    conn.close()
    monkeypatch.setattr(mod, "t2_ctx", lambda: T2Database(db_path))

    sub_step_log: list[str] = []

    def two_step_hook(doc_ids, collection, contents, embeddings, metadatas):
        sub_step_log.append("step_a_committed")
        # Simulate cross-collection projection failure after same-collection
        # assignment landed.
        raise RuntimeError("cross_collection_projection_failed")

    register_post_store_batch_hook(two_step_hook)

    fire_post_store_batch_hooks(
        ["doc-a", "doc-b"], "knowledge__delos", ["c1", "c2"], None, None,
    )

    assert sub_step_log == ["step_a_committed"]

    with T2Database(db_path) as db:
        rows = db.taxonomy.conn.execute(
            "SELECT batch_doc_ids, is_batch, error FROM hook_failures"
        ).fetchall()

    assert len(rows) == 1
    batch_doc_ids, is_batch, error = rows[0]
    assert batch_doc_ids == '["doc-a", "doc-b"]'
    assert is_batch == 1
    assert "cross_collection_projection_failed" in error


def test_fire_post_store_batch_hooks_falls_back_to_scalar_when_columns_absent(
    tmp_path: Path, monkeypatch,
) -> None:
    """If batch_doc_ids/is_batch columns aren't migrated yet (P1.1 merged
    before P1.2), the failure capture writes a scalar-only row rather than
    crashing."""
    import nexus.mcp_infra as mod
    from nexus.db.migrations import migrate_hook_failures
    from nexus.mcp_infra import (
        fire_post_store_batch_hooks,
        register_post_store_batch_hook,
    )
    import sqlite3

    db_path = tmp_path / "pre_migration.db"
    T2Database(db_path).close()
    conn = sqlite3.connect(str(db_path))
    migrate_hook_failures(conn)  # but NOT migrate_hook_failures_batch_columns
    conn.close()
    monkeypatch.setattr(mod, "t2_ctx", lambda: T2Database(db_path))

    def raising(doc_ids, collection, contents, embeddings, metadatas):
        raise RuntimeError("kaboom")

    register_post_store_batch_hook(raising)
    fire_post_store_batch_hooks(
        ["d1", "d2"], "code__nexus", ["c1", "c2"], None, None,
    )

    with T2Database(db_path) as db:
        cols = {
            r[1] for r in db.taxonomy.conn.execute(
                "PRAGMA table_info(hook_failures)"
            ).fetchall()
        }
        rows = db.taxonomy.conn.execute(
            "SELECT doc_id, hook_name, error FROM hook_failures"
        ).fetchall()

    assert "batch_doc_ids" not in cols  # confirm the pre-migration shape
    assert len(rows) == 1
    assert rows[0][0] == "d1"
    assert rows[0][1] == "raising"
    assert "kaboom" in rows[0][2]


def test_fire_post_store_batch_hooks_persist_failure_is_best_effort(
    monkeypatch,
) -> None:
    """If the persist path itself raises, fire_post_store_batch_hooks
    still returns and ingest continues."""
    import nexus.mcp_infra as mod
    from nexus.mcp_infra import (
        fire_post_store_batch_hooks,
        register_post_store_batch_hook,
    )

    monkeypatch.setattr(mod, "t2_ctx", lambda: (_ for _ in ()).throw(
        RuntimeError("t2 offline"),
    ))

    def raising(doc_ids, collection, contents, embeddings, metadatas):
        raise RuntimeError("primary failure")

    register_post_store_batch_hook(raising)
    # Must not raise even though both hook AND persist path fail.
    fire_post_store_batch_hooks(["d1"], "c", ["x"], None, None)


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
