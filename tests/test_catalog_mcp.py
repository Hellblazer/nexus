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
    catalog_links,
    catalog_list,
    catalog_register,
    catalog_resolve,
    catalog_search,
    catalog_show,
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
        assert "ok" in result or "from" in result
        links = catalog_links(tumbler="1.1.1")
        assert len(links) > 0


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
