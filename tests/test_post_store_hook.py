# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the post-store hook chains (RDR-070 single-doc, RDR-095 batch).

RDR-118 P1.S5 MVV (nexus-rkkn2): rewritten against the ``runtime``
fixture introduced in P1.S1 (bead nexus-atf8a). Each test constructs a
fresh ``NexusRuntime`` via the fixture so the HookRegistry is
naturally isolated; the legacy autouse
``_restore_post_store_batch_hooks_after_test`` snapshot/restore
machinery is no longer load-bearing for this file. The file-level
``no_legacy_isolation`` marker disables that autouse so the runtime
container's isolation is the only mechanism in scope.

Phase 1 closure gate: the rewrite must pass

1. With the legacy autouse fixtures still in place (running
   harmlessly because the test file does not depend on their
   isolation), AND
2. With the legacy autouse fixtures locally disabled via marker (the
   ``no_legacy_isolation`` pytestmark below).

Local ``_reset_hooks`` autouse from the pre-rewrite file (18 LOC of
snapshot/restore against the module-level lists) deletes entirely.
"""
from __future__ import annotations

from pathlib import Path

import chromadb
import pytest

from nexus.db.t2 import T2Database


# File-level: every test in this module exercises hook isolation purely
# through the ``runtime`` fixture. The marker disables the legacy
# ``_restore_post_store_batch_hooks_after_test`` autouse so the file
# stands on its own; criterion (2) of the Phase 1 MVV gate.
pytestmark = [
    pytest.mark.usefixtures("runtime"),
    pytest.mark.no_legacy_isolation,
]


@pytest.fixture()
def chroma_client() -> chromadb.ClientAPI:
    return chromadb.EphemeralClient()


# ── Single-doc hook chain (RDR-070, nexus-7h2) ───────────────────────────────


def test_fire_post_store_hooks_calls_registered(runtime) -> None:
    """``fire_single`` invokes every callable registered on the runtime's
    HookRegistry with the ``(doc_id, collection, content)`` shape."""
    calls: list[tuple] = []
    runtime.hooks.register_single(
        lambda doc_id, collection, content: calls.append(
            (doc_id, collection)
        )
    )
    runtime.hooks.fire_single("doc-1", "test__coll", "some content")
    assert len(calls) == 1
    assert calls[0] == ("doc-1", "test__coll")


def test_fire_post_store_hooks_exception_nonfatal(runtime) -> None:
    """A raising hook is caught, logged, and does not propagate."""

    def bad_hook(doc_id, collection, content):
        raise RuntimeError("hook failure")

    runtime.hooks.register_single(bad_hook)
    # Must not raise.
    runtime.hooks.fire_single("doc-1", "test__coll", "content")


def test_fire_post_store_hooks_persists_failure_to_t2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime,
) -> None:
    """GH #251: hook failures are persisted to T2 ``hook_failures`` for
    ``nx taxonomy status`` to surface. The fire path delegates failure
    recording through ``mcp_infra._record_hook_failure``, which opens a
    T2 via ``t2_ctx``; monkeypatching the ``t2_ctx`` binding intercepts
    the persistence call without needing a live daemon."""
    import sqlite3

    import nexus.mcp_infra as mod
    from nexus.db.migrations import migrate_hook_failures

    db_path = tmp_path / "hook_failures.db"
    T2Database(db_path).close()

    conn = sqlite3.connect(str(db_path))
    migrate_hook_failures(conn)
    conn.close()

    monkeypatch.setattr(mod, "t2_ctx", lambda: T2Database(db_path))

    def bad_hook(doc_id, collection, content):
        raise RuntimeError("simulated centroid failure")

    runtime.hooks.register_single(bad_hook)
    runtime.hooks.fire_single("doc-xyz", "knowledge__thing", "content")

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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime,
) -> None:
    """GH #251: when ``hook_failures`` is absent (pre-4.9.10 DB) the
    fire path must NOT raise. The store contract is "best effort" all
    the way down."""
    import nexus.mcp_infra as mod

    db_path = tmp_path / "no_hook_table.db"
    T2Database(db_path).close()
    monkeypatch.setattr(mod, "t2_ctx", lambda: T2Database(db_path))

    def bad_hook(doc_id, collection, content):
        raise RuntimeError("primary failure")

    runtime.hooks.register_single(bad_hook)
    runtime.hooks.fire_single("d", "c", "content")


def test_fire_post_store_hooks_persist_failure_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime,
) -> None:
    """GH #251: even if ``t2_ctx`` raises (T2 offline) the hook fire
    path still returns. The store contract holds."""
    import nexus.mcp_infra as mod

    def _broken_ctx():
        raise RuntimeError("t2 offline")

    monkeypatch.setattr(mod, "t2_ctx", _broken_ctx)

    def bad_hook(doc_id, collection, content):
        raise RuntimeError("original failure")

    runtime.hooks.register_single(bad_hook)
    runtime.hooks.fire_single("d", "c", "content")


# ── Batch hook chain (RDR-095, nexus-wxcb) ───────────────────────────────────


def test_register_post_store_batch_hook_appends(runtime) -> None:
    """``register_batch`` appends to the runtime's batch list."""

    def probe(doc_ids, collection, contents, embeddings, metadatas):
        return None

    assert runtime.hooks._batch == []
    runtime.hooks.register_batch(probe)
    assert runtime.hooks._batch == [probe]


def test_fire_post_store_batch_hooks_invokes_registered(runtime) -> None:
    """``fire_batch`` forwards every parameter unchanged to each hook."""
    seen: list[tuple] = []

    def hook_a(doc_ids, collection, contents, embeddings, metadatas):
        seen.append((
            "a", tuple(doc_ids), collection,
            tuple(contents), embeddings, metadatas,
        ))

    def hook_b(doc_ids, collection, contents, embeddings, metadatas):
        seen.append((
            "b", tuple(doc_ids), collection,
            tuple(contents), embeddings, metadatas,
        ))

    runtime.hooks.register_batch(hook_a)
    runtime.hooks.register_batch(hook_b)

    embeddings = [[0.1, 0.2], [0.3, 0.4]]
    metadatas = [{"k": "v1"}, {"k": "v2"}]
    runtime.hooks.fire_batch(
        ["d1", "d2"], "code__nexus", ["c1", "c2"], embeddings, metadatas,
    )

    assert seen == [
        ("a", ("d1", "d2"), "code__nexus", ("c1", "c2"), embeddings, metadatas),
        ("b", ("d1", "d2"), "code__nexus", ("c1", "c2"), embeddings, metadatas),
    ]


def test_fire_post_store_batch_hooks_empty_doc_ids_early_return(
    runtime,
) -> None:
    """An empty ``doc_ids`` list returns early without invoking any
    registered batch hook."""
    calls: list = []
    runtime.hooks.register_batch(
        lambda doc_ids, collection, contents, embeddings, metadatas: calls.append(1)
    )
    runtime.hooks.fire_batch([], "x", [], None, None)
    assert calls == []


def test_fire_post_store_batch_hooks_isolation(
    tmp_path: Path, monkeypatch, runtime,
) -> None:
    """A raising batch hook must not block subsequent hooks. The
    failure is captured + persisted to T2 ``hook_failures`` with the
    batch column shape (``is_batch=1``, ``batch_doc_ids`` JSON)."""
    import sqlite3

    import nexus.mcp_infra as mod
    from nexus.db.migrations import (
        migrate_hook_failures,
        migrate_hook_failures_batch_columns,
    )

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

    runtime.hooks.register_batch(raising_probe)
    runtime.hooks.register_batch(survivor)

    runtime.hooks.fire_batch(
        ["doc-1", "doc-2", "doc-3"], "code__nexus",
        ["c1", "c2", "c3"], None, None,
    )

    assert second_calls == [("doc-1", "doc-2", "doc-3")]

    with T2Database(db_path) as db:
        row = db.taxonomy.conn.execute(
            "SELECT doc_id, collection, hook_name, error, batch_doc_ids, is_batch "
            "FROM hook_failures"
        ).fetchone()

    assert row is not None
    doc_id, collection, hook_name, error, batch_doc_ids, is_batch = row
    assert doc_id == "doc-1"
    assert collection == "code__nexus"
    assert hook_name == "raising_probe"
    assert "simulated batch failure" in error
    assert batch_doc_ids == '["doc-1", "doc-2", "doc-3"]'
    assert is_batch == 1


def test_fire_post_store_batch_hooks_partial_commit_failure_mode(
    tmp_path: Path, monkeypatch, runtime,
) -> None:
    """A batch hook may commit sub-step A then raise on sub-step B. The
    framework writes a single ``hook_failures`` row capturing the full
    doc_id list and exception text; per-sub-step capture is hook-
    internal, not framework-level."""
    import sqlite3

    import nexus.mcp_infra as mod
    from nexus.db.migrations import (
        migrate_hook_failures,
        migrate_hook_failures_batch_columns,
    )

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
        raise RuntimeError("cross_collection_projection_failed")

    runtime.hooks.register_batch(two_step_hook)

    runtime.hooks.fire_batch(
        ["doc-a", "doc-b"], "knowledge__delos",
        ["c1", "c2"], None, None,
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
    tmp_path: Path, monkeypatch, runtime,
) -> None:
    """If the new ``batch_doc_ids``/``is_batch`` columns are not yet on
    the live DB (mixed-version operator scenario), the framework falls
    back to a scalar-only insert rather than crashing."""
    import sqlite3
    import threading

    import nexus.mcp_infra as mod
    from nexus.db.migrations import migrate_hook_failures
    from nexus.db.t2.memory_store import MemoryStore

    db_path = tmp_path / "pre_migration.db"
    raw = sqlite3.connect(str(db_path), isolation_level=None)
    migrate_hook_failures(raw)

    cols = {
        r[1] for r in raw.execute("PRAGMA table_info(hook_failures)").fetchall()
    }
    assert "batch_doc_ids" not in cols, (
        "pre-condition: hook_failures must lack batch_doc_ids before "
        "the test runs"
    )

    class _FakeTaxonomy:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn
            self._lock = threading.RLock()

    def _fake_memory(conn: sqlite3.Connection) -> MemoryStore:
        store = MemoryStore.__new__(MemoryStore)
        store.conn = conn
        store._lock = threading.Lock()
        return store

    class _FakeT2:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.taxonomy = _FakeTaxonomy(conn)
            self.memory = _fake_memory(conn)

        def __enter__(self) -> "_FakeT2":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(mod, "t2_ctx", lambda: _FakeT2(raw))

    def raising(doc_ids, collection, contents, embeddings, metadatas):
        raise RuntimeError("kaboom")

    runtime.hooks.register_batch(raising)
    runtime.hooks.fire_batch(
        ["d1", "d2"], "code__nexus", ["c1", "c2"], None, None,
    )

    rows = raw.execute(
        "SELECT doc_id, hook_name, error FROM hook_failures"
    ).fetchall()
    raw.close()

    assert len(rows) == 1
    assert rows[0][0] == "d1"
    assert rows[0][1] == "raising"
    assert "kaboom" in rows[0][2]


def test_record_batch_hook_failure_non_schema_operational_error_propagates(
    tmp_path: Path, monkeypatch,
) -> None:
    """A transient ``OperationalError`` that is NOT a schema-missing-column
    error must NOT silently fall through to the scalar-only insert path.

    The outer best-effort wrapper on ``_record_batch_hook_failure``
    swallows the error so ingest is unaffected, but the scalar fallback
    row must NOT be written for unrelated lock/IO failures.
    """
    import sqlite3

    import nexus.mcp_infra as mod
    from nexus.db.migrations import (
        migrate_hook_failures,
        migrate_hook_failures_batch_columns,
    )
    from nexus.mcp_infra import _record_batch_hook_failure

    db_path = tmp_path / "lock_propagate.db"
    T2Database(db_path).close()
    conn = sqlite3.connect(str(db_path))
    migrate_hook_failures(conn)
    migrate_hook_failures_batch_columns(conn)
    conn.close()

    class _LockingT2:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        class _Taxonomy:
            class _ConnRaisesLock:
                def execute(self, *a, **kw):
                    raise sqlite3.OperationalError("database is locked")

                def commit(self):
                    pass

            conn = _ConnRaisesLock()

            class _Lock:
                def __enter__(self):
                    return None

                def __exit__(self, *a):
                    return False

            _lock = _Lock()

        taxonomy = _Taxonomy()

    monkeypatch.setattr(mod, "t2_ctx", lambda: _LockingT2())

    _record_batch_hook_failure(
        doc_ids=["d1", "d2"],
        collection="code__nexus",
        hook_name="probe",
        error="boom",
    )

    real = sqlite3.connect(str(db_path))
    rows = real.execute("SELECT COUNT(*) FROM hook_failures").fetchone()
    real.close()
    assert rows == (0,)


def test_fire_post_store_batch_hooks_persist_failure_is_best_effort(
    monkeypatch, runtime,
) -> None:
    """If the persist path itself raises (T2 offline), the fire path
    still returns and ingest continues."""
    import nexus.mcp_infra as mod

    monkeypatch.setattr(
        mod, "t2_ctx",
        lambda: (_ for _ in ()).throw(RuntimeError("t2 offline")),
    )

    def raising(doc_ids, collection, contents, embeddings, metadatas):
        raise RuntimeError("primary failure")

    runtime.hooks.register_batch(raising)
    runtime.hooks.fire_batch(["d1"], "c", ["x"], None, None)


# ── Taxonomy batch hook fallback (MCP path with embeddings=None) ─────────────
#
# End-to-end taxonomy assignment behaviour (centroid lookup, cross-collection
# projection) is covered in tests/test_taxonomy.py via the underlying
# assign_batch path. The tests here cover only the new ``embeddings=None``
# fallback that the MCP store_put path relies on
# (``taxonomy_assign_batch_hook`` previously had no embedding-fetch path;
# the legacy single-doc shim handled it via ``taxonomy_assign_hook``).


def test_fetch_or_embed_returns_t3_embedding_when_present(
    monkeypatch,
) -> None:
    """``_fetch_or_embed`` returns the doc's existing T3 embedding
    without hitting the local-MiniLM fallback when the row is present."""
    import nexus.mcp_infra as mod
    from nexus.mcp_infra import _fetch_or_embed

    stored_emb = [0.1] * 384
    stored_emb[0] = 0.9

    class _Coll:
        def get(self, ids, include):
            return {"ids": list(ids), "embeddings": [stored_emb]}

    class _Client:
        def get_collection(self, name, embedding_function):
            return _Coll()

    class _T3Stub:
        _client = _Client()

    monkeypatch.setattr(mod, "get_t3", lambda: _T3Stub())

    result = _fetch_or_embed(["doc-1"], "fetch__coll", ["payload"])
    assert result is not None
    assert len(result) == 1
    assert result[0][0] == 0.9


def test_fetch_or_embed_falls_back_to_local_minilm(
    monkeypatch,
) -> None:
    """When T3 returns no embedding for a doc id, ``_fetch_or_embed``
    falls back to local MiniLM embedding of the supplied content. Keeps
    MCP store_put working when the just-upserted row is not yet
    retrievable (race condition with t3 visibility)."""
    import nexus.mcp_infra as mod
    from nexus.mcp_infra import _fetch_or_embed

    class _EmptyColl:
        def get(self, ids, include):
            return {"ids": [], "embeddings": []}

    class _Client:
        def get_collection(self, name, embedding_function):
            return _EmptyColl()

    class _T3Stub:
        _client = _Client()

    monkeypatch.setattr(mod, "get_t3", lambda: _T3Stub())

    result = _fetch_or_embed(["doc-x"], "fetch__coll", ["hello world"])
    assert result is not None
    assert len(result) == 1
    assert len(result[0]) == 384  # MiniLM dim


def test_fetch_or_embed_returns_none_when_no_t3_no_content(
    monkeypatch,
) -> None:
    """If T3 fetch raises AND contents is empty, the fallback has no
    input; the function returns None and the caller no-ops cleanly."""
    import nexus.mcp_infra as mod
    from nexus.mcp_infra import _fetch_or_embed

    class _T3Boom:
        @property
        def _client(self):
            raise RuntimeError("t3 unreachable")

    monkeypatch.setattr(mod, "get_t3", lambda: _T3Boom())

    result = _fetch_or_embed(["doc-y"], "any__coll", [])
    assert result is None
