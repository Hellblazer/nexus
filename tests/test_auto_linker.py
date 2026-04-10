# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.auto_linker import LinkContext, auto_link, read_link_contexts


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


def _make_catalog(tmp_path: Path) -> Catalog:
    catalog_dir = tmp_path / "catalog"
    cat = Catalog.init(catalog_dir)
    return cat


class TestAutoLink:
    def test_link_context_creates_relates_link(self, tmp_path):
        """Seeding one LinkContext with a valid tumbler creates a relates link."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("test", "curator")
        source_t = cat.register(owner, "Source Doc", content_type="knowledge")
        target_t = cat.register(owner, "Target Doc", content_type="knowledge")

        ctx = LinkContext(target_tumbler=str(target_t), link_type="relates")
        count = auto_link(cat, source_t, [ctx])

        assert count == 1
        links = cat.links_from(source_t, link_type="relates")
        assert len(links) == 1
        assert links[0].created_by == "auto-linker"

    def test_no_link_context_no_crash(self, tmp_path):
        """Empty context list produces no links and no exception."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("test", "curator")
        source_t = cat.register(owner, "Source Doc", content_type="knowledge")

        count = auto_link(cat, source_t, [])

        assert count == 0
        links = cat.links_from(source_t)
        assert links == []

    def test_nonexistent_tumbler_graceful_skip(self, tmp_path):
        """A LinkContext referencing a non-existent tumbler is silently skipped."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("test", "curator")
        source_t = cat.register(owner, "Source Doc", content_type="knowledge")

        ctx = LinkContext(target_tumbler="99.99.99", link_type="relates")
        count = auto_link(cat, source_t, [ctx])

        assert count == 0
        links = cat.links_from(source_t)
        assert links == []

    def test_multiple_contexts_create_multiple_links(self, tmp_path):
        """Two LinkContext objects each create one link — two total."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("test", "curator")
        source_t = cat.register(owner, "Source Doc", content_type="knowledge")
        target1_t = cat.register(owner, "Target One", content_type="knowledge")
        target2_t = cat.register(owner, "Target Two", content_type="knowledge")

        contexts = [
            LinkContext(target_tumbler=str(target1_t), link_type="relates"),
            LinkContext(target_tumbler=str(target2_t), link_type="relates"),
        ]
        count = auto_link(cat, source_t, contexts)

        assert count == 2
        links = cat.links_from(source_t, link_type="relates")
        assert len(links) == 2

    def test_single_entry_multiple_targets(self):
        """read_link_contexts() flattens a targets array into multiple LinkContext objects."""
        entries = [
            {
                "targets": [
                    {"target_tumbler": "1.1.1", "link_type": "relates"},
                    {"target_tumbler": "1.1.2", "link_type": "implements"},
                ]
            }
        ]
        contexts = read_link_contexts(entries)
        assert len(contexts) == 2
        assert contexts[0].target_tumbler == "1.1.1"
        assert contexts[0].link_type == "relates"
        assert contexts[1].target_tumbler == "1.1.2"
        assert contexts[1].link_type == "implements"

    def test_content_wrapper_parsing(self):
        """read_link_contexts() unwraps T1 scratch 'content' string format."""
        import json
        entries = [
            {
                "content": json.dumps({
                    "targets": [{"tumbler": "1.3.1", "link_type": "cites"}],
                    "source_agent": "researcher",
                }),
                "tags": "link-context",
            }
        ]
        contexts = read_link_contexts(entries)
        assert len(contexts) == 1
        assert contexts[0].target_tumbler == "1.3.1"
        assert contexts[0].link_type == "cites"

    def test_default_link_type_is_relates(self):
        """Omitting link_type defaults to 'relates'."""
        entries = [{"targets": [{"tumbler": "1.1.1"}]}]
        contexts = read_link_contexts(entries)
        assert len(contexts) == 1
        assert contexts[0].link_type == "relates"

    def test_idempotent_no_duplicate(self, tmp_path):
        """Calling auto_link twice with the same inputs creates only one link."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("test", "curator")
        source_t = cat.register(owner, "Source Doc", content_type="knowledge")
        target_t = cat.register(owner, "Target Doc", content_type="knowledge")

        ctx = LinkContext(target_tumbler=str(target_t), link_type="relates")
        auto_link(cat, source_t, [ctx])
        count2 = auto_link(cat, source_t, [ctx])

        assert count2 == 0
        links = cat.links_from(source_t, link_type="relates")
        assert len(links) == 1

    def test_created_by_auto_linker(self, tmp_path):
        """Every link created by auto_link has created_by='auto-linker'."""
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("test", "curator")
        source_t = cat.register(owner, "Source Doc", content_type="knowledge")
        target1_t = cat.register(owner, "Target One", content_type="knowledge")
        target2_t = cat.register(owner, "Target Two", content_type="knowledge")

        contexts = [
            LinkContext(target_tumbler=str(target1_t), link_type="relates"),
            LinkContext(target_tumbler=str(target2_t), link_type="implements"),
        ]
        auto_link(cat, source_t, contexts)

        links = cat.links_from(source_t)
        assert len(links) == 2
        for link in links:
            assert link.created_by == "auto-linker"


class TestCatalogAutoLinkIntegration:
    """Integration test: _catalog_auto_link wired through store_put path."""

    def test_store_put_creates_link_from_scratch_context(self, tmp_path):
        """Full pipeline: T1 scratch link-context + store_put → catalog link."""
        import json
        from nexus.db.t1 import T1Database
        from nexus.mcp_server import (
            _catalog_auto_link,
            _inject_catalog,
            _inject_t1,
            _reset_singletons,
        )

        _reset_singletons()

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("knowledge", "curator")
        target_t = cat.register(owner, "Target RDR", content_type="rdr")

        # Register a source doc (simulating what _catalog_store_hook does)
        source_t = cat.register(
            owner, "Agent Finding", content_type="knowledge",
            meta={"doc_id": "test-doc-001"},
        )

        _inject_catalog(cat)

        t1 = T1Database(session_id="test-auto-link-session")
        _inject_t1(t1)

        # Seed link-context in T1 scratch
        t1.put(
            content=json.dumps({
                "targets": [{"target_tumbler": str(target_t), "link_type": "relates"}],
                "source_agent": "developer",
            }),
            tags="link-context",
        )

        count = _catalog_auto_link("test-doc-001")

        assert count == 1
        links = cat.links_from(source_t, link_type="relates")
        assert len(links) == 1
        assert links[0].created_by == "auto-linker"

        _reset_singletons()

    def test_store_put_no_crash_without_context(self, tmp_path):
        """_catalog_auto_link with no link-context in scratch → 0, no crash."""
        from nexus.db.t1 import T1Database
        from nexus.mcp_server import (
            _catalog_auto_link,
            _inject_catalog,
            _inject_t1,
            _reset_singletons,
        )

        _reset_singletons()

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("knowledge", "curator")
        cat.register(
            owner, "Some Doc", content_type="knowledge",
            meta={"doc_id": "test-doc-002"},
        )

        _inject_catalog(cat)
        t1 = T1Database(session_id="test-no-context-session")
        _inject_t1(t1)

        count = _catalog_auto_link("test-doc-002")
        assert count == 0

        _reset_singletons()

    def test_store_put_no_catalog_returns_zero(self):
        """_catalog_auto_link with no catalog → 0."""
        from nexus.mcp_server import _catalog_auto_link, _reset_singletons

        _reset_singletons()
        count = _catalog_auto_link("nonexistent-doc")
        assert count == 0
        _reset_singletons()

    def test_link_context_persists_across_stores(self, tmp_path):
        """Link-context entries apply to every store_put in the session (by design)."""
        import json
        from nexus.db.t1 import T1Database
        from nexus.mcp_server import (
            _catalog_auto_link,
            _inject_catalog,
            _inject_t1,
            _reset_singletons,
        )

        _reset_singletons()

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("knowledge", "curator")
        target_t = cat.register(owner, "Target RDR", content_type="rdr")
        doc1_t = cat.register(
            owner, "Finding One", content_type="knowledge",
            meta={"doc_id": "multi-doc-001"},
        )
        doc2_t = cat.register(
            owner, "Finding Two", content_type="knowledge",
            meta={"doc_id": "multi-doc-002"},
        )

        _inject_catalog(cat)
        t1 = T1Database(session_id="test-multi-store-session")
        _inject_t1(t1)

        # Seed once
        t1.put(
            content=json.dumps({
                "targets": [{"target_tumbler": str(target_t), "link_type": "relates"}],
                "source_agent": "developer",
            }),
            tags="link-context",
        )

        # Two stores in the same session both get linked
        count1 = _catalog_auto_link("multi-doc-001")
        count2 = _catalog_auto_link("multi-doc-002")

        assert count1 == 1
        assert count2 == 1
        assert len(cat.links_from(doc1_t, link_type="relates")) == 1
        assert len(cat.links_from(doc2_t, link_type="relates")) == 1

        _reset_singletons()


# ── LLM hybrid pipeline (RDR-061 E3 Phase 2b, nexus-hg7c) ──────────────────


class _MockLLM:
    """Mock LLM client returning configurable responses."""

    def __init__(self, responses: list[dict] | None = None):
        self.responses = responses or []
        self.calls: list[str] = []
        self._idx = 0

    def classify_relation(self, prompt: str) -> dict:
        self.calls.append(prompt)
        if self._idx < len(self.responses):
            resp = self.responses[self._idx]
            self._idx += 1
            return resp
        return {"relation": "none", "confidence": 0.0}


class _BrokenLLM:
    def classify_relation(self, prompt: str) -> dict:
        raise RuntimeError("API error")


class TestLLMHybridPipeline:
    """Tests for llm_linker.py hybrid extraction pipeline."""

    def test_heuristic_pass_finds_candidates(self, tmp_path):
        """Two knowledge docs about same topic → heuristic finds candidates."""
        from nexus.catalog.llm_linker import heuristic_pass

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("kbase", "curator")
        entry_a = cat.register(owner, "search engine architecture", content_type="knowledge")
        entry_b = cat.register(owner, "search engine optimization", content_type="knowledge")

        entries = cat.all_documents()
        new_entry = next(e for e in entries if str(e.tumbler) == str(entry_a))
        candidates = heuristic_pass(new_entry, entries)
        assert len(candidates) >= 1
        assert any(str(c.target.tumbler) == str(entry_b) for c in candidates)

    def test_heuristic_pass_skips_code_entries(self, tmp_path):
        """Code entries are not returned as candidates."""
        from nexus.catalog.llm_linker import heuristic_pass

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("repo", "repo", repo_hash="abc")
        entry_a = cat.register(owner, "search engine design", content_type="knowledge")
        cat.register(owner, "search_engine", content_type="code", file_path="src/search_engine.py")

        entries = cat.all_documents()
        new_entry = next(e for e in entries if str(e.tumbler) == str(entry_a))
        candidates = heuristic_pass(new_entry, entries)
        assert all(c.target.content_type != "code" for c in candidates)

    def test_llm_verify_skips_high_confidence_heuristic(self, tmp_path):
        """Heuristic score >= 0.8 → auto-link without LLM call."""
        from nexus.catalog.llm_linker import CandidatePair, llm_verify_candidates

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("kbase", "curator")
        entry_a = cat.register(owner, "doc A", content_type="knowledge")
        entry_b = cat.register(owner, "doc B", content_type="knowledge")

        entries = cat.all_documents()
        ea = next(e for e in entries if str(e.tumbler) == str(entry_a))
        eb = next(e for e in entries if str(e.tumbler) == str(entry_b))

        mock_llm = _MockLLM()
        candidates = [CandidatePair(source=ea, target=eb, heuristic_score=0.85)]
        verified = llm_verify_candidates(candidates, mock_llm)

        assert len(verified) == 1
        assert verified[0][1] == "relates"
        assert len(mock_llm.calls) == 0  # no LLM call

    def test_llm_verify_creates_link_above_threshold(self, tmp_path):
        """LLM returning confidence >= 0.7 → link verified."""
        from nexus.catalog.llm_linker import CandidatePair, llm_verify_candidates

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("kbase", "curator")
        entry_a = cat.register(owner, "doc A", content_type="knowledge")
        entry_b = cat.register(owner, "doc B", content_type="knowledge")

        entries = cat.all_documents()
        ea = next(e for e in entries if str(e.tumbler) == str(entry_a))
        eb = next(e for e in entries if str(e.tumbler) == str(entry_b))

        mock_llm = _MockLLM([{"relation": "relates", "confidence": 0.8}])
        candidates = [CandidatePair(source=ea, target=eb, heuristic_score=0.5)]
        verified = llm_verify_candidates(candidates, mock_llm)

        assert len(verified) == 1
        assert verified[0][1] == "relates"
        assert verified[0][2] == 0.8
        assert len(mock_llm.calls) == 1

    def test_llm_verify_skips_below_threshold(self, tmp_path):
        """LLM returning confidence < 0.7 → no link."""
        from nexus.catalog.llm_linker import CandidatePair, llm_verify_candidates

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("kbase", "curator")
        entry_a = cat.register(owner, "doc A", content_type="knowledge")
        entry_b = cat.register(owner, "doc B", content_type="knowledge")

        entries = cat.all_documents()
        ea = next(e for e in entries if str(e.tumbler) == str(entry_a))
        eb = next(e for e in entries if str(e.tumbler) == str(entry_b))

        mock_llm = _MockLLM([{"relation": "relates", "confidence": 0.5}])
        candidates = [CandidatePair(source=ea, target=eb, heuristic_score=0.5)]
        verified = llm_verify_candidates(candidates, mock_llm)

        assert len(verified) == 0

    def test_full_pipeline_creates_links(self, tmp_path):
        """Full pipeline with mock LLM creates links for verified candidates."""
        from nexus.catalog.llm_linker import run_hybrid_pipeline

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("kbase", "curator")
        entry_a = cat.register(owner, "search engine architecture", content_type="knowledge")
        cat.register(owner, "search engine optimization", content_type="knowledge")

        mock_llm = _MockLLM([{"relation": "relates", "confidence": 0.9}])
        count = run_hybrid_pipeline(cat, entry_a, llm=mock_llm, source_excerpt="search engines")
        assert count >= 1

    def test_pipeline_no_llm_only_high_confidence(self, tmp_path):
        """Without LLM, only high-confidence heuristic matches create links."""
        from nexus.catalog.llm_linker import run_hybrid_pipeline

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("kbase", "curator")
        # Identical titles → Jaccard = 1.0 (well above 0.8 threshold)
        entry_a = cat.register(owner, "search engine design", content_type="knowledge")
        cat.register(owner, "search engine design notes", content_type="knowledge")

        count = run_hybrid_pipeline(cat, entry_a, llm=None)
        # "search engine design" tokens: {search, engine, design}
        # "search engine design notes" tokens: {search, engine, design, notes}
        # Jaccard: 3/4 = 0.75 < 0.8 → no auto-link without LLM
        assert count == 0

    def test_pipeline_nonfatal_on_llm_error(self, tmp_path):
        """LLM API error → no exception, just no links."""
        from nexus.catalog.llm_linker import run_hybrid_pipeline

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("kbase", "curator")
        entry_a = cat.register(owner, "search engine architecture", content_type="knowledge")
        cat.register(owner, "search engine optimization", content_type="knowledge")

        count = run_hybrid_pipeline(cat, entry_a, llm=_BrokenLLM())
        assert count == 0  # no crash, no links
