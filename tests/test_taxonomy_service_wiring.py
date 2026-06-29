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


# ── Incremental-assignment hook (nexus-7ydks C1) ────────────────────────────


def test_assign_batch_hook_routes_through_service_store(monkeypatch):
    """The per-store_put assignment hook must persist via the service store in
    service mode, not bail (the Critical the substantive-critic caught)."""
    import nexus.mcp_infra as mi
    from nexus.db.http_vector_client import HttpVectorClient

    ids = [f"c{i}" for i in range(4)]
    embs = [[float(i), 1.0, 2.0] for i in range(4)]

    # A real-typed (isinstance) HttpVectorClient so is_service_backed() is True,
    # with the two methods the hook calls stubbed.
    class _SvcT3(HttpVectorClient):
        def __init__(self):  # noqa: D107
            pass

        def get_embeddings(self, collection, doc_ids):  # noqa: ANN001
            import numpy as _np
            return _np.asarray(embs, dtype=_np.float32)

    persisted: list[list[dict]] = []

    class _SvcTax:
        def compute_assignments(self, collection, doc_ids, embeddings, *, cross_collection=False):  # noqa: ANN001
            # one assignment per doc for the same-collection pass, none cross
            return [] if cross_collection else [{"doc_id": d, "topic_id": 1} for d in doc_ids]

        def persist_assignments(self, assignments):  # noqa: ANN001
            persisted.append(assignments)
            return len(assignments)

    class _DB:
        taxonomy = _SvcTax()

    monkeypatch.setattr(mi, "get_t3", lambda: _SvcT3())
    # is_local_mode is a local import from nexus.config inside the hook.
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    # t2_index_write just runs the fn with our fake db (no daemon).
    monkeypatch.setattr(mi, "t2_index_write", lambda fn: fn(_DB()))

    # embeddings=None forces the service get_embeddings fetch path.
    mi.taxonomy_assign_batch_hook(ids, "docs__demo", ["t"] * 4, None, None)

    assert persisted, "service-mode hook did not persist any assignments (regressed to bail)"
    assert len(persisted[0]) == 4


def test_assign_batch_hook_refetches_on_empty_placeholder_embeddings(monkeypatch):
    """nexus-reskd: the server-side-embed paths (doc_indexer / streaming PDF)
    pass ``[[], [], ...]`` placeholder embeddings. The hook must RE-FETCH real
    vectors via get_embeddings — the old ``if not svc_embeddings`` was False for
    a non-empty outer list, so zero-dim vectors reached compute_assignments and
    silently produced no assignments."""
    import nexus.mcp_infra as mi
    from nexus.db.http_vector_client import HttpVectorClient

    ids = [f"c{i}" for i in range(3)]
    real = [[float(i), 1.0, 2.0] for i in range(3)]
    fetched: list[list[str]] = []
    seen_embeddings: list = []

    class _SvcT3(HttpVectorClient):
        def __init__(self):  # noqa: D107
            pass

        def get_embeddings(self, collection, doc_ids):  # noqa: ANN001
            import numpy as _np
            fetched.append(list(doc_ids))
            return _np.asarray(real, dtype=_np.float32)

    class _SvcTax:
        def compute_assignments(self, collection, doc_ids, embeddings, *, cross_collection=False):  # noqa: ANN001
            seen_embeddings.append(embeddings)
            return []

        def persist_assignments(self, assignments):  # noqa: ANN001
            return 0

    class _DB:
        taxonomy = _SvcTax()

    monkeypatch.setattr(mi, "get_t3", lambda: _SvcT3())
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    monkeypatch.setattr(mi, "t2_index_write", lambda fn: fn(_DB()))

    # The empty-placeholder shape ([[], [], []]) MUST trigger the re-fetch.
    mi.taxonomy_assign_batch_hook(ids, "docs__demo", ["t"] * 3, [[] for _ in ids], None)

    assert fetched == [ids], "empty-placeholder embeddings did not trigger get_embeddings re-fetch"
    assert seen_embeddings, "compute_assignments was never called"
    # compute_assignments saw the REAL re-fetched 3-dim vectors, not the empties.
    assert all(len(v) == 3 for v in seen_embeddings[0])


# ── split/project service fetch helpers (nexus-9pqoj) ───────────────────────


class _StubWithIds:
    """Service collection stub supporting both ids= (store-get) and paginated get."""

    def __init__(self, ids, docs):  # noqa: ANN001
        self._ids = ids
        self._docs = docs

    def get(self, ids=None, where=None, include=None, limit=10, offset=0):  # noqa: ANN001
        if ids is not None:
            idx = {i: d for i, d in zip(self._ids, self._docs)}
            rids = [i for i in ids if i in idx]
            return {"ids": rids, "documents": [idx[i] for i in rids]}
        sl = slice(offset, offset + limit)
        return {"ids": self._ids[sl], "documents": self._docs[sl]}


class _SplitT3:
    def __init__(self, ids, docs, embs):  # noqa: ANN001
        self._ids, self._docs = ids, docs
        self._embs = {i: e for i, e in zip(ids, embs)}

    def count(self, collection):  # noqa: ANN001
        return len(self._ids)

    def get_or_create_collection(self, name):  # noqa: ANN001
        return _StubWithIds(self._ids, self._docs)

    def get_embeddings(self, collection, ids):  # noqa: ANN001
        return np.asarray([self._embs[i] for i in ids], dtype=np.float32)


def test_svc_fetch_by_ids_aligned():
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
    ids, docs, embs = _corpus(6)
    t3 = _SplitT3(ids, docs, embs)
    g_ids, g_texts, g_embs = HttpTaxonomyStore._svc_fetch_by_ids(t3, "docs__d", ids[:4])
    assert g_ids == ids[:4]
    assert g_texts == docs[:4]
    assert g_embs.shape == (4, 3)


def test_svc_fetch_by_ids_bails_on_misalign():
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
    ids, docs, embs = _corpus(6)

    class _Drop(_SplitT3):
        def get_embeddings(self, collection, ids):  # noqa: ANN001
            return np.asarray(embs[:2], dtype=np.float32)  # short

    g_ids, g_texts, g_embs = HttpTaxonomyStore._svc_fetch_by_ids(_Drop(ids, docs, embs), "docs__d", ids)
    assert g_embs is None  # refuses misaligned


def test_svc_fetch_all_embeddings_paginates():
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
    ids, docs, embs = _corpus(7)
    g_ids, g_embs = HttpTaxonomyStore._svc_fetch_all_embeddings(_SplitT3(ids, docs, embs), "docs__d")
    assert g_ids == ids
    assert g_embs.shape == (7, 3)


def test_svc_fetch_all_embeddings_bails_on_misalign():
    # nexus-9pqoj S1 regression: a count skew between enumerated ids and the
    # returned embeddings must return (ids, None), NOT a silent partial set.
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
    ids, docs, embs = _corpus(7)

    class _Drop(_SplitT3):
        def get_embeddings(self, collection, ids):  # noqa: ANN001
            return np.asarray(embs[:3], dtype=np.float32)  # short

    g_ids, g_embs = HttpTaxonomyStore._svc_fetch_all_embeddings(_Drop(ids, docs, embs), "docs__d")
    assert g_ids == ids
    assert g_embs is None
