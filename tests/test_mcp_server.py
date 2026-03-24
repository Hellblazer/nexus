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
    memory_get,
    memory_put,
    memory_search,
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
