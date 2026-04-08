# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for nexus MCP server tools.

All tests use injected clients -- no API keys or network required.
- T1: chromadb.EphemeralClient (bundled ONNX MiniLM)
- T2: temp-file SQLite via T2Database
- T3: chromadb.EphemeralClient with DefaultEmbeddingFunction override
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest

from nexus.db.t1 import T1Database
from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database, VerifyResult
from nexus.mcp_server import (
    _get_collection_names,
    _inject_catalog,
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
    query,
    scratch,
    scratch_manage,
    search,
    store_get,
    store_list,
    store_put,
)
from nexus.session import find_ancestor_session
from nexus.types import SearchResult

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset():
    _reset_singletons()
    yield
    _reset_singletons()


@pytest.fixture()
def t1():
    client = chromadb.EphemeralClient()
    db = T1Database(session_id="test-session", client=client)
    _inject_t1(db)
    return db


@pytest.fixture()
def t1_isolated():
    client = chromadb.EphemeralClient()
    db = T1Database(session_id="test-session-iso", client=client)
    _inject_t1(db, isolated=True)
    return db


@pytest.fixture()
def t2_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield Path(f.name)


@pytest.fixture()
def t3():
    client = chromadb.EphemeralClient()
    ef = chromadb.utils.embedding_functions.DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    _inject_t3(db)
    return db


@pytest.fixture(autouse=True)
def _patch_t2(t2_path, monkeypatch):
    import nexus.mcp_server as mod
    monkeypatch.setattr(mod, "_t2_ctx", lambda: T2Database(t2_path))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mock_t3(collections: list[dict]) -> MagicMock:
    """Create a mock T3 with preset list_collections and inject it."""
    mock = MagicMock()
    mock.list_collections.return_value = collections
    _inject_t3(mock)
    return mock


def _capture_search():
    """Return (captured, fake_fn) that records which collections are queried."""
    captured: list[list[str]] = []

    def fake(query, collections, n_results, t3, where=None, **kwargs):
        captured.append(list(collections))
        return []

    return captured, fake


def _put_id(content: str, collection: str = "knowledge", title: str = "t") -> str:
    """store_put then extract the doc ID."""
    return store_put(content=content, collection=collection, title=title) \
        .split("Stored:")[1].strip().split(" ->")[0].strip()


# ── Search ───────────────────────────────────────────────────────────────────

def test_search_returns_results(t3):
    t3.put(collection="knowledge__test", content="chromadb vector database", title="doc1")
    result = search(query="vector database", corpus="knowledge__test", limit=5)
    assert not result.startswith("Error:")
    assert "vector database" in result.lower() or "doc1" in result


def test_search_no_results(t3):
    result = search(query="nonexistent topic", corpus="knowledge__empty")
    assert not result.startswith("Error:")
    assert "no" in result.lower()


# ── Store ────────────────────────────────────────────────────────────────────

def test_store_put(t3):
    result = store_put(content="test content", collection="knowledge", title="test-doc")
    assert "Stored:" in result
    assert "knowledge__knowledge" in result


def test_store_list(t3):
    store_put(content="listed entry", collection="knowledge", title="list-test")
    result = store_list(collection="knowledge")
    assert not result.startswith("Error:")
    assert "entries" in result.lower() or "list-test" in result


@pytest.mark.parametrize("content, collection, title, expect_in", [
    pytest.param("full document text here", "knowledge", "get-test",
                 ["full document text here"], id="full-content"),
    pytest.param("metadata content", "knowledge", "metadata-test-doc",
                 ["metadata-test-doc", "knowledge__knowledge"], id="metadata"),
    pytest.param("qualified content", "knowledge__knowledge", "qualified-test",
                 ["qualified content"], id="fully-qualified"),
])
def test_store_get_round_trip(t3, content, collection, title, expect_in):
    doc_id = _put_id(content, collection, title)
    result = store_get(doc_id=doc_id, collection=collection)
    assert not result.startswith("Error:")
    for text in expect_in:
        assert text in result


def test_store_get_not_found(t3):
    result = store_get(doc_id="nonexistent-id-12345", collection="knowledge")
    assert not result.startswith("Error:")
    assert "not found" in result.lower() or "nonexistent" in result.lower()


def test_store_get_empty_doc_id(t3):
    result = store_get(doc_id="", collection="knowledge")
    assert result.startswith("Error:")


def test_store_get_no_ansi(t3):
    doc_id = _put_id("ansi check content", title="ansi-get-test")
    result = store_get(doc_id=doc_id, collection="knowledge")
    assert not ANSI_RE.search(result), f"ANSI codes found in: {result[:100]}"


# ── Memory ───────────────────────────────────────────────────────────────────

def test_memory_put(t2_path):
    result = memory_put(content="test memory", project="testproj", title="finding.md")
    assert "Stored:" in result
    assert "testproj/finding.md" in result


def test_memory_get_by_title(t2_path):
    memory_put(content="retrievable content", project="testproj", title="doc.md")
    assert "retrievable content" in memory_get(project="testproj", title="doc.md")


def test_memory_get_empty_title_lists(t2_path):
    memory_put(content="entry1", project="listproj", title="a.md")
    memory_put(content="entry2", project="listproj", title="b.md")
    result = memory_get(project="listproj", title="")
    assert "2 entries" in result and "a.md" in result and "b.md" in result


def test_memory_search(t2_path):
    memory_put(content="chromadb vector embeddings", project="testproj", title="vectors.md")
    assert "vector" in memory_search(query="chromadb").lower()


# ── Scratch ──────────────────────────────────────────────────────────────────

def test_scratch_put(t1):
    result = scratch(action="put", content="scratch note")
    assert "Stored:" in result
    assert len(result.split("Stored:")[1].strip()) > 0


def test_scratch_search(t1):
    scratch(action="put", content="semantic search hypothesis")
    result = scratch(action="search", query="semantic search")
    assert "semantic" in result.lower() or "hypothesis" in result.lower()


def test_scratch_list(t1):
    scratch(action="put", content="listed scratch item")
    assert "listed scratch" in scratch(action="list")


def test_scratch_manage_flag(t1):
    doc_id = scratch(action="put", content="flaggable entry").split("Stored:")[1].strip()
    assert "Flagged:" in scratch_manage(action="flag", entry_id=doc_id)


def test_scratch_manage_promote(t1, t2_path):
    doc_id = scratch(action="put", content="promotable content").split("Stored:")[1].strip()
    result = scratch_manage(action="promote", entry_id=doc_id, project="promo", title="promoted.md")
    assert "Promoted:" in result
    with T2Database(t2_path) as t2:
        entry = t2.get(project="promo", title="promoted.md")
    assert entry is not None and entry["content"] == "promotable content"


# ── Error handling ───────────────────────────────────────────────────────────

def test_error_missing_params(t1):
    assert scratch(action="put", content="").startswith("Error:")
    assert scratch(action="search", query="").startswith("Error:")
    assert scratch(action="get", entry_id="").startswith("Error:")
    assert scratch_manage(action="promote", entry_id="fake-id").startswith("Error:")


def test_store_put_empty_content(t3):
    result = store_put(content="", collection="knowledge", title="empty")
    assert result.startswith("Error:") and "content" in result.lower()


def test_memory_put_empty_content(t2_path):
    result = memory_put(content="", project="testproj", title="empty.md")
    assert result.startswith("Error:") and "content" in result.lower()


def test_no_ansi_in_output(t1, t3, t2_path):
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
        assert not ANSI_RE.search(r), f"ANSI codes found in: {r[:100]}"


def test_t1_isolated_prefix(t1_isolated):
    assert "[T1 isolated]" in scratch(action="put", content="isolated test")
    assert "[T1 isolated]" in scratch(action="list")


# ── Multi-corpus search routing (parametrized) ──────────────────────────────

@pytest.mark.parametrize("corpus_arg, available, expected_in, expected_not_in", [
    pytest.param(
        None,
        [{"name": "knowledge__notes", "count": 5}, {"name": "code__repo", "count": 10},
         {"name": "docs__manual", "count": 3}],
        ["knowledge__notes", "code__repo", "docs__manual"], [],
        id="default-multi-corpus",
    ),
    pytest.param(
        "knowledge",
        [{"name": "knowledge__notes", "count": 5}, {"name": "code__repo", "count": 10}],
        ["knowledge__notes"], ["code__repo"],
        id="single-corpus-backward-compat",
    ),
    pytest.param(
        "all",
        [{"name": "knowledge__notes", "count": 5}, {"name": "code__repo", "count": 10},
         {"name": "docs__manual", "count": 3}, {"name": "rdr__decisions", "count": 7}],
        ["knowledge__notes", "code__repo", "docs__manual", "rdr__decisions"], [],
        id="all-alias",
    ),
    pytest.param(
        "knowledge__notes",
        [{"name": "knowledge__notes", "count": 5}, {"name": "knowledge__other", "count": 2}],
        ["knowledge__notes"], ["knowledge__other"],
        id="fully-qualified-collection",
    ),
])
def test_search_corpus_routing(corpus_arg, available, expected_in, expected_not_in):
    _mock_t3(available)
    captured, fake = _capture_search()
    kwargs = {"corpus": corpus_arg} if corpus_arg is not None else {}
    with patch("nexus.search_engine.search_cross_corpus", fake):
        search("test query", **kwargs)
    assert len(captured) == 1
    for name in expected_in:
        assert name in captured[0]
    for name in expected_not_in:
        assert name not in captured[0]


# ── collection_list ──────────────────────────────────────────────────────────

def test_collection_list_returns_names_and_counts():
    _mock_t3([{"name": "knowledge__test", "count": 42}, {"name": "code__repo", "count": 100}])
    result = collection_list()
    for s in ("knowledge__test", "42", "code__repo", "100", "voyage-context-3", "voyage-code-3"):
        assert s in result


def test_collection_list_empty():
    _mock_t3([])
    assert "no collections" in collection_list().lower()


def test_collection_list_sorted():
    _mock_t3([{"name": "knowledge__zzz", "count": 1}, {"name": "code__aaa", "count": 2}])
    result = collection_list()
    assert result.index("code__aaa") < result.index("knowledge__zzz")


# ── collection_info (parametrized) ───────────────────────────────────────────

@pytest.mark.parametrize("name, info_return, expect_in", [
    pytest.param("knowledge__test", {"count": 42, "metadata": {}},
                 ["knowledge__test", "42", "voyage-context-3"], id="knowledge-metadata"),
    pytest.param("code__myrepo", {"count": 10, "metadata": {}},
                 ["voyage-code-3"], id="code-both-models"),
    pytest.param("knowledge__test", {"count": 5, "metadata": {"source": "indexer", "version": "2"}},
                 ["source"], id="with-metadata"),
])
def test_collection_info_ok(name, info_return, expect_in):
    mock = MagicMock()
    mock.collection_info.return_value = info_return
    _inject_t3(mock)
    result = collection_info(name)
    for s in expect_in:
        assert s in result


def test_collection_info_not_found():
    mock = MagicMock()
    mock.collection_info.side_effect = KeyError("not found")
    _inject_t3(mock)
    result = collection_info("nonexistent")
    assert "not found" in result.lower() or "error" in result.lower()
    assert not result.startswith("Error: ")


# ── collection_verify (parametrized) ─────────────────────────────────────────

@pytest.mark.parametrize("name, verify_rv, side_effect, expect_in", [
    pytest.param("knowledge__test",
                 VerifyResult(status="healthy", doc_count=42, probe_doc_id="abc123",
                              distance=0.15, metric="l2"),
                 None, ["healthy", "42", "0.15"], id="healthy"),
    pytest.param("knowledge__tiny",
                 VerifyResult(status="skipped", doc_count=1),
                 None, ["skipped"], id="skipped"),
    pytest.param("nonexistent", None, KeyError("not found"), ["not found"], id="not-found"),
])
def test_collection_verify_cases(name, verify_rv, side_effect, expect_in):
    mock = MagicMock()
    _inject_t3(mock)
    with patch("nexus.mcp_server.verify_collection_deep") as mv:
        if side_effect:
            mv.side_effect = side_effect
        else:
            mv.return_value = verify_rv
        result = collection_verify(name)
    for s in expect_in:
        assert s in result.lower()


# ── Collection cache thread-safety ───────────────────────────────────────────

def test_collection_cache_thread_safe():
    import threading
    _mock_t3([{"name": "knowledge__test", "count": 5}])
    results: list[list[str]] = []
    errors: list[Exception] = []

    def worker():
        try:
            results.append(_get_collection_names())
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert all(len(r) > 0 for r in results), "Cache race: thread saw empty list"


# ── Pagination ───────────────────────────────────────────────────────────────

def test_search_pagination_first_page(t3):
    for i in range(5):
        t3.put(collection="knowledge__pag", content=f"document about topic {i}", title=f"doc{i}")
    result = search(query="topic", corpus="knowledge__pag", limit=2, offset=0)
    assert "showing 1-2" in result and "offset=2" in result


def test_search_pagination_pages_differ(t3):
    for i in range(6):
        t3.put(collection="knowledge__pag2", content=f"unique searchable content number {i}", title=f"doc{i}")
    page1 = search(query="searchable content", corpus="knowledge__pag2", limit=2, offset=0)
    page2 = search(query="searchable content", corpus="knowledge__pag2", limit=2, offset=2)
    assert not page2.startswith("Error:")
    p1 = {l.split("]")[0] for l in page1.split("\n") if l.startswith("[")}
    p2 = {l.split("]")[0] for l in page2.split("\n") if l.startswith("[")}
    assert p1.isdisjoint(p2), f"Pages overlap: {p1 & p2}"


def test_search_pagination_last_page(t3):
    t3.put(collection="knowledge__pag3", content="only document about finality", title="solo")
    assert "(end)" in search(query="finality", corpus="knowledge__pag3", limit=10, offset=0)


def test_search_pagination_offset_beyond_end(t3):
    t3.put(collection="knowledge__pag4", content="small collection", title="one")
    assert "No results at offset 100" in search(query="small", corpus="knowledge__pag4", limit=10, offset=100)


def test_store_list_pagination(t3):
    for i in range(5):
        store_put(content=f"entry {i}", collection="knowledge__pagtest", title=f"page-test-{i}")
    page1 = store_list(collection="knowledge__pagtest", limit=2, offset=0)
    assert "showing 1-2 of 5" in page1 and "next: offset=2" in page1
    assert "showing 3-4 of 5" in store_list(collection="knowledge__pagtest", limit=2, offset=2)


def test_store_list_pagination_offset_beyond_end(t3):
    store_put(content="solo entry", collection="knowledge__pagend", title="one")
    assert "No entries at offset 100" in store_list(collection="knowledge__pagend", limit=10, offset=100)


def test_store_list_collection_not_found(t3):
    assert "Collection not found" in store_list(collection="knowledge__doesnotexist", limit=10)


def test_memory_search_pagination(t2_path):
    for i in range(5):
        memory_put(content=f"finding about pagination topic {i}", project="testproj", title=f"page{i}.md")
    page1 = memory_search(query="pagination", limit=2, offset=0)
    assert "showing 1-2 of 5" in page1 and "next: offset=2" in page1
    assert "showing 3-4 of 5" in memory_search(query="pagination", limit=2, offset=2)


def test_memory_search_pagination_offset_beyond_end(t2_path):
    memory_put(content="solitary finding about offsets", project="testproj", title="solo.md")
    assert "No results at offset 100" in memory_search(query="offsets", limit=10, offset=100)


# ── Plans ────────────────────────────────────────────────────────────────────

def test_plan_save_and_search(t2_path):
    result = plan_save(
        query="compare error handling",
        plan_json='{"steps": [{"step": 1, "operation": "search"}]}',
        project="testproj", tags="search,compare",
    )
    assert "Saved plan:" in result
    assert "compare error handling" in plan_search(query="error handling", project="testproj")


def test_plan_search_empty(t2_path):
    assert "No matching plans" in plan_search(query="nonexistent")


# ── _get_catalog error propagation ───────────────────────────────────────────

def test_get_catalog_propagates_non_os_errors():
    from nexus.mcp_server import _get_catalog
    mock_catalog = MagicMock()
    mock_catalog.degraded = False
    with patch("nexus.mcp_server._catalog_instance", mock_catalog), \
         patch("nexus.mcp_server._catalog_mtime", 0.0), \
         patch("nexus.mcp_server._max_jsonl_mtime", return_value=999.0):
        mock_catalog._ensure_consistent.side_effect = ValueError("corrupt JSONL")
        with pytest.raises(ValueError):
            _get_catalog()
        mock_catalog._ensure_consistent.side_effect = OSError("file not found")
        assert _get_catalog() is mock_catalog


# ── Query catalog-routing ────────────────────────────────────────────────────

@pytest.fixture()
def catalog_with_docs(tmp_path):
    from nexus.catalog.catalog import Catalog
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")
    o1 = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
    o2 = cat.register_owner("papers", "curator")
    cat.register(o1, "indexer.py", content_type="code", file_path="src/nexus/indexer.py",
                 physical_collection="code__nexus", chunk_count=10, author="hal")
    cat.register(o1, "chunker.py", content_type="code", file_path="src/nexus/chunker.py",
                 physical_collection="code__nexus", chunk_count=5, author="hal")
    cat.register(o2, "Attention Paper", content_type="paper",
                 physical_collection="knowledge__papers", chunk_count=20, author="Vaswani")
    cat.register(o2, "BERT Paper", content_type="paper",
                 physical_collection="knowledge__papers", chunk_count=15, author="Devlin")
    cat.link(cat.find("Attention Paper")[0].tumbler,
             cat.find("BERT Paper")[0].tumbler, "cites", created_by="test")
    _inject_catalog(cat)
    return cat


class TestQueryCatalogRouting:
    def test_backward_compat(self, t3):
        t3.put(collection="knowledge__test", content="vector database chunking", title="doc1")
        assert not query(question="vector database", corpus="knowledge__test").startswith("Error:")

    @pytest.mark.parametrize("kwargs, collection, content, expect_in", [
        pytest.param({"author": "Vaswani"}, "knowledge__papers",
                     "transformer attention mechanism", ["knowledge__papers"], id="author"),
        pytest.param({"content_type": "code"}, "code__nexus",
                     "def index_repo(): chunking pipeline", [], id="content-type"),
        pytest.param({"subtree": "1.1"}, "code__nexus",
                     "def chunk_file(): ast parsing", [], id="subtree"),
        pytest.param({"follow_links": "cites"}, "knowledge__papers",
                     "transformer attention is all you need", [], id="follow-links"),
    ])
    def test_catalog_filter(self, t3, catalog_with_docs, kwargs, collection, content, expect_in):
        t3.put(collection=collection, content=content, title="t")
        result = query(question=content.split(":")[0] if ":" in content else content[:20], **kwargs)
        assert not result.startswith("Error:")
        for s in expect_in:
            assert s in result

    def test_catalog_params_without_catalog(self, t3, monkeypatch):
        import nexus.mcp_server as mod
        monkeypatch.setattr(mod, "_get_catalog", lambda: None)
        assert "catalog not initialized" in query(question="test", author="someone").lower()

    @pytest.mark.parametrize("kwargs", [
        pytest.param({"author": "NonexistentAuthor"}, id="author-no-match"),
        pytest.param({"subtree": "9.9"}, id="subtree-empty"),
    ])
    def test_catalog_no_match(self, t3, catalog_with_docs, kwargs):
        assert "No documents found matching catalog filters" in query(question="anything", **kwargs)


# ── Cluster output (RDR-056) ────────────────────────────────────────────────

def _make_clustered_results() -> list[SearchResult]:
    return [
        SearchResult(id="a1", content="HNSW tail failures in approximate search",
                     distance=0.41, collection="knowledge__papers",
                     metadata={"_cluster_label": "HNSW Robustness", "title": "HNSW paper"}),
        SearchResult(id="a2", content="Graph-based index failures compound in pipelines",
                     distance=0.52, collection="knowledge__papers",
                     metadata={"_cluster_label": "HNSW Robustness", "title": "Pipeline paper"}),
        SearchResult(id="b1", content="Ward hierarchical clustering groups results",
                     distance=0.45, collection="docs__manual",
                     metadata={"_cluster_label": "Result Clustering", "title": "Clustering doc"}),
        SearchResult(id="b2", content="Semantic grouping improves LLM comprehension",
                     distance=0.55, collection="docs__manual",
                     metadata={"_cluster_label": "Result Clustering", "title": "LLM doc"}),
    ]


def _search_clustered(results, corpus="knowledge,docs", cluster_by="semantic"):
    collections = [{"name": "knowledge__papers", "count": 10}]
    if "docs" in corpus:
        collections.append({"name": "docs__manual", "count": 5})
    _mock_t3(collections)
    with patch("nexus.search_engine.search_cross_corpus",
               lambda q, c, n_results=10, t3=None, where=None, **kw: results):
        return search("test query", corpus=corpus, cluster_by=cluster_by)


def test_cluster_labels_in_output():
    output = _search_clustered(_make_clustered_results())
    assert "HNSW Robustness" in output and "Result Clustering" in output


def test_cluster_order_preserved():
    output = _search_clustered(_make_clustered_results())
    positions = [output.find(s) for s in (
        "HNSW tail failures", "Graph-based index", "Ward hierarchical", "Semantic grouping")]
    assert positions == sorted(positions) and all(p >= 0 for p in positions)


def test_flat_search_no_cluster_headers():
    results = [SearchResult(id="r1", content="some result", distance=0.3,
                            collection="knowledge__papers", metadata={"title": "Paper"})]
    output = _search_clustered(results, corpus="knowledge", cluster_by="")
    assert "---" not in output.split("\n--- showing")[0]


# ── T1 session sharing ──────────────────────────────────────────────────────

def test_t1_shared_session():
    client = chromadb.EphemeralClient()
    t1a = T1Database(session_id="shared-42", client=client)
    t1b = T1Database(session_id="shared-42", client=client)
    doc_id = t1a.put("shared entry from A")
    entry = t1b.get(doc_id)
    assert entry is not None and entry["content"] == "shared entry from A"
    assert any("shared entry from A" in r["content"] for r in t1b.search("shared entry", n_results=5))


def test_t1_session_isolation():
    client = chromadb.EphemeralClient()
    t1a = T1Database(session_id="session-alpha", client=client)
    t1b = T1Database(session_id="session-beta", client=client)
    t1a.put("alpha only")
    t1b.put("beta only")
    assert all(e["content"] != "beta only" for e in t1a.list_entries())
    assert all(e["content"] != "alpha only" for e in t1b.list_entries())


@pytest.mark.parametrize("record, expected_found", [
    pytest.param({"session_id": "test-session-id", "server_host": "127.0.0.1",
                  "server_port": 9999, "server_pid": 12345}, True, id="resolves-record"),
    pytest.param(None, False, id="no-record"),
    pytest.param({"session_id": "stale-session", "server_host": "127.0.0.1",
                  "server_port": 8888, "server_pid": 99999,
                  "_created_at_offset": -25 * 3600}, False, id="skips-stale"),
])
def test_find_ancestor_session(record, expected_found):
    with tempfile.TemporaryDirectory() as tmpdir:
        sessions_dir = Path(tmpdir)
        pid = os.getpid()
        if record is not None:
            offset = record.pop("_created_at_offset", 0)
            record["created_at"] = time.time() + offset
            (sessions_dir / f"{pid}.session").write_text(json.dumps(record))
        result = find_ancestor_session(sessions_dir=sessions_dir, start_pid=pid)
        if expected_found:
            assert result is not None
            assert result["session_id"] == record["session_id"]
            assert result["server_port"] == record["server_port"]
        else:
            assert result is None


# ── MCP client SDK round-trip (integration) ──────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_server_round_trip():
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    nx_mcp = Path(sys.executable).parent / "nx-mcp"
    if not nx_mcp.exists():
        pytest.skip("nx-mcp entry point not found; run 'uv sync' first")

    server_params = StdioServerParameters(command=str(nx_mcp), args=[])
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tool_names = [t.name for t in (await session.list_tools()).tools]
            for expected in ("search", "store_put", "store_list", "memory_put",
                             "memory_get", "memory_search", "scratch", "scratch_manage"):
                assert expected in tool_names

            r = await session.call_tool("scratch", {"action": "put", "content": "integration test entry", "tags": "test"})
            assert "Stored:" in r.content[0].text or "isolated" in r.content[0].text.lower()

            r = await session.call_tool("scratch", {"action": "list"})
            assert "integration test" in r.content[0].text or "No scratch" in r.content[0].text

            r = await session.call_tool("memory_put", {"content": "integration memory test", "project": "mcp-integ", "title": "round-trip.md"})
            assert "Stored:" in r.content[0].text

            r = await session.call_tool("memory_get", {"project": "mcp-integ", "title": "round-trip.md"})
            assert "integration memory test" in r.content[0].text

            r = await session.call_tool("memory_search", {"query": "integration", "project": "mcp-integ"})
            assert "integration" in r.content[0].text.lower()
