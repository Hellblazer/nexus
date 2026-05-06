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


def test_search_query_plus_content_type_filters_both(cat) -> None:
    """Query + content_type must filter on BOTH (nexus-a414 Part 1).

    Pre-fix: the routing condition treated content_type as a SQL-path
    trigger that ignored ``query``. Live repro: ``catalog_search(
    query="incremental catalog projection rebuild", content_type="rdr")``
    returned the first N rdr entries regardless of query content.

    Expected: results match both filters; non-matching entries excluded
    even when they share the content_type.
    """
    catalog_register(title="incremental catalog projection rebuild",
                     owner="1.1", content_type="rdr")
    catalog_register(title="unrelated rdr about something else",
                     owner="1.1", content_type="rdr")
    catalog_register(title="incremental catalog projection rebuild as code",
                     owner="1.1", content_type="code")  # query match, wrong type

    results = catalog_search(
        query="incremental catalog projection",
        content_type="rdr",
    )
    titles = sorted(r["title"] for r in results if "title" in r)
    # Only the query+type match should land. The unrelated rdr (query miss)
    # and the code entry (type miss) must both be excluded.
    assert titles == ["incremental catalog projection rebuild"], (
        f"query+content_type filtered wrong: {titles!r}"
    )


def test_list_all(cat) -> None:
    catalog_register(title="A", owner="1.1")
    catalog_register(title="B", owner="1.1")
    assert len(catalog_list()) == 2


def test_list_by_owner(cat) -> None:
    catalog_register(title="A", owner="1.1")
    assert len(catalog_list(owner="1.1")) == 1


def test_list_filters_by_content_type_with_pagination(cat) -> None:
    """catalog_list(content_type=X) must push the filter into SQL so
    pagination is correct (nexus-blk2 Part 1).

    Pre-fix: filtering happened in Python AFTER the SQL LIMIT/OFFSET,
    so catalog_list(content_type='rdr', limit=5) returned [] when the
    first 5 entries weren't rdr. Live repro: catalog had 2,270 rdr
    docs but list(content_type='rdr', limit=5) returned [] because
    the LIMIT 5 saw 5 non-rdr docs and the post-filter dropped them
    all.

    This test reproduces the bug at small scale: 10 code docs +
    2 rdr docs, limit=5. Pre-fix list(content_type='rdr', limit=5)
    returns [] because the SQL grabs the first 5 (all code) then the
    Python filter empties them. Post-fix the SQL filter is applied
    first so the rdr docs are returned.
    """
    # Front-load 10 code docs so they fill the LIMIT 5 page.
    for i in range(10):
        catalog_register(title=f"code-{i}", owner="1.1", content_type="code")
    # Add 2 rdr docs at the end (would fall outside LIMIT 5 without
    # SQL-side filter).
    catalog_register(title="rdr-A", owner="1.1", content_type="rdr")
    catalog_register(title="rdr-B", owner="1.1", content_type="rdr")

    rdr_paged = catalog_list(content_type="rdr", limit=5)
    titles = sorted(r["title"] for r in rdr_paged if "title" in r)
    assert titles == ["rdr-A", "rdr-B"], (
        f"content_type filter not pushed into SQL: got {titles!r}"
    )


def test_resolve_dashed_owner_returns_typed_error(cat) -> None:
    """catalog_resolve(owner='1-2188') used to leak ValueError from
    Tumbler.parse (nexus-blk2 Part 2). The dashed format is the
    *collection-prefix* shape produced by ``nx doctor``, not the
    tumbler shape resolve expects. The handler must catch the
    parse failure and return a useful diagnostic instead of the
    raw int() error.
    """
    result = catalog_resolve(owner="1-2188", corpus="code")
    assert len(result) == 1
    assert result[0].startswith("Error:")
    # The error message must point at the actionable cause: dotted-tumbler
    # form expected. Do NOT leak the raw int() ValueError.
    err_lower = result[0].lower()
    assert (
        "tumbler" in err_lower or "dotted" in err_lower
    ), f"unhelpful error: {result[0]!r}"


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
