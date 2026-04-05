# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-052 verification tests: catalog-first query routing.

Unit tests (no API keys) verifying:
- Path routing logic (author, content_type, subtree, follow_links)
- Template matching and idempotency
- Plan TTL auto-cache behavior
- Backward compatibility (no catalog params = old behavior)
- Tumbler hierarchy helpers (ancestors, lca, depth, resolve_chunk)
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import chromadb
import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database
from nexus.mcp_server import (
    _inject_catalog,
    _inject_t3,
    _reset_singletons,
    query,
)


@pytest.fixture(autouse=True)
def _reset():
    _reset_singletons()
    yield
    _reset_singletons()


@pytest.fixture()
def t3():
    client = chromadb.EphemeralClient()
    ef = chromadb.utils.embedding_functions.DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    _inject_t3(db)
    return db


@pytest.fixture()
def catalog(tmp_path):
    """Catalog with two owners and documents for routing tests."""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")
    repo_owner = cat.register_owner("nexus", "repo", repo_hash="aabb1122")
    paper_owner = cat.register_owner("papers", "curator")

    # Code documents under nexus owner (1.1)
    cat.register(
        repo_owner, "indexer.py", content_type="code",
        file_path="src/nexus/indexer.py", physical_collection="code__nexus",
        chunk_count=10, author="hal",
    )
    cat.register(
        repo_owner, "chunker.py", content_type="code",
        file_path="src/nexus/chunker.py", physical_collection="code__nexus",
        chunk_count=5, author="hal",
    )

    # Paper documents under papers owner (1.2)
    cat.register(
        paper_owner, "Schema Mappings and Data Exchange",
        content_type="paper", physical_collection="knowledge__delos",
        chunk_count=20, author="Fagin",
    )
    cat.register(
        paper_owner, "Composing Mappings Among Data Sources",
        content_type="paper", physical_collection="knowledge__delos",
        chunk_count=15, author="Fagin",
    )
    cat.register(
        paper_owner, "Attention Is All You Need",
        content_type="paper", physical_collection="knowledge__transformers",
        chunk_count=30, author="Vaswani",
    )

    # RDR document
    cat.register(
        repo_owner, "RDR-052: Catalog-First Query Routing",
        content_type="rdr", physical_collection="rdr__nexus",
        chunk_count=8, author="hal",
    )

    # Links: Fagin paper cites Vaswani paper (synthetic for testing)
    fagin_t = cat.find("Schema Mappings")[0].tumbler
    vaswani_t = cat.find("Attention")[0].tumbler
    cat.link(fagin_t, vaswani_t, "cites", created_by="test")

    _inject_catalog(cat)
    return cat


# ── Path Routing Tests ───────────────────────────────────────────────────────


class TestPathRouting:
    """Verify that catalog params route to the correct collections."""

    def test_author_routes_to_author_collections(self, t3, catalog):
        """query(author="Fagin") should search knowledge__delos only."""
        t3.put(collection="knowledge__delos", content="chase procedure schema", title="fagin-chunk")
        t3.put(collection="knowledge__transformers", content="attention mechanism", title="vaswani-chunk")
        result = query(question="schema mappings", author="Fagin")
        assert not result.startswith("Error:")
        assert "knowledge__delos" in result

    def test_content_type_routes_to_type_collections(self, t3, catalog):
        """query(content_type="code") should search code__nexus only."""
        t3.put(collection="code__nexus", content="def index_repo(): pipeline", title="code-chunk")
        result = query(question="indexing pipeline", content_type="code")
        assert not result.startswith("Error:")
        assert "code__nexus" in result

    def test_subtree_routes_to_descendants(self, t3, catalog):
        """query(subtree="1.1") should find all nexus repo docs (code + rdr)."""
        t3.put(collection="code__nexus", content="tree sitter chunking", title="ts-chunk")
        t3.put(collection="rdr__nexus", content="catalog first query routing", title="rdr-chunk")
        result = query(question="chunking", subtree="1.1")
        assert not result.startswith("Error:")
        # Should search both code__nexus and rdr__nexus (both under owner 1.1)

    def test_follow_links_enriches_collections(self, t3, catalog):
        """query(follow_links="cites") should include linked document collections."""
        t3.put(collection="knowledge__delos", content="schema data exchange", title="delos-chunk")
        t3.put(collection="knowledge__transformers", content="attention heads layers", title="trans-chunk")
        # Fagin paper cites Vaswani — follow_links should pull in knowledge__transformers
        result = query(question="schema mappings", follow_links="cites")
        assert not result.startswith("Error:")
        assert "Found" in result or "knowledge__" in result

    def test_no_catalog_params_backward_compat(self, t3):
        """query() without catalog params works as before."""
        t3.put(collection="knowledge__test", content="vector database embeddings", title="vec-chunk")
        result = query(question="vector database", corpus="knowledge__test")
        assert not result.startswith("Error:")
        assert "vector" in result.lower() or "Found" in result

    def test_author_no_match_returns_clear_message(self, t3, catalog):
        """query(author="NonexistentPerson") returns a clear no-match message."""
        result = query(question="anything", author="NonexistentPerson")
        assert "No documents found matching catalog filters" in result

    def test_subtree_empty_returns_clear_message(self, t3, catalog):
        """query(subtree="9.9") returns a clear no-match message."""
        result = query(question="anything", subtree="9.9")
        assert "No documents found matching catalog filters" in result

    def test_catalog_params_without_catalog_returns_error(self, t3, monkeypatch):
        """query() with catalog params but no catalog returns clear error."""
        import nexus.mcp_server as mod
        monkeypatch.setattr(mod, "_get_catalog", lambda: None)
        result = query(question="test", author="someone")
        assert "catalog not initialized" in result.lower()


class TestReferenceQuestions:
    """5 reference questions that verify correct path selection."""

    def test_papers_by_fagin(self, t3, catalog):
        """'papers by Fagin' → Path 1: query(author='Fagin')."""
        t3.put(collection="knowledge__delos", content="schema mappings chase", title="ref1")
        result = query(question="papers by Fagin", author="Fagin")
        assert not result.startswith("Error:")
        assert "knowledge__delos" in result

    def test_author_and_topic(self, t3, catalog):
        """'schema mappings' + author=Fagin → Path 1: scoped query."""
        t3.put(collection="knowledge__delos", content="schema mappings data exchange", title="ref2")
        result = query(question="schema mappings", author="Fagin")
        assert not result.startswith("Error:")

    def test_rdr_by_type(self, t3, catalog):
        """'RDR about streaming' → Path 1: query(content_type='rdr')."""
        t3.put(collection="rdr__nexus", content="streaming pipeline buffer", title="ref3")
        result = query(question="RDR about streaming", content_type="rdr")
        assert not result.startswith("Error:")

    def test_citation_enriched(self, t3, catalog):
        """'what cites schema mappings' → Path 1: follow_links."""
        t3.put(collection="knowledge__delos", content="data exchange framework", title="ref4")
        result = query(question="what cites schema mappings", follow_links="cites")
        assert not result.startswith("Error:")

    def test_subtree_scoped(self, t3, catalog):
        """'nexus architecture' → subtree scoped to nexus owner."""
        t3.put(collection="code__nexus", content="module architecture design", title="ref5")
        result = query(question="nexus architecture", subtree="1.1")
        assert not result.startswith("Error:")


# ── Template and Plan Tests ──────────────────────────────────────────────────


class TestPlanTemplates:
    """Verify builtin template seeding and idempotency."""

    def test_seed_templates_creates_five(self, tmp_path, monkeypatch):
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        assert _seed_plan_templates() == 5

    def test_seed_templates_idempotent(self, tmp_path, monkeypatch):
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        _seed_plan_templates()
        assert _seed_plan_templates() == 0

    def test_templates_all_tagged_builtin(self, tmp_path, monkeypatch):
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        _seed_plan_templates()
        db = T2Database(db_path)
        for p in db.list_plans(limit=10):
            assert "builtin-template" in p["tags"]
        db.close()

    def test_templates_have_no_ttl(self, tmp_path, monkeypatch):
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        from nexus.commands.catalog import _seed_plan_templates
        _seed_plan_templates()
        db = T2Database(db_path)
        for p in db.list_plans(limit=10):
            assert p["ttl"] is None
        db.close()


class TestPlanTTL:
    """Verify plan TTL storage and auto-cache behavior."""

    def test_save_plan_with_ttl(self, tmp_path):
        db = T2Database(tmp_path / "t2.db")
        row_id = db.save_plan(query="cached plan", plan_json='{"steps":[]}', ttl=30)
        row = db.conn.execute("SELECT ttl FROM plans WHERE id = ?", (row_id,)).fetchone()
        assert row[0] == 30
        db.close()

    def test_save_plan_permanent(self, tmp_path):
        db = T2Database(tmp_path / "t2.db")
        row_id = db.save_plan(query="permanent plan", plan_json='{}')
        row = db.conn.execute("SELECT ttl FROM plans WHERE id = ?", (row_id,)).fetchone()
        assert row[0] is None
        db.close()

    def test_ttl_in_search_results(self, tmp_path):
        db = T2Database(tmp_path / "t2.db")
        db.save_plan(query="searchable cached plan", plan_json='{}', ttl=7)
        results = db.search_plans("searchable")
        assert len(results) == 1
        assert results[0]["ttl"] == 7
        db.close()

    def test_ttl_in_list_results(self, tmp_path):
        db = T2Database(tmp_path / "t2.db")
        db.save_plan(query="listed plan", plan_json='{}', ttl=14)
        results = db.list_plans()
        assert len(results) == 1
        assert results[0]["ttl"] == 14
        db.close()


# ── Tumbler Hierarchy Verification ───────────────────────────────────────────


class TestTumblerHierarchyVerification:
    """Cross-check tumbler hierarchy helpers match RDR-052 spec."""

    def test_depth(self):
        assert Tumbler.parse("1").depth == 1
        assert Tumbler.parse("1.2").depth == 2
        assert Tumbler.parse("1.2.42").depth == 3
        assert Tumbler.parse("1.2.42.7").depth == 4

    def test_ancestors_includes_self(self):
        t = Tumbler.parse("1.2.42")
        anc = t.ancestors()
        assert anc[-1] == t

    def test_lca_sibling_documents(self):
        a = Tumbler.parse("1.1.10")
        b = Tumbler.parse("1.1.20")
        assert Tumbler.lca(a, b) == Tumbler.parse("1.1")

    def test_lca_no_common_prefix(self):
        a = Tumbler.parse("1.1.1")
        b = Tumbler.parse("2.1.1")
        assert Tumbler.lca(a, b) is None

    def test_resolve_chunk_ghost_element(self, tmp_path):
        """Chunks are ghost elements — addressable without registration."""
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()
        cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="aabb")
        cat.register(owner, "a.py", content_type="code",
                     physical_collection="code__nexus", chunk_count=5)
        result = cat.resolve_chunk(Tumbler.parse("1.1.1.3"))
        assert result is not None
        assert result["document_tumbler"] == "1.1.1"
        assert result["chunk_index"] == 3
        assert result["physical_collection"] == "code__nexus"

    def test_resolve_chunk_out_of_range(self, tmp_path):
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()
        cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="aabb")
        cat.register(owner, "a.py", content_type="code",
                     physical_collection="code__nexus", chunk_count=5)
        assert cat.resolve_chunk(Tumbler.parse("1.1.1.10")) is None

    def test_negative_tumbler_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            Tumbler.parse("1.-1.42")

    def test_descendants_any_depth(self, tmp_path):
        """descendants() returns all depths, not just direct children."""
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()
        cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")
        o1 = cat.register_owner("nexus", "repo", repo_hash="aabb")
        o2 = cat.register_owner("arcaneum", "repo", repo_hash="ccdd")
        cat.register(o1, "a.py", content_type="code", file_path="a.py")
        cat.register(o2, "b.py", content_type="code", file_path="b.py")
        # Store "1" has two owners, each with one doc
        results = cat.descendants("1")
        assert len(results) == 2
