# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction

import json
import os
from pathlib import Path

import pytest

from nexus.catalog.tumbler import Tumbler
from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database
from nexus.mcp_server import _inject_t3, _reset_singletons, query
from tests.conftest import make_vector_test_client


@pytest.fixture(autouse=True)
def _reset():
    _reset_singletons()
    yield
    _reset_singletons()


@pytest.fixture()
def t3():
    client = make_vector_test_client()
    ef = MiniLMDirectEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    _inject_t3(db)
    return db


@pytest.fixture()
def catalog(tmp_path, monkeypatch):
    # nexus-i711w terminal deletion: seeds through ActiveCatalog (the live
    # service catalog) — the local Catalog.init arm is gone.
    from tests._catalog_fixture_ops import ActiveCatalog
    catalog_dir = tmp_path / "catalog"
    cat = ActiveCatalog()
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
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return cat


def _seed_templates(tmp_path, monkeypatch):
    db_path = tmp_path / "t2.db"
    monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
    from nexus.commands.catalog import _seed_plan_templates
    return db_path, _seed_plan_templates


# ── Path Routing ────────────────────────────────────────────────────────────


class TestPathRouting:
    """RDR-156 P4.2c (nexus-2bqpn): this file's ``t3`` fixture injects a
    non-service ``InMemoryVectorClient`` (``make_vector_test_client()``).
    Catalog-param routing used to reach real content through the app-side
    dance against exactly this substrate; the dance is deleted, so catalog
    params + non-service T3 now loud-reject. Real-content routing coverage
    for the surviving (service-mode) combined-query path lives in
    tests/test_query_repoint.py."""

    def test_subtree_on_non_service_t3_is_loud_rejected(self, t3, catalog):
        t3.put(collection="code__nexus", content="tree sitter chunking", title="ts-chunk")
        t3.put(collection="rdr__nexus", content="catalog first query routing", title="rdr-chunk")
        result = query(question="chunking", subtree="1.1")
        assert result.startswith("Error:")
        assert "service mode" in result

    def test_follow_links_on_non_service_t3_is_loud_rejected(self, t3, catalog):
        t3.put(collection="knowledge__delos", content="schema data exchange", title="delos-chunk")
        t3.put(collection="knowledge__transformers", content="attention heads layers", title="trans-chunk")
        result = query(question="schema mappings", follow_links="cites")
        assert result.startswith("Error:")
        assert "service mode" in result

    def test_no_catalog_params_backward_compat(self, t3):
        t3.put(collection="knowledge__test", content="vector database embeddings", title="vec-chunk")
        result = query(question="vector database", corpus="knowledge__test")
        assert not result.startswith("Error:")

    @pytest.mark.parametrize("kw", [
        {"author": "NonexistentPerson"},
        {"subtree": "9.9"},
    ])
    def test_catalog_params_on_non_service_t3_is_loud_rejected(self, t3, catalog, kw):
        result = query(question="anything", **kw)
        assert result.startswith("Error:")
        assert "service mode" in result

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
    """RDR-156 P4.2c (nexus-2bqpn): same non-service ``t3`` fixture as
    ``TestPathRouting`` above — catalog params now loud-reject rather than
    reaching real content through the deleted dance."""

    @pytest.mark.parametrize("question,kw", [
        ("schema mappings", {"author": "Fagin"}),
        ("RDR about streaming", {"content_type": "rdr"}),
        ("what cites schema mappings", {"follow_links": "cites"}),
        ("nexus architecture", {"subtree": "1.1"}),
    ])
    def test_reference_question_on_non_service_t3_is_loud_rejected(self, t3, catalog, question, kw):
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
        assert result.startswith("Error:")
        assert "service mode" in result


# ── Templates and Plans ─────────────────────────────────────────────────────


class TestPlanTemplates:
    def test_seed_creates_five_idempotent(self, tmp_path, monkeypatch):
        # RDR-092 Phase 0a: 12 YAML builtins (9 RDR-078 + 3 RDR-092
        # migrations). RDR-097 added 2 more (hybrid-factual-lookup,
        # traverse-then-generate). RDR-098 added abstract-themes
        # (CheapRAG community pattern). Total: 15. nexus-h33x8.6 a1 added
        # 2 single-query-step fast-path templates (document-discovery,
        # corpus-coverage-check). Total: 17. Legacy _PLAN_TEMPLATES
        # retired.
        db_path, seed_fn = _seed_templates(tmp_path, monkeypatch)
        assert seed_fn() == len(list(
            (Path(__file__).parent.parent
             / 'conexus' / 'plans' / 'builtin').glob('*.yml')
        ))
        assert seed_fn() == 0  # idempotent

    @pytest.mark.parametrize("field,expected", [
        ("tags", lambda v: "builtin-template" in v),
        ("ttl", lambda v: v is None),
    ])
    def test_template_properties(self, tmp_path, monkeypatch, field, expected):
        db_path, seed_fn = _seed_templates(tmp_path, monkeypatch)
        seed_fn()
        db = T2Database(db_path)
        for p in db.list_plans(limit=20):
            assert expected(p[field])
        db.close()


class TestPlanTTL:
    @pytest.mark.parametrize("ttl,expected_ttl", [(30, 30), (None, None)])
    def test_save_plan_ttl(self, tmp_path, ttl, expected_ttl):
        db = T2Database(tmp_path / "t2.db")
        row_id = db.save_plan(query="plan", plan_json='{}', **({} if ttl is None else {"ttl": ttl}))
        # Read back through the public surface (works on both the SQLite
        # and the service-backed substrate — RDR-155 P4b P0a').
        row = db.plans.get_plan(row_id)
        assert row is not None
        assert row["ttl"] == expected_ttl
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
    @staticmethod
    def _save_backdated(db, query: str, *, days_old: int, ttl: int | None):
        """Land a plan whose created_at is *days_old* days in the past.

        The store has no raw handle; use the fidelity-import surface
        (``import_plan``) which persists ``created_at`` verbatim
        (RDR-155 P4b P0a'; the SQLite raw-conn backdate leg died with the
        =sqlite opt-out).
        """
        from datetime import UTC, datetime, timedelta

        created_at = (
            datetime.now(UTC) - timedelta(days=days_old)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        return db.plans.import_plan(
            project="", query=query, plan_json='{}', outcome="success",
            tags="", created_at=created_at, ttl=ttl,
        )

    @pytest.mark.parametrize("method", ["search_plans", "list_plans"])
    def test_expired_plan_excluded(self, tmp_path, method):
        db = T2Database(tmp_path / "t2.db")
        self._save_backdated(db, "old cached plan", days_old=10, ttl=1)
        results = getattr(db, method)("old cached plan") if method == "search_plans" else getattr(db, method)()
        assert len(results) == 0

    def test_permanent_plan_never_expires(self, tmp_path):
        db = T2Database(tmp_path / "t2.db")
        self._save_backdated(db, "permanent plan", days_old=365, ttl=None)
        assert len(db.list_plans()) == 1

    def test_fresh_plan_with_ttl_included(self, tmp_path):
        db = T2Database(tmp_path / "t2.db")
        db.save_plan(query="fresh cached plan", plan_json='{}', ttl=30)
        assert len(db.search_plans("fresh cached plan")) == 1


class TestFollowLinksFallback:
    def test_follow_links_no_seed_on_non_service_t3_is_loud_rejected(self, t3, catalog):
        """RDR-156 P4.2c (nexus-2bqpn): this used to pin the dance's
        no-seed-found fallback to broad search (never a catalog-filter
        'no match' message). The dance is deleted — catalog params on this
        non-service fixture now loud-reject before seed resolution is ever
        attempted, so the assertion is the loud-reject contract, not the
        absence of one specific unrelated message (a pre-fix version of
        this test would have passed vacuously on that weaker check)."""
        t3.put(collection="knowledge__delos", content="schema data exchange", title="fb-chunk")
        result = query(question="xyzzy_nonexistent_topic_12345", follow_links="cites", corpus="knowledge__delos")
        assert result.startswith("Error:")
        assert "service mode" in result
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
        # RDR-092 Phase 0a: dimensional YAML uses ``tool:`` step keys,
        # not the legacy ``operation:`` key from the retired
        # _PLAN_TEMPLATES shape.
        assert any("tool" in step for step in plan["steps"])
        db.close()

    def test_migrated_legacy_shapes_searchable(self, tmp_path, monkeypatch):
        """The 3 legacy _PLAN_TEMPLATES shapes retained by RDR-092 Phase 0a
        are discoverable by their natural-language description and by
        their migrated strategy name.
        """
        db_path, seed_fn = _seed_templates(tmp_path, monkeypatch)
        seed_fn()
        db = T2Database(db_path)
        # Natural-language probe text derived from the migrated YAML
        # descriptions.
        probes = [
            "find documents attributed",   # find-by-author
            "trace the citation chain",    # citation-traversal
            "search within a single content type",  # type-scoped-search
        ]
        for probe in probes:
            results = db.search_plans(probe, limit=10)
            assert results, f"migrated template not searchable: {probe!r}"
            assert any(
                "builtin-template" in (r.get("tags") or "") for r in results
            ), f"no builtin-tagged result for {probe!r}"
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
        # nexus-i711w: runs against the live catalog (fresh per-test tenant),
        # so the deterministic first-owner/first-doc tumblers still hold.
        from tests._catalog_fixture_ops import ActiveCatalog
        cat = ActiveCatalog()
        owner = cat.register_owner("nexus", "repo", repo_hash="aabb")
        cat.register(owner, "a.py", content_type="code", physical_collection="code__nexus", chunk_count=5)
        result = cat.resolve_chunk(Tumbler.parse("1.1.1.3"))
        assert result is not None
        assert result["document_tumbler"] == "1.1.1"
        assert result["chunk_index"] == 3
        assert result["physical_collection"] == "code__nexus"

    def test_resolve_chunk_out_of_range(self, tmp_path):
        from tests._catalog_fixture_ops import ActiveCatalog
        cat = ActiveCatalog()
        owner = cat.register_owner("nexus", "repo", repo_hash="aabb")
        cat.register(owner, "a.py", content_type="code", physical_collection="code__nexus", chunk_count=5)
        assert cat.resolve_chunk(Tumbler.parse("1.1.1.10")) is None

    def test_negative_tumbler_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            Tumbler.parse("1.-1.42")

    def test_descendants_any_depth(self, tmp_path):
        from tests._catalog_fixture_ops import ActiveCatalog
        cat = ActiveCatalog()
        o1 = cat.register_owner("nexus", "repo", repo_hash="aabb")
        o2 = cat.register_owner("arcaneum", "repo", repo_hash="ccdd")
        cat.register(o1, "a.py", content_type="code", file_path="a.py")
        cat.register(o2, "b.py", content_type="code", file_path="b.py")
        assert len(cat.descendants("1")) == 2
