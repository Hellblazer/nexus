# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-7ydks: `nx taxonomy discover`/`rebuild` route through the
HttpTaxonomyStore drop-in when the taxonomy store is service-backed.

These are unit tests over the CLI dispatch seam (`discover_for_collection`)
using fakes — no live service required. The end-to-end exercise against the
real Java service lives in tests/db/test_http_taxonomy_store_integration.py.
"""
from __future__ import annotations

import numpy as np

from nexus.commands.taxonomy_cmd import (
    _enumerate_discoverable_collections,
    _fetch_service_vectors,
    discover_for_collection,
)


class _RawColl:
    def __init__(self, name: str, count: int) -> None:
        self.name = name
        self._count = count

    def count(self) -> int:
        return self._count


class _FakeRawClient:
    def __init__(self, colls) -> None:  # noqa: ANN001
        self._colls = colls

    def list_collections(self):
        return self._colls


class _FakeRawT3:
    """T3Database-shaped: exposes ``_client.list_collections()``."""

    def __init__(self, colls) -> None:  # noqa: ANN001
        self._client = _FakeRawClient(colls)


def test_enumerate_discoverable_collections_filters_and_does_not_crash():
    # nexus-7ydks HIGH-1 regression: the helper referenced module-scope
    # `fnmatch` that was only imported inside discover_cmd → NameError when
    # called from the index.py post-processing path. Exercise the filter.
    t3 = _FakeRawT3([
        _RawColl("docs__demo", 12),       # kept
        _RawColl("code__demo", 9),        # excluded by pattern
        _RawColl("docs__small", 3),       # too few
        _RawColl("taxonomy__centroids", 50),  # internal, skipped
    ])
    got = _enumerate_discoverable_collections(t3, exclude=["code__*"])
    assert got == ["docs__demo"]


class _FakeStub:
    """Service collection stub: paginated get(documents) over a fixed corpus."""

    def __init__(self, ids: list[str], docs: list[str]) -> None:
        self._ids = ids
        self._docs = docs

    def get(self, *, include=None, limit=10, offset=0):  # noqa: ANN001
        sl = slice(offset, offset + limit)
        return {"ids": self._ids[sl], "documents": self._docs[sl]}


class _FakeServiceT3:
    """Minimal HttpVectorClient-shaped handle for the discovery fetch path."""

    def __init__(self, ids, docs, embs) -> None:  # noqa: ANN001
        self._ids = ids
        self._docs = docs
        self._embs = np.asarray(embs, dtype=np.float32)

    def count(self, collection: str) -> int:
        return len(self._ids)

    def get_or_create_collection(self, name: str) -> _FakeStub:
        return _FakeStub(self._ids, self._docs)

    def get_embeddings(self, collection: str, ids: list[str]):  # noqa: ANN001
        # Return rows in request order (mirrors the real client contract).
        index = {i: r for i, r in zip(self._ids, self._embs)}
        return np.asarray([index[i] for i in ids], dtype=np.float32)


class _FakeServiceTaxonomy:
    """HttpTaxonomyStore-shaped store: no `_lock`/`conn` → not raw-access.

    Records the args it was dispatched so the test can assert the CLI handed
    over the fetched vectors verbatim.
    """

    def __init__(self) -> None:
        self.discover_calls: list[tuple] = []
        self.rebuild_calls: list[tuple] = []

    def discover_topics(self, collection_name, doc_ids, embeddings, texts, chroma_client=None):  # noqa: ANN001
        self.discover_calls.append((collection_name, list(doc_ids), np.asarray(embeddings), list(texts)))
        return len(set(doc_ids)) and 3  # pretend 3 topics

    def rebuild_taxonomy(self, collection_name, doc_ids, embeddings, texts, chroma_client=None):  # noqa: ANN001
        self.rebuild_calls.append((collection_name, list(doc_ids), np.asarray(embeddings), list(texts)))
        return 2


def _corpus(n: int):
    ids = [f"c{i}" for i in range(n)]
    docs = [f"text {i}" for i in range(n)]
    embs = [[float(i), float(i) + 1.0, 2.0] for i in range(n)]
    return ids, docs, embs


def test_fetch_service_vectors_returns_aligned_arrays():
    ids, docs, embs = _corpus(6)
    t3 = _FakeServiceT3(ids, docs, embs)
    got = _fetch_service_vectors("docs__demo", t3)
    assert got is not None
    g_ids, g_texts, g_embs = got
    assert g_ids == ids
    assert g_texts == docs
    assert g_embs.shape == (6, 3)


def test_fetch_service_vectors_bails_on_embedding_misalignment():
    ids, docs, embs = _corpus(6)

    class _Drops(_FakeServiceT3):
        def get_embeddings(self, collection, ids):  # noqa: ANN001
            return np.asarray(self._embs[:-1], dtype=np.float32)  # one short

    assert _fetch_service_vectors("docs__demo", _Drops(ids, docs, embs)) is None


def test_discover_for_collection_service_routes_to_discover_topics():
    ids, docs, embs = _corpus(8)
    t3 = _FakeServiceT3(ids, docs, embs)
    tax = _FakeServiceTaxonomy()

    n = discover_for_collection("docs__demo", tax, t3, force=False)

    assert n == 3
    assert len(tax.discover_calls) == 1
    assert not tax.rebuild_calls
    col, got_ids, got_embs, got_texts = tax.discover_calls[0]
    assert col == "docs__demo"
    assert got_ids == ids
    assert got_texts == docs
    assert got_embs.shape == (8, 3)


def test_discover_for_collection_service_force_routes_to_rebuild():
    ids, docs, embs = _corpus(8)
    t3 = _FakeServiceT3(ids, docs, embs)
    tax = _FakeServiceTaxonomy()

    n = discover_for_collection("docs__demo", tax, t3, force=True)

    assert n == 2
    assert len(tax.rebuild_calls) == 1
    assert not tax.discover_calls


def test_discover_for_collection_service_too_few_docs_returns_zero():
    ids, docs, embs = _corpus(4)  # < 5
    t3 = _FakeServiceT3(ids, docs, embs)
    tax = _FakeServiceTaxonomy()

    assert discover_for_collection("docs__demo", tax, t3) == 0
    assert not tax.discover_calls
