# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-241 Phase 1 Step 2 (nexus-uwggq.1.2): ``_aspect_bearing_collections`` reads one engine route.

``nx enrich aspects-backfill-uri`` (no ``--collection``) needs the set of collection names the catalog
holds. It used to build that set from ``all_documents()``, which an HTTP client pages 1000 documents at a
time. It now reads ``distinct_doc_collections()``: one ``GET /v1/catalog/docs/distinct-collections``.

The first test runs the real ``HttpCatalogClient`` over a fake of 50,000 documents with ``_get`` stubbed to
record paths. The second proves the answer is the one the walk built.
"""
from __future__ import annotations

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.commands.enrich import _aspect_bearing_collections

DOCUMENTS = 50_000
COLLECTIONS = ("code__1-1__voyage-code-3__v1", "docs__1-2__voyage-context-3__v1", "knowledge__1-3__x__v1")


def _collection_of(i: int) -> str:
    """Several distinct collections, plus an empty value for every fifth document."""
    return "" if i % 5 == 0 else COLLECTIONS[i % len(COLLECTIONS)]


class _FakeEngine:
    """The two catalog routes a collection-name read could use, over DOCUMENTS documents."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.docs = [
            {"tumbler": f"1.2.{i + 1}", "title": f"d{i}", "physical_collection": _collection_of(i)}
            for i in range(DOCUMENTS)
        ]

    def get(self, path: str, **params):
        self.paths.append(path)
        if path == "/docs/distinct-collections":
            return {"collections": sorted({d["physical_collection"] for d in self.docs} - {""})}
        if path == "/list":
            off, lim = int(params.get("offset", 0)), int(params.get("limit", 200))
            return {"documents": self.docs[off:off + lim]}
        raise AssertionError(f"unexpected catalog route {path}")


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> _FakeEngine:
    eng = _FakeEngine()
    cat = HttpCatalogClient(base_url="http://enrich.fake", _token="t")
    monkeypatch.setattr(cat, "_get", lambda path, **kw: eng.get(path, **{k: v for k, v in kw.items() if k != "idempotent"}))
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: cat)
    return eng


def test_one_distinct_collections_request_and_no_catalog_walk(engine: _FakeEngine) -> None:
    _aspect_bearing_collections(None)
    assert engine.paths == ["/docs/distinct-collections"], engine.paths


def test_result_is_the_set_the_walk_built(engine: _FakeEngine) -> None:
    walked = sorted({d["physical_collection"] for d in engine.docs if d["physical_collection"]})
    assert walked == sorted(COLLECTIONS)  # the fixture holds every collection, and the empty ones are skipped
    assert _aspect_bearing_collections(None) == walked


def test_an_empty_catalog_reader_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import click

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: None)
    with pytest.raises(click.ClickException, match="Catalog is empty"):
        _aspect_bearing_collections(None)
