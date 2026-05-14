# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for MemoryStore.record_hook_failure (RDR-112 P0.2 / nexus-xmu5).

The method is the domain owner for the ``hook_failures`` table; three
``mcp_infra`` raw-INSERT reach-throughs collapse into one call. Schema
fallback is internal so the daemon-mode swap in Phase 1 sees a single
RPC, not three subtly different ones.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


@pytest.fixture
def mem_db(tmp_path: Path):
    db = T2Database(tmp_path / "hf.db")
    try:
        yield db
    finally:
        db.close()


def _hook_failure_rows(db: T2Database) -> list[tuple]:
    return db.memory.conn.execute(
        "SELECT doc_id, collection, hook_name, error, chain, is_batch, batch_doc_ids "
        "FROM hook_failures ORDER BY id"
    ).fetchall()


# -- Happy paths -------------------------------------------------------------


def test_record_hook_failure_single(mem_db: T2Database) -> None:
    mem_db.memory.record_hook_failure(
        doc_id="doc-1", collection="code__x", hook_name="h1", error="boom",
    )

    rows = _hook_failure_rows(mem_db)
    assert len(rows) == 1
    doc_id, coll, hook, err, chain, is_batch, batch_ids = rows[0]
    assert (doc_id, coll, hook, err) == ("doc-1", "code__x", "h1", "boom")
    assert chain == "single"
    assert (is_batch or 0) == 0
    assert batch_ids is None


def test_record_hook_failure_document(mem_db: T2Database) -> None:
    mem_db.memory.record_hook_failure(
        doc_id="/abs/x.md", collection="knowledge__d", hook_name="h2",
        error="splat", chain="document",
    )

    rows = _hook_failure_rows(mem_db)
    assert len(rows) == 1
    assert rows[0][0] == "/abs/x.md"
    assert rows[0][4] == "document"


def test_record_hook_failure_batch_sets_marker_and_payload(mem_db: T2Database) -> None:
    mem_db.memory.record_hook_failure(
        doc_id="rep", collection="docs__x", hook_name="h3", error="oops",
        chain="batch", batch_doc_ids=["a", "b", "c"],
    )

    rows = _hook_failure_rows(mem_db)
    assert len(rows) == 1
    doc_id, coll, hook, err, chain, is_batch, batch_ids = rows[0]
    assert chain == "batch"
    assert is_batch == 1
    assert json.loads(batch_ids) == ["a", "b", "c"]


def test_record_hook_failure_truncates_error_to_cap(mem_db: T2Database) -> None:
    from nexus.db.t2.memory_store import MemoryStore

    mem_db.memory.record_hook_failure(
        doc_id="d", collection="c", hook_name="h",
        error="x" * (MemoryStore.HOOK_FAILURE_ERROR_MAX + 3000),
    )
    rows = _hook_failure_rows(mem_db)
    assert len(rows[0][3]) == MemoryStore.HOOK_FAILURE_ERROR_MAX


def test_record_hook_failure_invalid_chain_raises(mem_db: T2Database) -> None:
    """An unrecognised ``chain`` value is a contract bug, not silently stored.

    ``HookFailureChain = Literal[...]`` is a static-checker hint only;
    the runtime guard catches typos a misbehaving caller might smuggle
    in (test stub, future RPC client, raw Python).
    """
    with pytest.raises(ValueError, match="single.*batch.*document"):
        mem_db.memory.record_hook_failure(
            doc_id="d", collection="c", hook_name="h", error="e",
            chain="singleton",  # type: ignore[arg-type]
        )


def test_record_hook_failure_batch_without_doc_ids_raises(mem_db: T2Database) -> None:
    """Explicit ``chain='batch'`` with no ``batch_doc_ids`` is a contract bug."""
    with pytest.raises(ValueError, match="non-empty"):
        mem_db.memory.record_hook_failure(
            doc_id="rep", collection="c", hook_name="h", error="e",
            chain="batch",
        )


def test_record_hook_failure_batch_empty_doc_ids_raises(mem_db: T2Database) -> None:
    """Explicit ``chain='batch'`` with empty ``batch_doc_ids`` is also rejected.

    Catches a row with ``is_batch=1`` and no recoverable identity — a
    daemon-RPC client passing ``[]`` (vs ``None``) would otherwise slip
    through.
    """
    with pytest.raises(ValueError, match="non-empty"):
        mem_db.memory.record_hook_failure(
            doc_id="rep", collection="c", hook_name="h", error="e",
            chain="batch", batch_doc_ids=[],
        )


# -- Schema fallback (pre-4.14.2 + pre-4.14.1) -------------------------------


def _build_pre_4_14_2_db(path: Path) -> sqlite3.Connection:
    """Construct a hook_failures schema at the 4.14.1 level (no chain column)."""
    from nexus.db.migrations import (
        migrate_hook_failures,
        migrate_hook_failures_batch_columns,
    )

    raw = sqlite3.connect(str(path), isolation_level=None)
    migrate_hook_failures(raw)
    migrate_hook_failures_batch_columns(raw)
    return raw


def _build_pre_4_14_1_db(path: Path) -> sqlite3.Connection:
    """Construct a hook_failures schema at the 4.14.0 level (scalar-only)."""
    from nexus.db.migrations import migrate_hook_failures

    raw = sqlite3.connect(str(path), isolation_level=None)
    migrate_hook_failures(raw)
    return raw


def test_record_hook_failure_falls_back_when_chain_column_absent(tmp_path: Path) -> None:
    """Pre-4.14.2 schema: chain column missing — must still persist."""
    from nexus.db.t2.memory_store import MemoryStore

    db_path = tmp_path / "pre_4_14_2.db"
    raw = _build_pre_4_14_2_db(db_path)
    raw.close()

    store = MemoryStore.__new__(MemoryStore)
    store.conn = sqlite3.connect(str(db_path), check_same_thread=False)
    store._lock = threading.Lock()

    try:
        store.record_hook_failure(
            doc_id="d", collection="c", hook_name="h", error="e",
            chain="document",
        )
        rows = store.conn.execute(
            "SELECT doc_id, collection, hook_name, error FROM hook_failures"
        ).fetchall()
    finally:
        store.conn.close()

    assert rows == [("d", "c", "h", "e")]


def test_record_hook_failure_falls_back_when_batch_columns_absent(tmp_path: Path) -> None:
    """Pre-4.14.1 schema: chain AND batch columns missing — must still persist."""
    from nexus.db.t2.memory_store import MemoryStore

    db_path = tmp_path / "pre_4_14_1.db"
    raw = _build_pre_4_14_1_db(db_path)
    raw.close()

    store = MemoryStore.__new__(MemoryStore)
    store.conn = sqlite3.connect(str(db_path), check_same_thread=False)
    store._lock = threading.Lock()

    try:
        store.record_hook_failure(
            doc_id="rep", collection="c", hook_name="h", error="e",
            chain="batch", batch_doc_ids=["a", "b"],
        )
        rows = store.conn.execute(
            "SELECT doc_id, collection, hook_name, error FROM hook_failures"
        ).fetchall()
    finally:
        store.conn.close()

    assert rows == [("rep", "c", "h", "e")]


# -- Concurrency -------------------------------------------------------------


def test_record_hook_failure_concurrent_writes(mem_db: T2Database) -> None:
    """Many threads each record one row; all rows land, no exceptions."""
    barrier = threading.Barrier(8)

    def worker(i: int):
        barrier.wait()
        mem_db.memory.record_hook_failure(
            doc_id=f"d{i}", collection="c", hook_name="h", error=f"err-{i}",
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    count = mem_db.memory.conn.execute(
        "SELECT COUNT(*) FROM hook_failures"
    ).fetchone()[0]
    assert count == 8
