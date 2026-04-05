# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.mcp_server import (
    _inject_catalog,
    _reset_singletons,
    catalog_link,
    catalog_link_audit,
    catalog_link_bulk,
    catalog_link_query,
    catalog_links,
    catalog_list,
    catalog_register,
    catalog_resolve,
    catalog_search,
    catalog_show,
    catalog_unlink,
    catalog_update,
)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture(autouse=True)
def clean_singletons():
    _reset_singletons()
    yield
    _reset_singletons()


def _make_test_catalog(tmp_path: Path) -> Catalog:
    cat = Catalog.init(tmp_path / "catalog")
    cat.register_owner("test-repo", "repo", repo_hash="abcd1234")
    return cat


class TestGracefulAbsence:
    def test_search_without_catalog(self):
        result = catalog_search("anything")
        assert isinstance(result, list)
        assert "error" in result[0]
        assert "not initialized" in result[0]["error"].lower()

    def test_list_without_catalog(self):
        result = catalog_list()
        assert "error" in result[0]

    def test_show_without_catalog(self):
        result = catalog_show(tumbler="1.1.1")
        assert "error" in result


class TestCatalogRegister:
    def test_register_and_show(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        result = catalog_register(title="Test Paper", owner="1.1", content_type="paper")
        assert "tumbler" in result
        assert result["tumbler"] == "1.1.1"
        show = catalog_show(tumbler="1.1.1")
        assert show["title"] == "Test Paper"

    def test_register_ghost(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        result = catalog_register(
            title="Ghost Paper", owner="1.1",
            physical_collection="",
        )
        assert result["tumbler"] == "1.1.1"


class TestCatalogSearch:
    def test_search_returns_results(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="authentication module", owner="1.1", content_type="code")
        catalog_register(title="database schema", owner="1.1", content_type="code")
        results = catalog_search("authentication")
        assert len(results) == 1
        assert results[0]["title"] == "authentication module"

    def test_search_no_results(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        results = catalog_search(query="nonexistent")
        assert results == []

    def test_search_by_author(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="Paper A", owner="1.1", author="Fagin")
        catalog_register(title="Paper B", owner="1.1", author="Bernstein")
        results = catalog_search(author="Fagin")
        assert len(results) == 1
        assert results[0]["author"] == "Fagin"

    def test_search_by_corpus(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="A", owner="1.1", corpus="ml")
        catalog_register(title="B", owner="1.1", corpus="systems")
        results = catalog_search(corpus="ml")
        assert len(results) == 1
        assert results[0]["title"] == "A"

    def test_search_by_owner(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        cat.register_owner("other", "repo", repo_hash="xxxx1234")
        catalog_register(title="A", owner="1.1")
        catalog_register(title="B", owner="1.2")
        results = catalog_search(owner="1.1")
        assert len(results) == 1
        assert results[0]["title"] == "A"

    def test_search_requires_some_param(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        results = catalog_search()
        assert "error" in results[0]


class TestCatalogList:
    def test_list_all(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="A", owner="1.1")
        catalog_register(title="B", owner="1.1")
        results = catalog_list()
        assert len(results) == 2

    def test_list_by_owner(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="A", owner="1.1")
        results = catalog_list(owner="1.1")
        assert len(results) == 1


class TestCatalogUpdate:
    def test_update(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="Old Title", owner="1.1")
        result = catalog_update(tumbler="1.1.1", title="New Title")
        assert "tumbler" in result
        show = catalog_show(tumbler="1.1.1")
        assert show["title"] == "New Title"


class TestCatalogLinks:
    def test_link_and_links(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="A", owner="1.1")
        catalog_register(title="B", owner="1.1")
        result = catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
        assert result["from"] == "1.1.1"
        graph = catalog_links(tumbler="1.1.1")
        assert "nodes" in graph
        assert "edges" in graph
        assert len(graph["edges"]) > 0
        # Starting node included in nodes
        node_tumblers = {n["tumbler"] for n in graph["nodes"]}
        assert "1.1.1" in node_tumblers


class TestTitleResolution:
    def test_catalog_link_by_title(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="auth module", owner="1.1", content_type="code")
        catalog_register(title="db schema", owner="1.1", content_type="code")
        result = catalog_link(from_tumbler="auth module", to_tumbler="db schema", link_type="cites")
        assert "error" not in result
        assert result["from"] == "1.1.1"
        assert result["to"] == "1.1.2"

    def test_catalog_link_by_tumbler(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="A", owner="1.1")
        catalog_register(title="B", owner="1.1")
        result = catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
        assert "error" not in result
        assert result["created"] is True

    def test_catalog_link_merge_returns_created_false(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="A", owner="1.1")
        catalog_register(title="B", owner="1.1")
        catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
        result = catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites",
                              created_by="other")
        assert result["created"] is False

    def test_catalog_link_dangling_after_delete(self, tmp_path):
        """Delete a doc, then try to link to it — MCP returns error."""
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="A", owner="1.1")
        catalog_register(title="B", owner="1.1")
        # Delete B
        from nexus.catalog.tumbler import Tumbler
        cat.delete_document(Tumbler.parse("1.1.2"))
        result = catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
        assert "error" in result
        assert "dangling" in result["error"] or "Not found" in result["error"]

    def test_catalog_link_ambiguous_title_returns_error(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="auth module main", owner="1.1")
        catalog_register(title="auth module test", owner="1.1")
        result = catalog_link(from_tumbler="auth module", to_tumbler="1.1.2", link_type="cites")
        assert "error" in result
        assert "Ambiguous" in result["error"]

    def test_catalog_link_not_found_returns_error(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        result = catalog_link(from_tumbler="nonexistent", to_tumbler="1.1.1", link_type="cites")
        assert "error" in result
        assert "Not found" in result["error"]

    def test_catalog_unlink_by_title(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="auth module", owner="1.1")
        catalog_register(title="db schema", owner="1.1")
        catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
        result = catalog_unlink(from_tumbler="auth module", to_tumbler="db schema", link_type="cites")
        assert "error" not in result
        assert result["removed"] == 1

    def test_catalog_links_by_title(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="auth module", owner="1.1")
        catalog_register(title="db schema", owner="1.1")
        catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
        result = catalog_links(tumbler="auth module")
        assert "edges" in result
        assert len(result["edges"]) >= 1


class TestCatalogLinkQuery:
    def test_catalog_link_query_mcp_by_type(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="A", owner="1.1")
        catalog_register(title="B", owner="1.1")
        catalog_register(title="C", owner="1.1")
        catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
        catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.3", link_type="implements")
        results = catalog_link_query(link_type="cites")
        assert len(results) == 1
        assert results[0]["type"] == "cites"

    def test_catalog_link_query_mcp_by_created_by(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="A", owner="1.1")
        catalog_register(title="B", owner="1.1")
        catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites", created_by="bib_enricher")
        results = catalog_link_query(created_by="bib_enricher")
        assert len(results) == 1


class TestCatalogLinkBulk:
    def test_catalog_link_bulk_dry_run(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="A", owner="1.1")
        catalog_register(title="B", owner="1.1")
        catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
        result = catalog_link_bulk(link_type="cites", dry_run=True)
        assert result["removed"] == 1
        assert result["dry_run"] is True
        # Still exists
        links = catalog_link_query(link_type="cites")
        assert len(links) == 1


class TestCatalogLinkAudit:
    def test_catalog_link_audit_mcp(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="A", owner="1.1")
        catalog_register(title="B", owner="1.1")
        catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
        result = catalog_link_audit()
        assert result["total"] == 1
        assert result["by_type"]["cites"] == 1


class TestCatalogResolve:
    def test_resolve_document(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(
            title="A", owner="1.1",
            physical_collection="code__test",
        )
        result = catalog_resolve(tumbler="1.1.1")
        assert "code__test" in result

    def test_resolve_owner(self, tmp_path):
        cat = _make_test_catalog(tmp_path)
        _inject_catalog(cat)
        catalog_register(title="A", owner="1.1", physical_collection="code__test")
        result = catalog_resolve(owner="1.1")
        assert "code__test" in result
