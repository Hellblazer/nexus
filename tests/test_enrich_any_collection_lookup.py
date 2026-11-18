# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-241 (nexus-uwggq.1.9): the enrich target resolver never sends a blank collection.

``_resolve_enrich_targets`` used to call ``lookup_doc_id_by_collection_and_path(collection_name or "", path)``,
leaning on the engine reading a blank collection as "any collection" (``handleResolve`` falls to
``documentsByFilePath``). The client now refuses a blank collection, so "any collection" is the explicit
``find_by_file_path`` call, which reaches the same engine query. These tests drive the resolver through a real
``HttpCatalogClient`` whose ``_get`` is stubbed, so the wire request is what is asserted.
"""
from __future__ import annotations

from typing import Any

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.tumbler import Tumbler
from nexus.commands.enrich import _resolve_enrich_targets
from nexus.errors import BlankFilterError


def _reader(monkeypatch: pytest.MonkeyPatch, answers: dict[tuple[str, tuple], Any]) -> tuple[HttpCatalogClient, list[tuple[str, dict]]]:
    cat = HttpCatalogClient(base_url="http://enrich.fake", _token="t")
    seen: list[tuple[str, dict]] = []

    def _get(path: str, **kw: Any) -> Any:
        seen.append((path, kw))
        assert kw.get("collection", "x").strip(), f"a blank collection was sent: {path} {kw}"
        return answers.get((path, tuple(sorted(kw.items()))), {"documents": []})

    monkeypatch.setattr(cat, "_get", _get)
    return cat, seen


DOC = {"tumbler": "1.2.3", "file_path": "docs/a.md", "title": "A", "physical_collection": "docs__c"}


def test_no_collection_resolves_by_path_across_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    cat, seen = _reader(monkeypatch, {("/list", (("file_path", "docs/a.md"),)): {"documents": [DOC]}})
    out = _resolve_enrich_targets(cat, "", "A", ["docs/a.md"], Tumbler)
    assert [str(t) for t in out] == ["1.2.3"]
    assert seen == [("/list", {"file_path": "docs/a.md"})]


def test_no_collection_tries_the_relative_form_of_an_absolute_path(monkeypatch: pytest.MonkeyPatch) -> None:
    cat, seen = _reader(monkeypatch, {("/list", (("file_path", "docs/a.md"),)): {"documents": [DOC]}})
    out = _resolve_enrich_targets(cat, "", "A", ["/docs/a.md"], Tumbler)
    assert [str(t) for t in out] == ["1.2.3"]
    assert seen == [("/list", {"file_path": "/docs/a.md"}), ("/list", {"file_path": "docs/a.md"})]


def test_no_collection_and_no_path_match_sends_no_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    cat, seen = _reader(monkeypatch, {})
    cat.find_by_title_exact = lambda *a, **k: []  # type: ignore[method-assign]
    assert _resolve_enrich_targets(cat, "", "A", ["docs/missing.md"], Tumbler) == []
    assert all(path != "/resolve" for path, _ in seen), seen


def test_a_collection_still_uses_the_collection_scoped_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    cat, seen = _reader(monkeypatch, {("/resolve", (("collection", "docs__c"), ("file_path", "docs/a.md"))): {"documents": [DOC]}})
    out = _resolve_enrich_targets(cat, "docs__c", "A", ["docs/a.md"], Tumbler)
    assert [str(t) for t in out] == ["1.2.3"]
    assert seen == [("/resolve", {"file_path": "docs/a.md", "collection": "docs__c"})]


def test_a_blank_path_variant_is_skipped_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """``"/"`` has no relative form: the stripped variant is blank and must not become an unfiltered /list."""
    cat, seen = _reader(monkeypatch, {})
    cat.find_by_title_exact = lambda *a, **k: []  # type: ignore[method-assign]
    _resolve_enrich_targets(cat, "", "A", ["/"], Tumbler)
    by_path = [kw for path, kw in seen if path == "/list" and "file_path" in kw]
    assert by_path == [{"file_path": "/"}]
    assert all(kw for _, kw in seen), "every request carries a filter"


def test_a_whitespace_collection_is_refused_not_widened(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truthy blank collection name used to resolve across every collection; the resolver now lets the client's
    refusal propagate (the enrich hook around it is best-effort and degrades to False)."""
    cat, seen = _reader(monkeypatch, {})
    with pytest.raises(BlankFilterError):
        _resolve_enrich_targets(cat, "  ", "A", ["docs/a.md"], Tumbler)
    assert seen == []
