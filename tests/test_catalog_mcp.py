# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

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
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@test.invalid")


@pytest.fixture(autouse=True)
def clean_singletons():
    _reset_singletons()
    yield
    _reset_singletons()


@pytest.fixture
def cat(tmp_path: Path) -> Catalog:
    c = Catalog.init(tmp_path / "catalog")
    c.register_owner("test-repo", "repo", repo_hash="abcd1234")
    _inject_catalog(c)
    return c


@pytest.mark.parametrize("fn,kwargs", [
    (catalog_search, dict(query="anything")),
    (catalog_list, {}),
])
def test_without_catalog_returns_error(fn, kwargs, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_CATALOG_PATH", "/tmp/nonexistent-catalog-test")
    _reset_singletons()
    assert "error" in fn(**kwargs)[0]


def test_show_without_catalog(monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_CATALOG_PATH", "/tmp/nonexistent-catalog-test")
    _reset_singletons()
    assert "error" in catalog_show(tumbler="1.1.1")


def test_register_and_show(cat) -> None:
    assert catalog_register(title="Test Paper", owner="1.1", content_type="paper")["tumbler"] == "1.1.1"
    assert catalog_show(tumbler="1.1.1")["title"] == "Test Paper"


def test_register_ghost(cat) -> None:
    assert catalog_register(title="Ghost", owner="1.1", physical_collection="")["tumbler"] == "1.1.1"


def test_search_returns_match(cat) -> None:
    catalog_register(title="authentication module", owner="1.1", content_type="code")
    catalog_register(title="database schema", owner="1.1", content_type="code")
    results = catalog_search("authentication")
    assert len(results) == 1 and results[0]["title"] == "authentication module"


def test_search_no_results(cat) -> None:
    assert catalog_search(query="nonexistent") == []


@pytest.mark.parametrize("field,register_val,search_kwarg", [
    ("author", "Fagin", "author"),
    ("corpus", "ml", "corpus"),
])
def test_search_by_field(cat, field, register_val, search_kwarg) -> None:
    catalog_register(title="A", owner="1.1", **{field: register_val})
    catalog_register(title="B", owner="1.1", **{field: "other"})
    results = catalog_search(**{search_kwarg: register_val})
    assert len(results) == 1


def test_search_by_owner(cat) -> None:
    cat.register_owner("other", "repo", repo_hash="xxxx1234")
    catalog_register(title="A", owner="1.1")
    catalog_register(title="B", owner="1.2")
    assert len(catalog_search(owner="1.1")) == 1


def test_search_requires_param(cat) -> None:
    assert "error" in catalog_search()[0]


def test_search_by_content_type_alone(cat) -> None:
    """content_type alone is a valid filter — fixed in 4.9.6 follow-up."""
    catalog_register(title="alpha", owner="1.1", content_type="prose")
    catalog_register(title="beta", owner="1.1", content_type="code")
    catalog_register(title="gamma", owner="1.1", content_type="prose")
    results = catalog_search(content_type="prose")
    assert all("error" not in r for r in results)
    titles = sorted(r["title"] for r in results if "title" in r)
    assert titles == ["alpha", "gamma"]


def test_list_all(cat) -> None:
    catalog_register(title="A", owner="1.1")
    catalog_register(title="B", owner="1.1")
    assert len(catalog_list()) == 2


def test_list_by_owner(cat) -> None:
    catalog_register(title="A", owner="1.1")
    assert len(catalog_list(owner="1.1")) == 1


def test_update(cat) -> None:
    catalog_register(title="Old Title", owner="1.1")
    catalog_update(tumbler="1.1.1", title="New Title")
    assert catalog_show(tumbler="1.1.1")["title"] == "New Title"


def _setup_two_docs(cat) -> None:
    catalog_register(title="A", owner="1.1")
    catalog_register(title="B", owner="1.1")


def test_link_and_links(cat) -> None:
    _setup_two_docs(cat)
    assert catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")["from"] == "1.1.1"
    graph = catalog_links(tumbler="1.1.1")
    assert len(graph["edges"]) > 0 and "1.1.1" in {n["tumbler"] for n in graph["nodes"]}


def test_link_by_title(cat) -> None:
    catalog_register(title="auth module", owner="1.1", content_type="code")
    catalog_register(title="db schema", owner="1.1", content_type="code")
    result = catalog_link(from_tumbler="auth module", to_tumbler="db schema", link_type="cites")
    assert result["from"] == "1.1.1" and result["to"] == "1.1.2"


def test_link_by_tumbler(cat) -> None:
    _setup_two_docs(cat)
    result = catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
    assert result["created"] is True


def test_link_merge_returns_created_false(cat) -> None:
    _setup_two_docs(cat)
    catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
    result = catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites", created_by="other")
    assert result["created"] is False


def test_link_dangling_after_delete(cat) -> None:
    _setup_two_docs(cat)
    from nexus.catalog.tumbler import Tumbler
    cat.delete_document(Tumbler.parse("1.1.2"))
    result = catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
    assert "error" in result


@pytest.mark.parametrize("from_t,err_substr", [
    ("auth module", "Ambiguous"),
    ("nonexistent", "Not found"),
])
def test_link_resolution_errors(cat, from_t, err_substr) -> None:
    if err_substr == "Ambiguous":
        catalog_register(title="auth module main", owner="1.1")
        catalog_register(title="auth module test", owner="1.1")
    result = catalog_link(from_tumbler=from_t, to_tumbler="1.1.1", link_type="cites")
    assert err_substr in result["error"]


def test_unlink_by_title(cat) -> None:
    catalog_register(title="auth module", owner="1.1")
    catalog_register(title="db schema", owner="1.1")
    catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
    result = catalog_unlink(from_tumbler="auth module", to_tumbler="db schema", link_type="cites")
    assert result["removed"] == 1


def test_links_by_title(cat) -> None:
    catalog_register(title="auth module", owner="1.1")
    catalog_register(title="db schema", owner="1.1")
    catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
    assert len(catalog_links(tumbler="auth module")["edges"]) >= 1


# ── Link query ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("query_kw,link_kw", [
    (dict(link_type="cites"), dict(link_type="cites")),
    (dict(created_by="bib_enricher"), dict(link_type="cites", created_by="bib_enricher")),
])
def test_link_query(cat, query_kw, link_kw) -> None:
    _setup_two_docs(cat)
    catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", **link_kw)
    assert len(catalog_link_query(**query_kw)) == 1


def test_link_bulk_dry_run(cat) -> None:
    _setup_two_docs(cat)
    catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
    result = catalog_link_bulk(link_type="cites", dry_run=True)
    assert result["would_remove"] == 1 and result["dry_run"] is True
    assert len(catalog_link_query(link_type="cites")) == 1


def test_link_audit(cat) -> None:
    _setup_two_docs(cat)
    catalog_link(from_tumbler="1.1.1", to_tumbler="1.1.2", link_type="cites")
    result = catalog_link_audit()
    assert result["total"] == 1 and result["by_type"]["cites"] == 1


@pytest.mark.parametrize("resolve_kw", [dict(tumbler="1.1.1"), dict(owner="1.1")])
def test_resolve(cat, resolve_kw) -> None:
    catalog_register(title="A", owner="1.1", physical_collection="code__test")
    assert "code__test" in catalog_resolve(**resolve_kw)
