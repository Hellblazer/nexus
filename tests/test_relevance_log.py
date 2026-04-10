# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for RDR-061 E2: Retrieval Feedback Loop.

Verifies:
- T2 relevance_log table schema and migration
- T2.log_relevance() and get_relevance_log() methods
- Search trace cache in mcp_infra (record/get/clear/TTL)
- search() → store_put() correlation flow
- search() → catalog_link() correlation flow (collection match filter)
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest

from nexus.db.t1 import T1Database
from nexus.db.t2 import T2Database
from nexus.mcp_infra import (
    clear_search_traces,
    get_recent_search_traces,
    record_search_trace,
    reset_singletons,
)


# ── T2 relevance_log schema + methods ────────────────────────────────────────


@pytest.fixture()
def t2():
    with tempfile.TemporaryDirectory() as d:
        db = T2Database(Path(d) / "t2.db")
        yield db


def test_relevance_log_table_exists(t2):
    """Migration creates the relevance_log table with correct columns."""
    cols = {r[1] for r in t2.conn.execute("PRAGMA table_info(relevance_log)").fetchall()}
    assert cols == {"id", "query", "chunk_id", "collection", "action", "session_id", "timestamp"}


def test_log_relevance_inserts_row(t2):
    """log_relevance() inserts a row and returns its id."""
    row_id = t2.log_relevance(
        query="vector search",
        chunk_id="chunk-abc",
        action="stored",
        session_id="sess-1",
        collection="knowledge__notes",
    )
    assert row_id is not None
    rows = t2.conn.execute("SELECT query, chunk_id, action FROM relevance_log").fetchall()
    assert rows == [("vector search", "chunk-abc", "stored")]


def test_get_relevance_log_no_filter(t2):
    """get_relevance_log() returns all rows when no filter given."""
    t2.log_relevance("q1", "c1", "stored")
    t2.log_relevance("q2", "c2", "linked")
    rows = t2.get_relevance_log()
    assert len(rows) == 2
    assert {r["action"] for r in rows} == {"stored", "linked"}


def test_get_relevance_log_filter_by_query(t2):
    """get_relevance_log() filters by query."""
    t2.log_relevance("q1", "c1", "stored")
    t2.log_relevance("q2", "c2", "linked")
    rows = t2.get_relevance_log(query="q1")
    assert len(rows) == 1
    assert rows[0]["chunk_id"] == "c1"


def test_get_relevance_log_filter_by_chunk(t2):
    t2.log_relevance("q1", "c1", "stored")
    t2.log_relevance("q2", "c1", "linked")
    rows = t2.get_relevance_log(chunk_id="c1")
    assert len(rows) == 2


def test_get_relevance_log_filter_by_action(t2):
    t2.log_relevance("q1", "c1", "stored")
    t2.log_relevance("q2", "c2", "linked")
    rows = t2.get_relevance_log(action="stored")
    assert len(rows) == 1
    assert rows[0]["query"] == "q1"


def test_get_relevance_log_ordered_most_recent_first(t2):
    """get_relevance_log() returns rows ordered most-recent first."""
    t2.log_relevance("q1", "c1", "stored")
    t2.log_relevance("q2", "c2", "stored")
    rows = t2.get_relevance_log()
    # Most recent first: q2 before q1
    assert rows[0]["query"] == "q2"
    assert rows[1]["query"] == "q1"


# ── Search trace cache (mcp_infra) ───────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_traces():
    clear_search_traces()
    yield
    clear_search_traces()


def test_record_and_get_search_trace():
    """record_search_trace stores, get_recent_search_traces retrieves."""
    record_search_trace("sess-1", "vector search", [("c1", "knowledge__a"), ("c2", "knowledge__a")])
    traces = get_recent_search_traces("sess-1")
    assert len(traces) == 1
    assert traces[0]["query"] == "vector search"
    assert traces[0]["chunks"] == [("c1", "knowledge__a"), ("c2", "knowledge__a")]


def test_search_trace_session_isolation():
    """Traces are keyed by session_id."""
    record_search_trace("sess-1", "q1", [("c1", "col")])
    record_search_trace("sess-2", "q2", [("c2", "col")])
    assert len(get_recent_search_traces("sess-1")) == 1
    assert len(get_recent_search_traces("sess-2")) == 1
    assert get_recent_search_traces("sess-1")[0]["query"] == "q1"


def test_search_trace_empty_session_id_noop():
    """Empty session_id is a no-op for both record and get."""
    record_search_trace("", "q", [("c", "col")])
    assert get_recent_search_traces("") == []


def test_search_trace_empty_chunks_noop():
    """Empty chunks list is a no-op."""
    record_search_trace("sess-1", "q", [])
    assert get_recent_search_traces("sess-1") == []


def test_search_trace_max_per_session():
    """Trace buffer trims to _SEARCH_TRACE_MAX_PER_SESSION."""
    from nexus.mcp_infra import _SEARCH_TRACE_MAX_PER_SESSION
    for i in range(_SEARCH_TRACE_MAX_PER_SESSION + 5):
        record_search_trace("sess-1", f"q{i}", [(f"c{i}", "col")])
    traces = get_recent_search_traces("sess-1")
    assert len(traces) == _SEARCH_TRACE_MAX_PER_SESSION
    # Most recent queries preserved
    assert traces[-1]["query"] == f"q{_SEARCH_TRACE_MAX_PER_SESSION + 4}"


def test_search_trace_ttl_expiry():
    """Traces older than TTL are filtered out."""
    # Record a trace, then manually backdate it
    record_search_trace("sess-1", "old", [("c1", "col")])
    from nexus.mcp_infra import _search_traces, _SEARCH_TRACE_TTL_SECONDS
    _search_traces["sess-1"][0]["timestamp"] -= _SEARCH_TRACE_TTL_SECONDS + 10
    assert get_recent_search_traces("sess-1") == []


def test_clear_search_traces():
    """clear_search_traces() empties the cache."""
    record_search_trace("sess-1", "q", [("c", "col")])
    clear_search_traces()
    assert get_recent_search_traces("sess-1") == []


# ── End-to-end: search → store_put correlation ───────────────────────────────


@pytest.fixture()
def t1():
    reset_singletons()
    client = chromadb.EphemeralClient()
    db = T1Database(session_id="test-session-e2", client=client)
    from nexus.mcp_infra import inject_t1
    inject_t1(db)
    yield db
    reset_singletons()


def test_store_put_logs_relevance_for_recent_searches(t1, tmp_path, monkeypatch):
    """After a search, calling store_put logs (query, chunk_id, 'stored') triples."""
    from nexus.mcp.core import store_put

    # Point T2 context at a temp DB
    t2_path = tmp_path / "t2.db"
    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: T2Database(t2_path))

    # Mock T3 so store_put doesn't hit real ChromaDB
    mock_t3 = MagicMock()
    mock_t3.put.return_value = "new-doc-id"
    from nexus.mcp_infra import inject_t3
    inject_t3(mock_t3)

    # Simulate a prior search: record a trace
    record_search_trace(
        "test-session-e2",
        "how does vector search work",
        [("chunk-1", "knowledge__ml"), ("chunk-2", "knowledge__ml")],
    )

    # Call store_put — should log relevance for both chunks
    result = store_put(content="some notes about vector search", collection="knowledge")
    assert "Stored" in result

    # Verify relevance_log has entries
    with T2Database(t2_path) as db:
        rows = db.get_relevance_log(action="stored")
    assert len(rows) == 2
    queries = {r["query"] for r in rows}
    chunks = {r["chunk_id"] for r in rows}
    assert queries == {"how does vector search work"}
    assert chunks == {"chunk-1", "chunk-2"}


def test_store_put_without_search_trace_no_log(t1, tmp_path, monkeypatch):
    """store_put with no recent searches doesn't log anything."""
    from nexus.mcp.core import store_put

    t2_path = tmp_path / "t2.db"
    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: T2Database(t2_path))

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "new-doc-id"
    from nexus.mcp_infra import inject_t3
    inject_t3(mock_t3)

    # No search trace — direct store_put
    clear_search_traces()
    result = store_put(content="random notes", collection="knowledge")
    assert "Stored" in result

    with T2Database(t2_path) as db:
        rows = db.get_relevance_log()
    assert rows == []


def test_store_put_only_logs_latest_trace(t1, tmp_path, monkeypatch):
    """Multiple search traces — store_put only correlates with the newest one."""
    from nexus.mcp.core import store_put

    t2_path = tmp_path / "t2.db"
    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: T2Database(t2_path))

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "new-doc-id"
    from nexus.mcp_infra import inject_t3
    inject_t3(mock_t3)

    record_search_trace("test-session-e2", "old query", [("c-old", "knowledge__a")])
    record_search_trace("test-session-e2", "newer query", [("c-new-1", "knowledge__a"), ("c-new-2", "knowledge__a")])

    store_put(content="notes", collection="knowledge")

    with T2Database(t2_path) as db:
        rows = db.get_relevance_log()
    # Only 2 rows (from the latest trace), not 3
    assert len(rows) == 2
    assert {r["query"] for r in rows} == {"newer query"}
    assert {r["chunk_id"] for r in rows} == {"c-new-1", "c-new-2"}


def test_get_relevance_log_filter_by_session(t2):
    """get_relevance_log() filters by session_id."""
    t2.log_relevance("q1", "c1", "stored", session_id="sess-a")
    t2.log_relevance("q2", "c2", "stored", session_id="sess-b")
    rows = t2.get_relevance_log(session_id="sess-a")
    assert len(rows) == 1
    assert rows[0]["query"] == "q1"


def test_log_relevance_batch(t2):
    """log_relevance_batch inserts multiple rows in one transaction."""
    rows = [
        ("q1", "c1", "knowledge__a", "stored", "sess-1"),
        ("q1", "c2", "knowledge__a", "stored", "sess-1"),
        ("q2", "c3", "knowledge__b", "linked", "sess-1"),
    ]
    count = t2.log_relevance_batch(rows)
    assert count == 3
    all_rows = t2.get_relevance_log()
    assert len(all_rows) == 3


def test_log_relevance_batch_empty_is_noop(t2):
    """Empty batch returns 0 and writes nothing."""
    assert t2.log_relevance_batch([]) == 0
    assert t2.get_relevance_log() == []


def test_search_trace_cache_evicts_empty_session_keys():
    """When all traces expire, the session key is removed from the cache."""
    from nexus.mcp_infra import _search_traces, _SEARCH_TRACE_TTL_SECONDS

    record_search_trace("sess-ephemeral", "q", [("c", "col")])
    assert "sess-ephemeral" in _search_traces

    # Backdate the trace beyond TTL
    _search_traces["sess-ephemeral"][0]["timestamp"] -= _SEARCH_TRACE_TTL_SECONDS + 10

    # Read should evict the key
    traces = get_recent_search_traces("sess-ephemeral")
    assert traces == []
    assert "sess-ephemeral" not in _search_traces


def test_t1_public_session_id_property(t1):
    """T1Database exposes session_id as a public property."""
    assert t1.session_id == "test-session-e2"


def test_catalog_link_logs_relevance_with_collection_match(t1, tmp_path, monkeypatch):
    """catalog_link logs relevance when target collection matches a recent search chunk."""
    from nexus.catalog import Catalog
    from nexus.catalog.tumbler import Tumbler
    from nexus.mcp.catalog import catalog_link
    from nexus.mcp_infra import inject_catalog

    # Set up a catalog with two documents in the same collection
    catalog_dir = tmp_path / "catalog"
    cat = Catalog.init(catalog_dir)
    owner = cat.register_owner("test", "proj")
    doc_a = cat.register(owner, "source", content_type="knowledge",
                         physical_collection="knowledge__ml")
    doc_b = cat.register(owner, "target", content_type="knowledge",
                         physical_collection="knowledge__ml")
    inject_catalog(cat)

    t2_path = tmp_path / "t2.db"
    monkeypatch.setattr("nexus.mcp.catalog._t2_ctx", lambda: T2Database(t2_path))

    # Record a search trace with a chunk in knowledge__ml
    record_search_trace(
        "test-session-e2",
        "ml concepts",
        [("chunk-1", "knowledge__ml"), ("chunk-2", "knowledge__other")],
    )

    # Create a link — target is in knowledge__ml, matches chunk-1
    result = catalog_link(
        from_tumbler=str(doc_a),
        to_tumbler=str(doc_b),
        link_type="cites",
    )
    assert "created" in result

    # Only chunk-1 should be logged (collection match filter)
    with T2Database(t2_path) as db:
        rows = db.get_relevance_log(action="linked")
    assert len(rows) == 1
    assert rows[0]["chunk_id"] == "chunk-1"
    assert rows[0]["query"] == "ml concepts"


def test_catalog_link_no_log_when_collection_mismatch(t1, tmp_path, monkeypatch):
    """catalog_link does NOT log when no trace chunks match the target collection."""
    from nexus.catalog import Catalog
    from nexus.mcp.catalog import catalog_link
    from nexus.mcp_infra import inject_catalog

    catalog_dir = tmp_path / "catalog"
    cat = Catalog.init(catalog_dir)
    owner = cat.register_owner("test", "proj")
    doc_a = cat.register(owner, "src", content_type="knowledge",
                         physical_collection="knowledge__ml")
    doc_b = cat.register(owner, "dst", content_type="knowledge",
                         physical_collection="knowledge__ml")
    inject_catalog(cat)

    t2_path = tmp_path / "t2.db"
    monkeypatch.setattr("nexus.mcp.catalog._t2_ctx", lambda: T2Database(t2_path))

    # Search was in a different collection
    record_search_trace(
        "test-session-e2",
        "unrelated",
        [("chunk-x", "knowledge__other")],
    )

    catalog_link(from_tumbler=str(doc_a), to_tumbler=str(doc_b), link_type="cites")

    with T2Database(t2_path) as db:
        rows = db.get_relevance_log()
    assert rows == []
