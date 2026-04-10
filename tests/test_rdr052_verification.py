# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
from pathlib import Path

import chromadb
import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database
from nexus.mcp_server import _inject_catalog, _inject_t3, _reset_singletons, query


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
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")
    repo_owner = cat.register_owner("nexus", "repo", repo_hash="aabb1122")
    paper_owner = cat.register_owner("papers", "curator")
    cat.register(repo_owner, "indexer.py", content_type="code",
                 file_path="src/nexus/indexer.py", physical_collection="code__nexus", chunk_count=10, author="hal")
    cat.register(repo_owner, "chunker.py", content_type="code",
                 file_path="src/nexus/chunker.py", physical_collection="code__nexus", chunk_count=5, author="hal")
    cat.register(paper_owner, "Schema Mappings and Data Exchange",
                 content_type="paper", physical_collection="knowledge__delos", chunk_count=20, author="Fagin")
    cat.register(paper_owner, "Composing Mappings Among Data Sources",
                 content_type="paper", physical_collection="knowledge__delos", chunk_count=15, author="Fagin")
    cat.register(paper_owner, "Attention Is All You Need",
                 content_type="paper", physical_collection="knowledge__transformers", chunk_count=30, author="Vaswani")
    cat.register(repo_owner, "RDR-052: Catalog-First Query Routing",
                 content_type="rdr", physical_collection="rdr__nexus", chunk_count=8, author="hal")
    fagin_t = cat.find("Schema Mappings")[0].tumbler
    vaswani_t = cat.find("Attention")[0].tumbler
    cat.link(fagin_t, vaswani_t, "cites", created_by="test")
    _inject_catalog(cat)
    return cat


def _seed_templates(tmp_path, monkeypatch):
    db_path = tmp_path / "t2.db"
    monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
    from nexus.commands.catalog import _seed_plan_templates
    return db_path, _seed_plan_templates


# ── Path Routing ────────────────────────────────────────────────────────────


class TestPathRouting:
    @pytest.mark.parametrize("put_col,put_content,query_kw,assert_in", [
        ("knowledge__delos", "chase procedure schema", {"author": "Fagin"}, "knowledge__delos"),
        ("code__nexus", "def index_repo(): pipeline", {"content_type": "code"}, "code__nexus"),
    ])
    def test_catalog_param_routes_correctly(self, t3, catalog, put_col, put_content, query_kw, assert_in):
        t3.put(collection=put_col, content=put_content, title="chunk")
        result = query(question=put_content.split()[0], **query_kw)
        assert not result.startswith("Error:")
        assert assert_in in result

    def test_subtree_routes_to_descendants(self, t3, catalog):
        t3.put(collection="code__nexus", content="tree sitter chunking", title="ts-chunk")
        t3.put(collection="rdr__nexus", content="catalog first query routing", title="rdr-chunk")
        result = query(question="chunking", subtree="1.1")
        assert not result.startswith("Error:")

    def test_follow_links_enriches_collections(self, t3, catalog):
        t3.put(collection="knowledge__delos", content="schema data exchange", title="delos-chunk")
        t3.put(collection="knowledge__transformers", content="attention heads layers", title="trans-chunk")
        result = query(question="schema mappings", follow_links="cites")
        assert not result.startswith("Error:")

    def test_no_catalog_params_backward_compat(self, t3):
        t3.put(collection="knowledge__test", content="vector database embeddings", title="vec-chunk")
        result = query(question="vector database", corpus="knowledge__test")
        assert not result.startswith("Error:")

    @pytest.mark.parametrize("kw,expected_msg", [
        ({"author": "NonexistentPerson"}, "No documents found matching catalog filters"),
        ({"subtree": "9.9"}, "No documents found matching catalog filters"),
    ])
    def test_no_match_returns_clear_message(self, t3, catalog, kw, expected_msg):
        result = query(question="anything", **kw)
        assert expected_msg in result

    def test_subtree_document_level_returns_error(self, t3, catalog):
        result = query(question="anything", subtree="1.1.42")
        assert "document-level address" in result
        assert "1.1" in result

    def test_catalog_params_without_catalog_returns_error(self, t3, monkeypatch):
        import nexus.mcp.core as mod
        monkeypatch.setattr(mod, "_get_catalog", lambda: None)
        result = query(question="test", author="someone")
        assert "catalog not initialized" in result.lower()


class TestReferenceQuestions:
    @pytest.mark.parametrize("question,kw,assert_check", [
        ("papers by Fagin", {"author": "Fagin"}, lambda r: "knowledge__delos" in r),
        ("schema mappings", {"author": "Fagin"}, lambda r: not r.startswith("Error:")),
        ("RDR about streaming", {"content_type": "rdr"}, lambda r: not r.startswith("Error:")),
        ("what cites schema mappings", {"follow_links": "cites"}, lambda r: not r.startswith("Error:")),
        ("nexus architecture", {"subtree": "1.1"}, lambda r: not r.startswith("Error:")),
    ])
    def test_reference_question(self, t3, catalog, question, kw, assert_check):
        # Seed data for all reference questions
        for col, content, title in [
            ("knowledge__delos", "schema mappings chase", "ref1"),
            ("knowledge__delos", "schema mappings data exchange", "ref2"),
            ("rdr__nexus", "streaming pipeline buffer", "ref3"),
            ("knowledge__delos", "data exchange framework", "ref4"),
            ("code__nexus", "module architecture design", "ref5"),
        ]:
            t3.put(collection=col, content=content, title=title)
        result = query(question=question, **kw)
        assert assert_check(result)


# ── Templates and Plans ─────────────────────────────────────────────────────


class TestPlanTemplates:
    def test_seed_creates_five_idempotent(self, tmp_path, monkeypatch):
        db_path, seed_fn = _seed_templates(tmp_path, monkeypatch)
        assert seed_fn() == 5
        assert seed_fn() == 0  # idempotent

    @pytest.mark.parametrize("field,expected", [
        ("tags", lambda v: "builtin-template" in v),
        ("ttl", lambda v: v is None),
    ])
    def test_template_properties(self, tmp_path, monkeypatch, field, expected):
        db_path, seed_fn = _seed_templates(tmp_path, monkeypatch)
        seed_fn()
        db = T2Database(db_path)
        for p in db.list_plans(limit=10):
            assert expected(p[field])
        db.close()


class TestPlanTTL:
    @pytest.mark.parametrize("ttl,expected_ttl", [(30, 30), (None, None)])
    def test_save_plan_ttl(self, tmp_path, ttl, expected_ttl):
        db = T2Database(tmp_path / "t2.db")
        row_id = db.save_plan(query="plan", plan_json='{}', **({} if ttl is None else {"ttl": ttl}))
        row = db.conn.execute("SELECT ttl FROM plans WHERE id = ?", (row_id,)).fetchone()
        assert row[0] == expected_ttl
        db.close()

    @pytest.mark.parametrize("method", ["search_plans", "list_plans"])
    def test_ttl_in_results(self, tmp_path, method):
        db = T2Database(tmp_path / "t2.db")
        db.save_plan(query="ttl plan", plan_json='{}', ttl=7)
        results = getattr(db, method)("ttl plan") if method == "search_plans" else getattr(db, method)()
        assert len(results) == 1
        assert results[0]["ttl"] == 7
        db.close()


class TestPlanTTLEnforcement:
    @pytest.mark.parametrize("method", ["search_plans", "list_plans"])
    def test_expired_plan_excluded(self, tmp_path, method):
        db = T2Database(tmp_path / "t2.db")
        row_id = db.save_plan(query="old cached plan", plan_json='{}', ttl=1)
        db.conn.execute("UPDATE plans SET created_at = datetime('now', '-10 days') WHERE id = ?", (row_id,))
        db.conn.commit()
        results = getattr(db, method)("old cached plan") if method == "search_plans" else getattr(db, method)()
        assert len(results) == 0

    def test_permanent_plan_never_expires(self, tmp_path):
        db = T2Database(tmp_path / "t2.db")
        db.save_plan(query="permanent plan", plan_json='{}')
        db.conn.execute("UPDATE plans SET created_at = datetime('now', '-365 days') WHERE id = 1")
        db.conn.commit()
        assert len(db.list_plans()) == 1

    def test_fresh_plan_with_ttl_included(self, tmp_path):
        db = T2Database(tmp_path / "t2.db")
        db.save_plan(query="fresh cached plan", plan_json='{}', ttl=30)
        assert len(db.search_plans("fresh cached plan")) == 1


class TestFollowLinksFallback:
    def test_follow_links_no_seed_falls_back(self, t3, catalog):
        t3.put(collection="knowledge__delos", content="schema data exchange", title="fb-chunk")
        result = query(question="xyzzy_nonexistent_topic_12345", follow_links="cites", corpus="knowledge__delos")
        assert "No documents found matching catalog filters" not in result


class TestTemplateRetrieval:
    def test_builtin_template_retrievable_by_query(self, tmp_path, monkeypatch):
        db_path, seed_fn = _seed_templates(tmp_path, monkeypatch)
        seed_fn()
        db = T2Database(db_path)
        results = db.search_plans("find documents by author")
        builtin = [r for r in results if "builtin-template" in r.get("tags", "")]
        assert len(builtin) >= 1
        assert "steps" in json.loads(builtin[0]["plan_json"])
        db.close()

    def test_template_plan_json_structure(self, tmp_path, monkeypatch):
        db_path, seed_fn = _seed_templates(tmp_path, monkeypatch)
        seed_fn()
        db = T2Database(db_path)
        results = db.search_plans("citation chain")
        builtin = [r for r in results if "builtin-template" in r.get("tags", "")]
        assert len(builtin) >= 1
        plan = json.loads(builtin[0]["plan_json"])
        assert any("operation" in step for step in plan["steps"])
        db.close()

    def test_all_five_templates_searchable(self, tmp_path, monkeypatch):
        db_path, seed_fn = _seed_templates(tmp_path, monkeypatch)
        seed_fn()
        from nexus.commands.catalog import _PLAN_TEMPLATES
        db = T2Database(db_path)
        for tmpl in _PLAN_TEMPLATES:
            assert db.search_plans(tmpl["query"]), f"Template not found: {tmpl['query']}"
        db.close()


# ── Tumbler Hierarchy ───────────────────────────────────────────────────────


class TestTumblerHierarchy:
    @pytest.mark.parametrize("addr,expected_depth", [
        ("1", 1), ("1.2", 2), ("1.2.42", 3), ("1.2.42.7", 4),
    ])
    def test_depth(self, addr, expected_depth):
        assert Tumbler.parse(addr).depth == expected_depth

    def test_ancestors_includes_self(self):
        t = Tumbler.parse("1.2.42")
        assert t.ancestors()[-1] == t

    @pytest.mark.parametrize("a,b,expected", [
        ("1.1.10", "1.1.20", "1.1"),
        ("1.1.1", "2.1.1", None),
        ("1.1", "2.2", None),
    ])
    def test_lca(self, a, b, expected):
        result = Tumbler.lca(Tumbler.parse(a), Tumbler.parse(b))
        assert result == (Tumbler.parse(expected) if expected else None)

    def test_resolve_chunk_ghost_element(self, tmp_path):
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()
        cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="aabb")
        cat.register(owner, "a.py", content_type="code", physical_collection="code__nexus", chunk_count=5)
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
        cat.register(owner, "a.py", content_type="code", physical_collection="code__nexus", chunk_count=5)
        assert cat.resolve_chunk(Tumbler.parse("1.1.1.10")) is None

    def test_negative_tumbler_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            Tumbler.parse("1.-1.42")

    def test_descendants_any_depth(self, tmp_path):
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()
        cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")
        o1 = cat.register_owner("nexus", "repo", repo_hash="aabb")
        o2 = cat.register_owner("arcaneum", "repo", repo_hash="ccdd")
        cat.register(o1, "a.py", content_type="code", file_path="a.py")
        cat.register(o2, "b.py", content_type="code", file_path="b.py")
        assert len(cat.descendants("1")) == 2
