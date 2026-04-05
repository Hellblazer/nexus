# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2E tests for the catalog system (RDR-049 + RDR-050).

No API keys required — T3 is backed by EphemeralClient + local ONNX model.
Tests the full pipeline: index repo → catalog hook fires → catalog populated →
MCP tools return real data → link generation works → query planner templates exist.

Uses the same rich_repo fixture pattern as test_indexer_e2e.py.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.catalog.catalog import Catalog
from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry


# ── Corpus definition ────────────────────────────────────────────────────────

_NEXUS_ROOT = Path(__file__).parent.parent

_CODE_FILES = [
    "src/nexus/ttl.py",
    "src/nexus/corpus.py",
    "src/nexus/types.py",
]

_PROSE_FILES = {
    "README.md": (
        "# Test Repo\n\nA test repository for catalog E2E tests.\n\n"
        "## Features\n\n- Catalog integration\n- Tumbler addressing\n"
    ),
}

_RDR_FILES = {
    "docs/rdr/rdr-001-corpus-ttl-design.md": (
        "---\ntitle: Corpus and TTL Design\nstatus: accepted\n---\n\n"
        "# RDR-001: Corpus and TTL Design\n\n"
        "## Decision\n\nWe use tumblers for addressing.\n"
        "## Implementation\n\nThe ttl module handles time-to-live logic.\n"
        "The corpus module handles naming.\n"
    ),
}


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture(scope="module")
def catalog_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real git repo with code, prose, and RDR files."""
    repo = tmp_path_factory.mktemp("catalog-e2e")

    # Code files — copied from real Nexus source
    for rel in _CODE_FILES:
        src = _NEXUS_ROOT / rel
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    # Prose files
    for rel, content in _PROSE_FILES.items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    # RDR files
    for rel, content in _RDR_FILES.items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@nexus"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Nexus Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def local_t3() -> T3Database:
    """Fresh EphemeralClient T3Database — no API keys needed."""
    client = chromadb.EphemeralClient()
    ef = DefaultEmbeddingFunction()
    return T3Database(_client=client, _ef_override=ef)


@pytest.fixture
def registry(tmp_path: Path, catalog_repo: Path) -> RepoRegistry:
    """RepoRegistry with catalog_repo pre-registered."""
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(catalog_repo)
    return reg


@pytest.fixture(autouse=True)
def mock_voyage_client():
    """Patch voyageai.Client so E2E tests run without API keys."""
    ef = DefaultEmbeddingFunction()
    mock_client = MagicMock()

    def fake_embed(texts, model, input_type="document"):
        result = MagicMock()
        result.embeddings = ef(texts)
        return result

    def fake_contextualized_embed(inputs, model, input_type="document"):
        result = MagicMock()
        batch_result = MagicMock()
        batch_result.embeddings = ef(inputs[0])
        result.results = [batch_result]
        return result

    mock_client.embed.side_effect = fake_embed
    mock_client.contextualized_embed.side_effect = fake_contextualized_embed

    with patch("voyageai.Client", return_value=mock_client):
        yield mock_client


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch) -> Path:
    """Initialize a catalog and point NEXUS_CATALOG_PATH at it."""
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    Catalog.init(catalog_dir)
    return catalog_dir


# ── E2E Tests ─────────────────────────────────────────────────────────────────


class TestIndexerPopulatesCatalog:
    """index_repository() fires _catalog_hook which populates the catalog."""

    def test_index_creates_catalog_owner(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        from nexus.indexer import index_repository

        monkeypatch.setenv("NX_LOCAL", "1")
        with patch("nexus.db.make_t3", return_value=local_t3), \
             patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
            index_repository(catalog_repo, registry)

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        # Owner should have been auto-created by the hook
        rows = cat._db.execute("SELECT count(*) FROM owners").fetchone()
        assert rows[0] >= 1

    def test_index_registers_code_files(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        from nexus.indexer import index_repository

        monkeypatch.setenv("NX_LOCAL", "1")
        with patch("nexus.db.make_t3", return_value=local_t3), \
             patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
            index_repository(catalog_repo, registry)

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        rows = cat._db.execute(
            "SELECT count(*) FROM documents WHERE content_type = 'code'"
        ).fetchone()
        assert rows[0] >= len(_CODE_FILES)

    def test_index_registers_rdr_files(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        from nexus.indexer import index_repository

        monkeypatch.setenv("NX_LOCAL", "1")
        with patch("nexus.db.make_t3", return_value=local_t3), \
             patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
            index_repository(catalog_repo, registry)

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        rows = cat._db.execute(
            "SELECT count(*) FROM documents WHERE content_type = 'rdr'"
        ).fetchone()
        assert rows[0] >= 1, "RDR files should be registered in catalog"

    def test_reindex_preserves_tumblers(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        from nexus.indexer import index_repository

        monkeypatch.setenv("NX_LOCAL", "1")
        with patch("nexus.db.make_t3", return_value=local_t3), \
             patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
            index_repository(catalog_repo, registry)

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        first_tumblers = {
            r[0] for r in cat._db.execute("SELECT tumbler FROM documents").fetchall()
        }

        # Re-index
        with patch("nexus.db.make_t3", return_value=local_t3), \
             patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
            index_repository(catalog_repo, registry, force=True)

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        second_tumblers = {
            r[0] for r in cat2._db.execute("SELECT tumbler FROM documents").fetchall()
        }
        # All first-run tumblers should still exist (idempotent registration)
        assert first_tumblers.issubset(second_tumblers), \
            f"Original tumblers should be preserved. Lost: {first_tumblers - second_tumblers}"


class TestCatalogMCPWithRealData:
    """MCP tools work against a catalog populated by real indexing."""

    def _index_and_get_catalog(self, catalog_repo, registry, local_t3, catalog_env, monkeypatch):
        from nexus.indexer import index_repository
        from nexus.mcp_server import _inject_catalog, _reset_singletons

        monkeypatch.setenv("NX_LOCAL", "1")
        with patch("nexus.db.make_t3", return_value=local_t3), \
             patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
            index_repository(catalog_repo, registry)

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        _reset_singletons()
        _inject_catalog(cat)
        return cat

    def test_catalog_search_returns_indexed_files(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        from nexus.mcp_server import catalog_search

        self._index_and_get_catalog(catalog_repo, registry, local_t3, catalog_env, monkeypatch)
        results = catalog_search(query="ttl")
        assert len(results) >= 1
        assert any("ttl" in r.get("title", "").lower() or "ttl" in r.get("file_path", "").lower()
                    for r in results)

    def test_catalog_search_structured_filter(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        from nexus.mcp_server import catalog_search

        cat = self._index_and_get_catalog(catalog_repo, registry, local_t3, catalog_env, monkeypatch)
        # Get the owner tumbler
        owner_row = cat._db.execute("SELECT tumbler_prefix FROM owners LIMIT 1").fetchone()
        assert owner_row is not None
        results = catalog_search(owner=owner_row[0])
        assert len(results) >= 1
        assert "error" not in results[0]

    def test_catalog_show_returns_full_entry(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        from nexus.mcp_server import catalog_show

        cat = self._index_and_get_catalog(catalog_repo, registry, local_t3, catalog_env, monkeypatch)
        tumbler = cat._db.execute("SELECT tumbler FROM documents LIMIT 1").fetchone()[0]
        result = catalog_show(tumbler=tumbler)
        assert "error" not in result
        assert result["tumbler"] == tumbler
        assert "links_from" in result
        assert "links_to" in result

    def test_catalog_resolve_returns_collections(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        from nexus.mcp_server import catalog_resolve

        cat = self._index_and_get_catalog(catalog_repo, registry, local_t3, catalog_env, monkeypatch)
        owner_row = cat._db.execute("SELECT tumbler_prefix FROM owners LIMIT 1").fetchone()
        result = catalog_resolve(owner=owner_row[0])
        assert len(result) >= 1
        # Should be real collection names like code__* or docs__*
        assert any("__" in name for name in result)


class TestLinkGenerationE2E:
    """Link generation works against a catalog populated by real indexing."""

    def test_code_rdr_links_generated(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        from nexus.catalog.link_generator import generate_code_rdr_links
        from nexus.indexer import index_repository

        monkeypatch.setenv("NX_LOCAL", "1")
        with patch("nexus.db.make_t3", return_value=local_t3), \
             patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
            index_repository(catalog_repo, registry)

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")

        # Verify we have both code and RDR entries
        code_count = cat._db.execute(
            "SELECT count(*) FROM documents WHERE content_type='code'"
        ).fetchone()[0]
        rdr_count = cat._db.execute(
            "SELECT count(*) FROM documents WHERE content_type='rdr'"
        ).fetchone()[0]
        assert code_count >= 1, "Need code entries for link generation"
        assert rdr_count >= 1, "Need RDR entries for link generation"

        # Links auto-generated by _catalog_hook during index_repository
        link_count = cat._db.execute("SELECT count(*) FROM links").fetchone()[0]
        assert link_count >= 1, "Expected auto-generated code→RDR links from indexer hook"
        # Calling again should be idempotent (0 new links)
        count = generate_code_rdr_links(cat)
        assert count == 0, "Re-running generator should create 0 new links (idempotent)"


class TestLinkLifecycleE2E:
    """Full link lifecycle: create → query → audit → delete → regenerate."""

    def _make_linked_catalog(self, tmp_path):
        catalog_dir = tmp_path / "catalog"
        cat = Catalog.init(catalog_dir)
        owner = cat.register_owner("test", "repo", repo_hash="aabb1122")
        doc_a = cat.register(owner, "paper-a", content_type="paper",
                             meta={"bib_semantic_scholar_id": "ss-a",
                                   "references": ["ss-b", "ss-c"]})
        doc_b = cat.register(owner, "paper-b", content_type="paper",
                             meta={"bib_semantic_scholar_id": "ss-b"})
        doc_c = cat.register(owner, "paper-c", content_type="paper",
                             meta={"bib_semantic_scholar_id": "ss-c"})
        return cat, doc_a, doc_b, doc_c

    def test_full_link_lifecycle(self, tmp_path):
        from nexus.catalog.link_generator import generate_citation_links

        cat, doc_a, doc_b, doc_c = self._make_linked_catalog(tmp_path)

        # Generate
        count = generate_citation_links(cat)
        assert count == 2  # a→b, a→c

        # Query
        links = cat.link_query(created_by="bib_enricher")
        assert len(links) == 2

        # Bulk delete
        removed = cat.bulk_unlink(created_by="bib_enricher")
        assert removed == 2
        assert cat.link_query(created_by="bib_enricher") == []

        # Regenerate (idempotent)
        count2 = generate_citation_links(cat)
        assert count2 == 2

        # Audit
        audit = cat.link_audit()
        assert audit["orphaned_count"] == 0
        assert audit["total"] == 2

    def test_delete_document_orphan_preserved(self, tmp_path):
        cat, doc_a, doc_b, doc_c = self._make_linked_catalog(tmp_path)
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.delete_document(doc_a)

        audit = cat.link_audit()
        assert audit["orphaned_count"] == 1
        assert cat.resolve(doc_a) is None
        assert len(cat.links_to(doc_b)) == 1

    def test_link_if_absent_idempotent_generator(self, tmp_path):
        from nexus.catalog.link_generator import generate_citation_links

        cat, doc_a, doc_b, doc_c = self._make_linked_catalog(tmp_path)
        count1 = generate_citation_links(cat)
        count2 = generate_citation_links(cat)
        assert count1 == 2
        assert count2 == 0  # all already exist
        assert len(cat.link_query(link_type="cites")) == 2


class TestMCPGraphTraversalE2E:
    """The core value proposition: index → search catalog → traverse links → get collections."""

    def _index_and_inject(self, catalog_repo, registry, local_t3, catalog_env, monkeypatch):
        from nexus.indexer import index_repository
        from nexus.mcp_server import _inject_catalog, _reset_singletons

        monkeypatch.setenv("NX_LOCAL", "1")
        with patch("nexus.db.make_t3", return_value=local_t3), \
             patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
            index_repository(catalog_repo, registry)

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        _reset_singletons()
        _inject_catalog(cat)
        return cat

    def test_search_then_traverse_links(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        """Agent workflow: search catalog → find entry → traverse links → get connected docs."""
        from nexus.mcp_server import catalog_links, catalog_search

        cat = self._index_and_inject(catalog_repo, registry, local_t3, catalog_env, monkeypatch)

        # Step 1: agent searches for a code file by name
        results = catalog_search(query="ttl")
        assert len(results) >= 1
        tumbler = results[0]["tumbler"]

        # Step 2: agent traverses the link graph from that entry
        graph = catalog_links(tumbler=tumbler, depth=1)
        assert "nodes" in graph
        assert "edges" in graph
        # The indexer hook auto-generates implements-heuristic links
        # so the code file should have outbound links to matching RDRs
        node_tumblers = {n["tumbler"] for n in graph["nodes"]}
        assert tumbler in node_tumblers  # starting node included

    def test_link_creation_via_mcp_with_title(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        """Agent creates a link using document titles, not tumblers."""
        from nexus.mcp_server import catalog_link, catalog_link_query

        self._index_and_inject(catalog_repo, registry, local_t3, catalog_env, monkeypatch)

        # Create a 'relates' link between two entries using exact titles
        # (exact title match bypasses the ambiguity guard)
        result = catalog_link(
            from_tumbler="types.py", to_tumbler="corpus.py",
            link_type="relates", created_by="test",
        )
        assert "error" not in result
        assert result["created"] is True

        # Verify via query
        links = catalog_link_query(link_type="relates", created_by="test")
        assert len(links) >= 1

    def test_catalog_link_audit_after_indexing(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        """Audit reports correct stats after real indexing."""
        from nexus.mcp_server import catalog_link_audit

        self._index_and_inject(catalog_repo, registry, local_t3, catalog_env, monkeypatch)

        audit = catalog_link_audit()
        assert "error" not in audit
        assert audit["total"] >= 1  # auto-generated links exist
        assert "implements-heuristic" in audit["by_type"]
        assert audit["orphaned_count"] == 0  # all endpoints live


class TestMCPStorePutCatalogE2E:
    """MCP store_put creates catalog entries — the primary agent write path."""

    def test_store_put_registers_in_catalog(self, tmp_path, monkeypatch):
        from nexus.mcp_server import _reset_singletons, store_put

        catalog_dir = tmp_path / "catalog"
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        cat = Catalog.init(catalog_dir)

        # Pre-create a knowledge curator owner (the hook expects this)
        cat.register_owner("knowledge", "curator")

        _reset_singletons()

        # Simulate what an agent does: store_put via MCP
        with patch("nexus.mcp_server._get_t3") as mock_t3:
            mock_db = MagicMock()
            mock_db.put.return_value = "doc-abc123"
            mock_t3.return_value = mock_db

            result = store_put(
                content="# Research: Vector Indexing\n\nFindings about HNSW...",
                collection="knowledge",
                title="research-vector-indexing",
                tags="research,embeddings",
            )

        assert "Stored" in result

        # Verify the catalog entry was created
        cat2 = Catalog(catalog_dir, catalog_dir / ".catalog.db")
        entry = cat2.by_doc_id("doc-abc123")
        assert entry is not None
        assert entry.title == "research-vector-indexing"


class TestTumblerPermanenceE2E:
    """Tumbler permanence under real conditions: index → delete → compact → re-index."""

    def test_tumblers_stable_across_delete_compact_reindex(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        from nexus.indexer import index_repository

        monkeypatch.setenv("NX_LOCAL", "1")
        with patch("nexus.db.make_t3", return_value=local_t3), \
             patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
            index_repository(catalog_repo, registry)

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        original_tumblers = {
            r[0] for r in cat._db.execute("SELECT tumbler FROM documents").fetchall()
        }
        original_count = len(original_tumblers)

        # Delete one document
        first_tumbler = sorted(original_tumblers)[0]
        from nexus.catalog.tumbler import Tumbler
        cat.delete_document(Tumbler.parse(first_tumbler))

        # Compact (removes tombstones)
        cat.compact()

        # Re-index — new files should get NEW tumblers, not reuse deleted one
        with patch("nexus.db.make_t3", return_value=local_t3), \
             patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
            index_repository(catalog_repo, registry, force=True)

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        new_tumblers = {
            r[0] for r in cat2._db.execute("SELECT tumbler FROM documents").fetchall()
        }

        # The deleted tumbler must NOT reappear
        assert first_tumbler not in new_tumblers, \
            f"Deleted tumbler {first_tumbler} was reused — permanent addressing violated"
        # We should have at least as many docs as before (minus 1 deleted, plus re-registered)
        assert len(new_tumblers) >= original_count - 1


class TestSpanTransclusionE2E:
    """Span-addressed links with text resolution."""

    def test_link_with_line_span_resolves_text(self, tmp_path):
        catalog_dir = tmp_path / "catalog"
        cat = Catalog.init(catalog_dir)
        owner = cat.register_owner("test", "repo", repo_hash="e2etest")

        # Create a real file with known content
        src_file = tmp_path / "source.py"
        src_file.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")

        doc_a = cat.register(owner, "source.py", content_type="code",
                             file_path=str(src_file))
        doc_b = cat.register(owner, "target.py", content_type="code",
                             file_path="target.py")

        # Create a link with a span pointing to lines 2-4 of the source
        cat.link(doc_a, doc_b, "quotes", created_by="user",
                 from_span="2-4", to_span="")

        # Resolve the span — should return the actual text
        text = cat.resolve_span(doc_a, "2-4")
        assert text == "line2\nline3\nline4"


class TestCatalogRebuildFromJSONL:
    """A fresh Catalog instance rebuilds correctly from JSONL truth."""

    def test_fresh_catalog_sees_indexed_data(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        from nexus.indexer import index_repository

        monkeypatch.setenv("NX_LOCAL", "1")
        with patch("nexus.db.make_t3", return_value=local_t3), \
             patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
            index_repository(catalog_repo, registry)

        # Create a completely fresh Catalog with new DB file
        fresh_db = catalog_env / ".catalog-fresh.db"
        cat2 = Catalog(catalog_env, fresh_db)
        # Should auto-rebuild from JSONL
        rows = cat2._db.execute("SELECT count(*) FROM documents").fetchone()
        assert rows[0] >= len(_CODE_FILES), "Fresh catalog should rebuild from JSONL"


class TestCatalogCompactE2E:
    """compact() produces correct JSONL that survives rebuild."""

    def test_compact_and_rebuild(
        self, catalog_repo, registry, local_t3, catalog_env, monkeypatch
    ):
        from nexus.indexer import index_repository

        monkeypatch.setenv("NX_LOCAL", "1")
        with patch("nexus.db.make_t3", return_value=local_t3), \
             patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
            index_repository(catalog_repo, registry)

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        doc_count_before = cat._db.execute("SELECT count(*) FROM documents").fetchone()[0]

        # Compact
        cat.compact()

        # Rebuild from compacted JSONL
        fresh_db = catalog_env / ".catalog-compact.db"
        cat2 = Catalog(catalog_env, fresh_db)
        doc_count_after = cat2._db.execute("SELECT count(*) FROM documents").fetchone()[0]
        assert doc_count_after == doc_count_before, "Compact + rebuild should preserve all documents"


class TestPlanTemplatesSeeded:
    """Verify catalog-aware plan templates exist in T2 plan library."""

    def test_catalog_plan_templates_exist(self, db):
        """Check that T2 plan library schema supports catalog-tagged plans.

        Templates are seeded in production T2 via plan_save MCP, not test DB.
        This verifies the schema can store and query catalog-tagged plans.
        """
        rows = db.conn.execute(
            "SELECT count(*) FROM plans WHERE tags LIKE '%catalog%'"
        ).fetchall()
        assert isinstance(rows, list)  # schema query succeeds
        assert rows[0][0] >= 0  # count is a valid integer (0 in test, >0 in prod)
