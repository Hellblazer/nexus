# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for nexus MCP server tools.

All tests use injected clients — no API keys or network required.
- T1: chromadb.EphemeralClient (bundled ONNX MiniLM)
- T2: temp-file SQLite via T2Database
- T3: chromadb.EphemeralClient with DefaultEmbeddingFunction override
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import chromadb
import pytest

from nexus.db.t1 import T1Database
from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database
from nexus.mcp_server import (
    _inject_t1,
    _inject_t3,
    _reset_singletons,
    collection_info,
    collection_list,
    collection_verify,
    memory_get,
    memory_put,
    memory_search,
    plan_save,
    plan_search,
    scratch,
    scratch_manage,
    search,
    store_list,
    store_put,
)


@pytest.fixture(autouse=True)
def _reset():
    """Reset singletons before and after each test."""
    _reset_singletons()
    yield
    _reset_singletons()


@pytest.fixture()
def t1():
    """Ephemeral T1Database for scratch tests."""
    client = chromadb.EphemeralClient()
    db = T1Database(session_id="test-session", client=client)
    _inject_t1(db)
    return db


@pytest.fixture()
def t1_isolated():
    """Ephemeral T1Database simulating EphemeralClient fallback (isolated mode)."""
    client = chromadb.EphemeralClient()
    db = T1Database(session_id="test-session-iso", client=client)
    _inject_t1(db, isolated=True)
    return db


@pytest.fixture()
def t2_path():
    """Temp file path for T2Database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield Path(f.name)


@pytest.fixture()
def t3():
    """Ephemeral T3Database (no API keys)."""
    client = chromadb.EphemeralClient()
    ef = chromadb.utils.embedding_functions.DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    _inject_t3(db)
    return db


@pytest.fixture(autouse=True)
def _patch_t2(t2_path, monkeypatch):
    """Redirect T2 to use temp database path."""
    import nexus.mcp_server as mod
    monkeypatch.setattr(mod, "_t2_ctx", lambda: T2Database(t2_path))


# ── Search tests ──────────────────────────────────────────────────────────────

def test_search_returns_results(t3):
    """search tool returns formatted results from T3."""
    t3.put(collection="knowledge__test", content="chromadb vector database", title="doc1")
    result = search(query="vector database", corpus="knowledge__test", n=5)
    assert not result.startswith("Error:"), f"search returned error: {result}"
    assert "vector database" in result.lower() or "doc1" in result


def test_search_no_results(t3):
    """search tool returns 'No results.' when nothing matches."""
    result = search(query="nonexistent topic", corpus="knowledge__empty")
    assert not result.startswith("Error:"), f"search returned error: {result}"
    # Either "No collections" or "No results."
    assert "no" in result.lower()


# ── Store tests ───────────────────────────────────────────────────────────────

def test_store_put(t3):
    """store_put returns confirmation with doc ID."""
    result = store_put(content="test content", collection="knowledge", title="test-doc")
    assert "Stored:" in result
    assert "knowledge__knowledge" in result


def test_store_list(t3):
    """store_list returns listing after put."""
    store_put(content="listed entry", collection="knowledge", title="list-test")
    result = store_list(collection="knowledge")
    assert not result.startswith("Error:"), f"store_list returned error: {result}"
    assert "entries" in result.lower() or "list-test" in result


# ── Memory tests ──────────────────────────────────────────────────────────────

def test_memory_put(t2_path):
    """memory_put stores an entry and returns confirmation."""
    result = memory_put(content="test memory", project="testproj", title="finding.md")
    assert "Stored:" in result
    assert "testproj/finding.md" in result


def test_memory_get_by_title(t2_path):
    """memory_get retrieves by project+title."""
    memory_put(content="retrievable content", project="testproj", title="doc.md")
    result = memory_get(project="testproj", title="doc.md")
    assert "retrievable content" in result


def test_memory_get_empty_title_lists(t2_path):
    """memory_get with empty title lists project entries."""
    memory_put(content="entry1", project="listproj", title="a.md")
    memory_put(content="entry2", project="listproj", title="b.md")
    result = memory_get(project="listproj", title="")
    assert "2 entries" in result
    assert "a.md" in result
    assert "b.md" in result


def test_memory_search(t2_path):
    """memory_search uses FTS5 to find entries."""
    memory_put(content="chromadb vector embeddings", project="testproj", title="vectors.md")
    result = memory_search(query="chromadb")
    assert "vector" in result.lower()


# ── Scratch tests ─────────────────────────────────────────────────────────────

def test_scratch_put(t1):
    """scratch put action stores content and returns ID."""
    result = scratch(action="put", content="scratch note")
    assert "Stored:" in result
    # Extract the ID
    doc_id = result.split("Stored:")[1].strip()
    assert len(doc_id) > 0


def test_scratch_search(t1):
    """scratch search action returns results."""
    scratch(action="put", content="semantic search hypothesis")
    result = scratch(action="search", query="semantic search")
    assert "semantic" in result.lower() or "hypothesis" in result.lower()


def test_scratch_list(t1):
    """scratch list action returns entries."""
    scratch(action="put", content="listed scratch item")
    result = scratch(action="list")
    assert "listed scratch" in result


def test_scratch_manage_flag(t1):
    """scratch_manage flag action marks entry."""
    put_result = scratch(action="put", content="flaggable entry")
    doc_id = put_result.split("Stored:")[1].strip()
    result = scratch_manage(action="flag", entry_id=doc_id)
    assert "Flagged:" in result


def test_scratch_manage_promote(t1, t2_path):
    """scratch_manage promote copies entry to T2."""
    put_result = scratch(action="put", content="promotable content")
    doc_id = put_result.split("Stored:")[1].strip()
    result = scratch_manage(action="promote", entry_id=doc_id, project="promo", title="promoted.md")
    assert "Promoted:" in result
    # Verify it actually landed in T2
    with T2Database(t2_path) as t2:
        entry = t2.get(project="promo", title="promoted.md")
    assert entry is not None
    assert entry["content"] == "promotable content"


# ── Error handling ────────────────────────────────────────────────────────────

def test_error_missing_params(t1):
    """Error cases return 'Error: ...' strings."""
    result = scratch(action="put", content="")
    assert result.startswith("Error:")

    result = scratch(action="search", query="")
    assert result.startswith("Error:")

    result = scratch(action="get", entry_id="")
    assert result.startswith("Error:")

    result = scratch_manage(action="promote", entry_id="fake-id")
    assert result.startswith("Error:")


def test_store_put_empty_content(t3):
    """store_put rejects empty content."""
    result = store_put(content="", collection="knowledge", title="empty")
    assert result.startswith("Error:")
    assert "content" in result.lower()


def test_memory_put_empty_content(t2_path):
    """memory_put rejects empty content."""
    result = memory_put(content="", project="testproj", title="empty.md")
    assert result.startswith("Error:")
    assert "content" in result.lower()


def test_no_ansi_in_output(t1, t3, t2_path):
    """No ANSI escape codes in any tool output."""
    ansi_re = re.compile(r"\x1b\[[0-9;]*m")

    results = [
        scratch(action="put", content="ansi test"),
        scratch(action="list"),
        search(query="test", corpus="knowledge"),
        memory_put(content="ansi check", project="test", title="ansi.md"),
        memory_get(project="test", title="ansi.md"),
        memory_search(query="ansi"),
        store_put(content="ansi store", title="ansi-doc"),
        store_list(collection="knowledge"),
    ]
    for r in results:
        assert not ansi_re.search(r), f"ANSI codes found in: {r[:100]}"


def test_t1_isolated_prefix(t1_isolated):
    """EphemeralClient fallback shows [T1 isolated] prefix."""
    result = scratch(action="put", content="isolated test")
    assert "[T1 isolated]" in result

    result = scratch(action="list")
    assert "[T1 isolated]" in result


# ── B1: Multi-corpus search ───────────────────────────────────────────────────

def test_search_default_multi_corpus():
    """Default corpus searches knowledge, code, and docs collections."""
    from unittest.mock import MagicMock, patch
    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [
        {"name": "knowledge__notes", "count": 5},
        {"name": "code__repo", "count": 10},
        {"name": "docs__manual", "count": 3},
    ]
    _inject_t3(mock_t3)

    captured: list[list[str]] = []

    def fake_search(query, collections, n_results, t3):
        captured.append(list(collections))
        return []

    with patch("nexus.search_engine.search_cross_corpus", fake_search):
        result = search("test query")  # no corpus= arg — uses default

    assert len(captured) == 1
    searched = captured[0]
    assert "knowledge__notes" in searched
    assert "code__repo" in searched
    assert "docs__manual" in searched


def test_search_single_corpus_backward_compat():
    """corpus='knowledge' still works (backward compatibility)."""
    from unittest.mock import MagicMock, patch
    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [
        {"name": "knowledge__notes", "count": 5},
        {"name": "code__repo", "count": 10},
    ]
    _inject_t3(mock_t3)

    captured: list[list[str]] = []

    def fake_search(query, collections, n_results, t3):
        captured.append(list(collections))
        return []

    with patch("nexus.search_engine.search_cross_corpus", fake_search):
        result = search("test query", corpus="knowledge")

    assert len(captured) == 1
    searched = captured[0]
    assert "knowledge__notes" in searched
    assert "code__repo" not in searched


def test_search_all_alias():
    """corpus='all' expands to knowledge,code,docs,rdr."""
    from unittest.mock import MagicMock, patch
    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [
        {"name": "knowledge__notes", "count": 5},
        {"name": "code__repo", "count": 10},
        {"name": "docs__manual", "count": 3},
        {"name": "rdr__decisions", "count": 7},
    ]
    _inject_t3(mock_t3)

    captured: list[list[str]] = []

    def fake_search(query, collections, n_results, t3):
        captured.append(list(collections))
        return []

    with patch("nexus.search_engine.search_cross_corpus", fake_search):
        result = search("test query", corpus="all")

    assert len(captured) == 1
    searched = captured[0]
    assert "knowledge__notes" in searched
    assert "code__repo" in searched
    assert "docs__manual" in searched
    assert "rdr__decisions" in searched


def test_search_fully_qualified_collection():
    """corpus='knowledge__specific' targets that collection directly."""
    from unittest.mock import MagicMock, patch
    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [
        {"name": "knowledge__notes", "count": 5},
        {"name": "knowledge__other", "count": 2},
    ]
    _inject_t3(mock_t3)

    captured: list[list[str]] = []

    def fake_search(query, collections, n_results, t3):
        captured.append(list(collections))
        return []

    with patch("nexus.search_engine.search_cross_corpus", fake_search):
        result = search("test query", corpus="knowledge__notes")

    assert len(captured) == 1
    assert captured[0] == ["knowledge__notes"]


# ── B2: collection_list ───────────────────────────────────────────────────────

def test_collection_list_returns_names_and_counts():
    """collection_list shows all collections with counts and models."""
    from unittest.mock import MagicMock
    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [
        {"name": "knowledge__test", "count": 42},
        {"name": "code__repo", "count": 100},
    ]
    _reset_singletons()
    _inject_t3(mock_t3)

    result = collection_list()
    assert "knowledge__test" in result
    assert "42" in result
    assert "code__repo" in result
    assert "100" in result
    # Models should appear
    assert "voyage-context-3" in result  # knowledge__ model
    assert "voyage-4" in result  # code__ query model


def test_collection_list_empty():
    """collection_list handles no collections gracefully."""
    from unittest.mock import MagicMock
    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = []
    _reset_singletons()
    _inject_t3(mock_t3)

    result = collection_list()
    assert "no collections" in result.lower()


def test_collection_list_sorted():
    """collection_list returns collections in sorted order."""
    from unittest.mock import MagicMock
    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [
        {"name": "knowledge__zzz", "count": 1},
        {"name": "code__aaa", "count": 2},
    ]
    _reset_singletons()
    _inject_t3(mock_t3)

    result = collection_list()
    idx_aaa = result.index("code__aaa")
    idx_zzz = result.index("knowledge__zzz")
    assert idx_aaa < idx_zzz


# ── B3: collection_info ───────────────────────────────────────────────────────

def test_collection_info_returns_metadata():
    """collection_info shows count and model info."""
    from unittest.mock import MagicMock
    mock_t3 = MagicMock()
    mock_t3.collection_info.return_value = {"count": 42, "metadata": {}}
    _reset_singletons()
    _inject_t3(mock_t3)

    result = collection_info("knowledge__test")
    assert "knowledge__test" in result
    assert "42" in result
    assert "voyage-context-3" in result  # CCE model for knowledge__


def test_collection_info_shows_both_models():
    """collection_info shows both index and query models."""
    from unittest.mock import MagicMock
    mock_t3 = MagicMock()
    mock_t3.collection_info.return_value = {"count": 10, "metadata": {}}
    _reset_singletons()
    _inject_t3(mock_t3)

    result = collection_info("code__myrepo")
    assert "voyage-code-3" in result   # index model
    assert "voyage-4" in result        # query model


def test_collection_info_not_found():
    """collection_info returns error for missing collection."""
    from unittest.mock import MagicMock
    mock_t3 = MagicMock()
    mock_t3.collection_info.side_effect = KeyError("not found")
    _reset_singletons()
    _inject_t3(mock_t3)

    result = collection_info("nonexistent")
    assert "not found" in result.lower() or "error" in result.lower()
    assert not result.startswith("Error: ")  # Should be user-friendly, not raw exception


def test_collection_info_with_metadata():
    """collection_info includes non-empty metadata."""
    from unittest.mock import MagicMock
    mock_t3 = MagicMock()
    mock_t3.collection_info.return_value = {
        "count": 5,
        "metadata": {"source": "indexer", "version": "2"},
    }
    _reset_singletons()
    _inject_t3(mock_t3)

    result = collection_info("knowledge__test")
    assert "source" in result or "indexer" in result


# ── B4: collection_verify ─────────────────────────────────────────────────────

def test_collection_verify_healthy():
    """collection_verify returns healthy status for a good collection."""
    from unittest.mock import MagicMock, patch
    from nexus.mcp_server import collection_verify
    from nexus.db.t3 import VerifyResult

    _reset_singletons()
    mock_t3 = MagicMock()
    _inject_t3(mock_t3)

    with patch("nexus.mcp_server.verify_collection_deep") as mock_verify:
        mock_verify.return_value = VerifyResult(
            status="healthy", doc_count=42, probe_doc_id="abc123",
            distance=0.15, metric="l2"
        )
        result = collection_verify("knowledge__test")

    assert "healthy" in result.lower()
    assert "42" in result
    assert "0.15" in result


def test_collection_verify_not_found():
    """collection_verify returns error for missing collection."""
    from unittest.mock import MagicMock, patch
    from nexus.mcp_server import collection_verify

    _reset_singletons()
    mock_t3 = MagicMock()
    _inject_t3(mock_t3)

    with patch("nexus.mcp_server.verify_collection_deep") as mock_verify:
        mock_verify.side_effect = KeyError("not found")
        result = collection_verify("nonexistent")

    assert "not found" in result.lower() or "error" in result.lower()


def test_collection_verify_skipped():
    """collection_verify reports skipped for tiny collections."""
    from unittest.mock import MagicMock, patch
    from nexus.mcp_server import collection_verify
    from nexus.db.t3 import VerifyResult

    _reset_singletons()
    mock_t3 = MagicMock()
    _inject_t3(mock_t3)

    with patch("nexus.mcp_server.verify_collection_deep") as mock_verify:
        mock_verify.return_value = VerifyResult(status="skipped", doc_count=1)
        result = collection_verify("knowledge__tiny")

    assert "skipped" in result.lower()


# ── Collection cache thread-safety ────────────────────────────────────────────

def test_collection_cache_thread_safe():
    """Concurrent cache refreshes never return an empty list."""
    import threading
    from unittest.mock import MagicMock
    from nexus.mcp_server import _get_collection_names

    _reset_singletons()

    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [{"name": "knowledge__test", "count": 5}]
    _inject_t3(mock_t3)

    results: list[list[str]] = []
    errors: list[Exception] = []

    def worker():
        try:
            names = _get_collection_names()
            results.append(names)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    for r in results:
        assert len(r) > 0, f"Cache race: thread saw empty collection list: {r!r}"


# ── Pagination tests ─────────────────────────────────────────────────────────


def test_search_pagination_first_page(t3):
    """search offset=0 returns first page with next offset."""
    for i in range(5):
        t3.put(collection="knowledge__pag", content=f"document about topic {i}", title=f"doc{i}")
    result = search(query="topic", corpus="knowledge__pag", n=2, offset=0)
    assert "showing 1-2" in result
    assert "offset=2" in result


def test_search_pagination_pages_differ(t3):
    """Page 2 content differs from page 1."""
    for i in range(6):
        t3.put(collection="knowledge__pag2", content=f"unique searchable content number {i}", title=f"doc{i}")
    page1 = search(query="searchable content", corpus="knowledge__pag2", n=2, offset=0)
    page2 = search(query="searchable content", corpus="knowledge__pag2", n=2, offset=2)
    assert not page2.startswith("Error:")
    # Extract doc IDs from both pages — they should not overlap
    page1_ids = {line.split("]")[0] for line in page1.split("\n") if line.startswith("[")}
    page2_ids = {line.split("]")[0] for line in page2.split("\n") if line.startswith("[")}
    assert page1_ids.isdisjoint(page2_ids), f"Pages overlap: {page1_ids & page2_ids}"


def test_search_pagination_last_page(t3):
    """Single-result search shows (end) indicator."""
    t3.put(collection="knowledge__pag3", content="only document about finality", title="solo")
    result = search(query="finality", corpus="knowledge__pag3", n=10, offset=0)
    assert "(end)" in result


def test_search_pagination_offset_beyond_end(t3):
    """Offset past all results returns 'No results at offset'."""
    t3.put(collection="knowledge__pag4", content="small collection", title="one")
    result = search(query="small", corpus="knowledge__pag4", n=10, offset=100)
    assert "No results at offset 100" in result


def test_store_list_pagination(t3):
    """store_list pages with true total from collection count."""
    for i in range(5):
        store_put(content=f"entry {i}", collection="knowledge__pagtest", title=f"page-test-{i}")
    page1 = store_list(collection="knowledge__pagtest", limit=2, offset=0)
    assert "showing 1-2 of 5" in page1
    assert "next: offset=2" in page1

    page2 = store_list(collection="knowledge__pagtest", limit=2, offset=2)
    assert "showing 3-4 of 5" in page2


def test_store_list_pagination_offset_beyond_end(t3):
    """store_list offset past total returns 'No entries at offset'."""
    store_put(content="solo entry", collection="knowledge__pagend", title="one")
    result = store_list(collection="knowledge__pagend", limit=10, offset=100)
    assert "No entries at offset 100" in result


def test_store_list_collection_not_found(t3):
    """store_list returns 'Collection not found' for missing collection."""
    result = store_list(collection="knowledge__doesnotexist", limit=10)
    assert "Collection not found" in result


def test_memory_search_pagination(t2_path):
    """memory_search pages with offset."""
    for i in range(5):
        memory_put(content=f"finding about pagination topic {i}", project="testproj", title=f"page{i}.md")
    page1 = memory_search(query="pagination", limit=2, offset=0)
    assert "showing 1-2 of 5" in page1
    assert "next: offset=2" in page1

    page2 = memory_search(query="pagination", limit=2, offset=2)
    assert "showing 3-4 of 5" in page2


def test_memory_search_pagination_offset_beyond_end(t2_path):
    """memory_search offset past results returns 'No results at offset'."""
    memory_put(content="solitary finding about offsets", project="testproj", title="solo.md")
    result = memory_search(query="offsets", limit=10, offset=100)
    assert "No results at offset 100" in result


def test_plan_save_and_search(t2_path):
    """plan_save stores a plan, plan_search retrieves it."""
    result = plan_save(
        query="compare error handling",
        plan_json='{"steps": [{"step": 1, "operation": "search"}]}',
        project="testproj",
        tags="search,compare",
    )
    assert "Saved plan:" in result

    found = plan_search(query="error handling", project="testproj")
    assert "compare error handling" in found


def test_plan_search_empty(t2_path):
    """plan_search returns 'No matching plans.' when library is empty."""
    result = plan_search(query="nonexistent")
    assert "No matching plans" in result
