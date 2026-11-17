# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-241 (nexus-uwggq.1.9): the sibling blank-filter guards of ``list_by_collection`` (nexus-uwggq.1.6).

The engine reads a blank filter as absent (``CatalogHandler.handleList`` / ``handleResolve``,
``CatalogLinksHandler.wantsCombinedList``) and ``HttpCatalogClient._get`` drops a ``""`` or ``None`` param before
it sends, so a blank argument turned a scoped read into a read of the whole tenant (or of every collection). Each
method here raises ``BlankFilterError`` (a ``ValueError``) before any request. The stub replaces ``_get`` and fails
the test if it is called: the refusal comes before the request.

``list_scoped`` is the one method with a legitimate blank: ``""`` is its default and means "not given".
"""
from __future__ import annotations

from typing import Any

import click
import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import _StorageBackendGuardGroup
from nexus.errors import BlankFilterError

# "" (dropped by _get), whitespace of several kinds, and the ideographic space (U+3000), which Java's isBlank
# reads as blank.
BLANKS = ["", " ", "   ", "\t", "\n", "　"]
IDS = [repr(b) for b in BLANKS]


def _client(monkeypatch: pytest.MonkeyPatch) -> tuple[HttpCatalogClient, list[Any]]:
    cat = HttpCatalogClient(base_url="http://blank.fake", _token="t")
    calls: list[Any] = []

    def _boom(path: str, **kw: Any) -> Any:
        calls.append((path, kw))
        raise AssertionError(f"no request may be sent for a blank filter: {path} {kw}")

    monkeypatch.setattr(cat, "_get", _boom)
    return cat, calls


def _recording(cat: HttpCatalogClient, monkeypatch: pytest.MonkeyPatch, reply: Any) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []

    def _get(path: str, **kw: Any) -> Any:
        seen.append((path, kw))
        return reply

    monkeypatch.setattr(cat, "_get", _get)
    return seen


# ── one method per row: (name, call, the blank param named in the message) ──────────────────────────────

SINGLE_ARG = [
    ("by_source_uri", lambda c, v: c.by_source_uri(v), "uri"),
    ("by_corpus", lambda c, v: c.by_corpus(v), "corpus"),
    ("by_content_type", lambda c, v: c.by_content_type(v), "content_type"),
    ("by_owner", lambda c, v: c.by_owner(v), "owner"),
    ("lookup_doc_id_by_collection_and_path", lambda c, v: c.lookup_doc_id_by_collection_and_path(v, "src/a.py"), "collection"),
    ("find_all_by_file_path", lambda c, v: c.find_all_by_file_path(v), "file_path"),
    ("find_all_by_file_path", lambda c, v: c.find_by_file_path(v), "file_path"),  # delegates; the error names the inner call
    ("by_file_path", lambda c, v: c.by_file_path("1.2", v), "file_path"),
    ("by_file_path", lambda c, v: c.by_file_path(v, "src/a.py"), "owner"),
]


@pytest.mark.parametrize("blank", BLANKS, ids=IDS)
@pytest.mark.parametrize("name,call,param", SINGLE_ARG, ids=[f"{r[0]}-{r[2]}" for r in SINGLE_ARG])
def test_a_blank_value_raises_before_any_request(name, call, param, blank, monkeypatch: pytest.MonkeyPatch) -> None:
    cat, calls = _client(monkeypatch)
    with pytest.raises(BlankFilterError, match=f"{name}: {param} is blank"):
        call(cat, blank)
    assert calls == []


@pytest.mark.parametrize("name,call,param", SINGLE_ARG, ids=[f"{r[0]}-{r[2]}" for r in SINGLE_ARG])
def test_none_raises_before_any_request(name, call, param, monkeypatch: pytest.MonkeyPatch) -> None:
    """_get drops a None param too, so a None value would also have widened the read."""
    cat, calls = _client(monkeypatch)
    with pytest.raises(BlankFilterError, match=f"{name}: {param} is blank"):
        call(cat, None)
    assert calls == []


def test_the_error_is_a_value_error() -> None:
    """list_by_collection's callers (and its 1.6 tests) already catch ValueError."""
    assert issubclass(BlankFilterError, ValueError)


# ── the named value is still one filtered request ────────────────────────────────────────────────────────


def test_by_source_uri_named_value_is_one_filtered_request(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = HttpCatalogClient(base_url="http://blank.fake", _token="t")
    seen = _recording(cat, monkeypatch, {"documents": []})
    assert cat.by_source_uri("file:///a.md") is None
    assert seen == [("/list", {"source_uri": "file:///a.md"})]


def test_by_corpus_by_content_type_by_owner_named_value_is_one_filtered_request(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = HttpCatalogClient(base_url="http://blank.fake", _token="t")
    seen = _recording(cat, monkeypatch, {"documents": []})
    cat.by_corpus("rdr")
    cat.by_content_type("paper")
    cat.by_owner("1.2")
    assert seen == [("/list", {"corpus": "rdr"}), ("/list", {"content_type": "paper"}), ("/list", {"owner": "1.2"})]


def test_lookup_named_collection_is_one_resolve_request(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = HttpCatalogClient(base_url="http://blank.fake", _token="t")
    seen = _recording(cat, monkeypatch, {"documents": [{"tumbler": "1.2.3"}]})
    assert cat.lookup_doc_id_by_collection_and_path("code__x", "src/a.py") == "1.2.3"
    assert seen == [("/resolve", {"file_path": "src/a.py", "collection": "code__x"})]


# ── list_scoped: "" is "not given", a whitespace value is refused ────────────────────────────────────────

WHITESPACE = [b for b in BLANKS if b]


@pytest.mark.parametrize("blank", WHITESPACE, ids=[repr(b) for b in WHITESPACE])
def test_list_scoped_whitespace_owner_raises(blank: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace owner with content types passed the old truthiness check; the engine reads it as absent and
    answers the content-type branch, the whole tenant's documents of that type."""
    cat, calls = _client(monkeypatch)
    with pytest.raises(BlankFilterError, match="list_scoped: owner is blank"):
        cat.list_scoped(owner=blank, content_types=("paper",))
    assert calls == []


@pytest.mark.parametrize("blank", WHITESPACE, ids=[repr(b) for b in WHITESPACE])
def test_list_scoped_whitespace_prefix_raises(blank: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace prefix passed the old check; the engine reads it as absent, so it dropped the scope (and with
    nothing else given, listed an unfiltered page)."""
    cat, calls = _client(monkeypatch)
    with pytest.raises(BlankFilterError, match="list_scoped: source_uri_prefix is blank"):
        cat.list_scoped(source_uri_prefix=blank)
    assert calls == []


def test_list_scoped_whitespace_prefix_raises_beside_a_valid_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    cat, calls = _client(monkeypatch)
    with pytest.raises(BlankFilterError, match="source_uri_prefix is blank"):
        cat.list_scoped(owner="1.2", content_types=("paper",), source_uri_prefix="  ")
    assert calls == []


def test_list_scoped_whitespace_owner_raises_beside_a_valid_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    cat, calls = _client(monkeypatch)
    with pytest.raises(BlankFilterError, match="owner is blank"):
        cat.list_scoped(owner=" ", source_uri_prefix="file:///repo/")
    assert calls == []


@pytest.mark.parametrize("blank", BLANKS, ids=IDS)
def test_list_scoped_blank_content_type_entry_raises(blank: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """``CatalogLinksHandler.queryParamAll`` drops a blank ``content_type`` value, so ``("paper", "")`` or ``("",)``
    reached the engine as no content type and an owner read became the owner's documents of every type."""
    cat, calls = _client(monkeypatch)
    with pytest.raises(BlankFilterError, match="list_scoped: content_types entry is blank"):
        cat.list_scoped(owner="1.2", content_types=("paper", blank))
    with pytest.raises(BlankFilterError, match="content_types entry is blank"):
        cat.list_scoped(owner="1.2", content_types=[blank])
    assert calls == []


def test_list_scoped_empty_values_are_not_given_and_still_unscoped_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defaults are "": that stays "not given", so the existing unscoped refusal (a plain ValueError) holds."""
    cat, calls = _client(monkeypatch)
    with pytest.raises(ValueError, match="needs an owner with content_types, or a source_uri_prefix") as info:
        cat.list_scoped()
    assert not isinstance(info.value, BlankFilterError)
    with pytest.raises(ValueError, match="needs an owner with content_types"):
        cat.list_scoped(owner="", content_types=("paper",), source_uri_prefix="")
    assert calls == []


def test_list_scoped_scoped_calls_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = HttpCatalogClient(base_url="http://blank.fake", _token="t")
    seen = _recording(cat, monkeypatch, {"documents": []})
    cat.list_scoped(owner="1.2", content_types=("paper", "rdr"))
    cat.list_scoped(source_uri_prefix="chroma://")
    assert seen == [
        ("/list", {"owner": "1.2", "content_type": ["paper", "rdr"]}),
        ("/list", {"source_uri_prefix": "chroma://"}),
    ]


# ── the CLI boundary: a clean usage error, not a traceback ───────────────────────────────────────────────


def test_the_cli_group_renders_a_blank_filter_as_a_usage_error() -> None:
    @click.group(cls=_StorageBackendGuardGroup)
    def grp() -> None: ...

    @grp.command()
    def boom() -> None:
        HttpCatalogClient(base_url="http://blank.fake", _token="t").by_corpus(" ")

    result = CliRunner().invoke(grp, ["boom"])
    assert result.exit_code == 2, result.output
    assert "Error: by_corpus: corpus is blank" in result.output
    assert isinstance(result.exception, SystemExit), "a click usage error, not a traceback"


def test_list_by_collection_blank_is_the_same_error_type(monkeypatch: pytest.MonkeyPatch) -> None:
    cat, calls = _client(monkeypatch)
    with pytest.raises(BlankFilterError, match="physical_collection is blank"):
        cat.list_by_collection(" ")
    assert calls == []


# ── an OPTIONAL filter: "" and None are "no filter", a whitespace-only value is refused ──────────────────

WS = [b for b in BLANKS if b]
WS_IDS = [repr(b) for b in WS]


def _opt_client(monkeypatch: pytest.MonkeyPatch) -> tuple[HttpCatalogClient, list[Any]]:
    cat, calls = _client(monkeypatch)
    monkeypatch.setattr(cat, "_post", lambda *a, **k: calls.append(("POST", a)) or (_ for _ in ()).throw(
        AssertionError("no POST may be sent for a whitespace filter")))
    return cat, calls


OPTIONAL = [
    ("all_documents-limit", lambda c, v: c.all_documents(limit=5, content_type=v), "all_documents: content_type"),
    ("all_documents-unbounded", lambda c, v: c.all_documents(content_type=v), "all_documents: content_type"),
    ("find", lambda c, v: c.find("q", content_type=v), "find: content_type"),
    ("find_by_title_exact-type", lambda c, v: c.find_by_title_exact("T", content_type=v), "find_by_title_exact: content_type"),
    ("find_by_title_exact-owner", lambda c, v: c.find_by_title_exact("T", owner=v), "find_by_title_exact: owner"),
    ("links_from-type", lambda c, v: c.links_from("1.2.3", link_type=v), "links: link_type"),
    ("links_to-type", lambda c, v: c.links_to("1.2.3", link_type=v), "links: link_type"),
    ("links_from-types", lambda c, v: c.links_from("1.2.3", link_types=["cites", v]), "links: link_types entry"),
    ("links_to-types", lambda c, v: c.links_to("1.2.3", link_types=[v]), "links: link_types entry"),
    ("link_query-from", lambda c, v: c.link_query(from_t=v), "link_query: from_t"),
    ("link_query-to", lambda c, v: c.link_query(to_t=v), "link_query: to_t"),
    ("link_query-type", lambda c, v: c.link_query(link_type=v), "link_query: link_type"),
    ("link_query-by", lambda c, v: c.link_query(created_by=v), "link_query: created_by"),
    ("link_query-before", lambda c, v: c.link_query(created_at_before=v), "link_query: created_at_before"),
    ("link_query-direction", lambda c, v: c.link_query(direction=v), "link_query: direction"),
    ("link_query-tumbler", lambda c, v: c.link_query(tumbler=v), "link_query: tumbler"),
    ("gc_audit_list-collection", lambda c, v: c.gc_audit_list(collection=v), "gc_audit_list: collection"),
    ("gc_audit_list-operation", lambda c, v: c.gc_audit_list(operation=v), "gc_audit_list: operation"),
    ("list_collections-state", lambda c, v: c.list_collections(lifecycle_state=v), "list_collections: lifecycle_state"),
    ("list_collections-type", lambda c, v: c.list_collections(content_type=v), "list_collections: content_type"),
    ("coverage_by_content_type", lambda c, v: c.coverage_by_content_type(v), "coverage_by_content_type: owner_prefix"),
    ("bulk_unlink-from", lambda c, v: c.bulk_unlink(from_t=v), "bulk_unlink: from_t"),
    ("bulk_unlink-to", lambda c, v: c.bulk_unlink(to_t=v), "bulk_unlink: to_t"),
    ("bulk_unlink-type", lambda c, v: c.bulk_unlink(link_type=v), "bulk_unlink: link_type"),
    ("bulk_unlink-by", lambda c, v: c.bulk_unlink(created_by=v), "bulk_unlink: created_by"),
    ("bulk_unlink-before", lambda c, v: c.bulk_unlink(created_at_before=v), "bulk_unlink: created_at_before"),
    ("bulk_unlink-dry", lambda c, v: c.bulk_unlink(link_type=v, dry_run=True), "bulk_unlink: link_type"),
    ("bulk_unlink-beside-a-filter", lambda c, v: c.bulk_unlink(link_type="cites", created_by=v), "bulk_unlink: created_by"),
]


@pytest.mark.parametrize("blank", WS, ids=WS_IDS)
@pytest.mark.parametrize("name,call,msg", OPTIONAL, ids=[r[0] for r in OPTIONAL])
def test_a_whitespace_optional_filter_raises_before_any_request(name, call, msg, blank, monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine reads a whitespace value as absent, so it would drop the scope (``bulk_unlink`` with no other
    filter deleted every link)."""
    cat, calls = _opt_client(monkeypatch)
    with pytest.raises(BlankFilterError, match=msg):
        call(cat, blank)
    assert calls == []


def test_empty_optional_filters_are_the_unfiltered_call(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = HttpCatalogClient(base_url="http://blank.fake", _token="t")
    seen = _recording(cat, monkeypatch, {"documents": [], "links": [], "links_from": [], "links_to": [], "collections": [], "coverage": []})
    cat.all_documents(limit=5, content_type="")
    cat.link_query(from_t="", link_type=None, limit=3)
    cat.links_from("1.2.3", link_type="")
    cat.coverage_by_content_type("")
    cat.list_collections(None, "")
    assert seen == [
        ("/list", {"limit": 5, "offset": 0}),
        ("/link_query", {"limit": 3, "offset": 0}),
        ("/links", {"tumbler": "1.2.3", "direction": "out"}),
        ("/coverage", {}),
        ("/collections/list", {"lifecycle_state": None, "content_type": ""}),
    ]


def test_bulk_unlink_with_a_real_filter_and_an_empty_one_is_one_post(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = HttpCatalogClient(base_url="http://blank.fake", _token="t")
    posts: list[Any] = []
    monkeypatch.setattr(cat, "_post", lambda path, body=None, **k: posts.append((path, body)) or {"deleted": 2})
    assert cat.bulk_unlink(link_type="cites", created_by="") == 2
    assert posts == [("/unlink", {"link_type": "cites"})]


def test_bulk_unlink_with_no_filter_is_still_refused() -> None:
    cat = HttpCatalogClient(base_url="http://blank.fake", _token="t")
    with pytest.raises(ValueError, match="at least one filter") as info:
        cat.bulk_unlink()
    assert not isinstance(info.value, BlankFilterError)


def test_a_named_optional_filter_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = HttpCatalogClient(base_url="http://blank.fake", _token="t")
    seen = _recording(cat, monkeypatch, {"documents": [], "links": [], "links_to": [], "entries": [], "coverage": []})
    cat.link_query(link_type="cites", tumbler="1.2.3", limit=1)
    cat.links_to("1.2.3", link_types=["cites", "quotes"])
    cat.gc_audit_list(collection="c", operation="purge")
    cat.coverage_by_content_type("1.2")
    assert seen[0] == ("/link_query", {"limit": 1, "offset": 0, "link_type": "cites", "tumbler": "1.2.3"})
    assert seen[1] == ("/links", {"tumbler": "1.2.3", "direction": "in", "link_types": "cites,quotes"})
    assert seen[2] == ("/gc_audit/list", {"collection": "c", "operation": "purge", "limit": 100, "offset": 0})
    assert seen[3] == ("/coverage", {"owner_prefix": "1.2"})
