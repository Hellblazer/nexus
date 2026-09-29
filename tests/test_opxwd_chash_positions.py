# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-opxwd: search's reverse lookup reads (chash, doc_id, position,
chunk_count) from one engine route instead of every referencing document's
full manifest, and falls back to the manifest path on an engine without it.

The parity tests run against the engine substrate: whatever the positions
route stamps must equal what the manifest path stamps for the same hits.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.catalog.factory import make_catalog_reader
from nexus.db.http_vector_client import HttpVectorClient
from nexus.search_engine import _attach_doc_ids_from_catalog, _attach_from_chash_positions
from nexus.types import SearchResult

_COLL = "docs__opxwd-owner__bge-base-en-v15-768__v1"


class _ManifestOnly:
    """The same catalog with the positions route hidden: the manifest path."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name == "chash_positions":
            raise AttributeError(name)
        return getattr(self._inner, name)


def _results(chashes: list[str]) -> list[SearchResult]:
    return [
        SearchResult(id=f"r{i}", content="x", distance=0.1, collection=_COLL,
                     metadata={"chunk_text_hash": c})
        for i, c in enumerate(chashes)
    ]


@pytest.fixture
def indexed(t2_service_env, tmp_path):
    from nexus.doc_indexer import index_markdown  # noqa: PLC0415 — test-local import

    client = HttpVectorClient(tenant=t2_service_env)
    for n in range(2):
        md = tmp_path / f"opxwd-{n}.md"
        md.write_text(f"# opxwd {n}\n\n" + " ".join(f"opxwd doc {n} sentence {j}." for j in range(90)))
        assert index_markdown(md, corpus="opxwd", t3=client, collection_name=_COLL)
    reader = make_catalog_reader()
    docs = [d for d in reader.find_all("opxwd") if getattr(d, "physical_collection", "") == _COLL]
    manifests = reader.get_manifests([str(d.tumbler) for d in docs])
    chashes = [row.chash for rows in manifests.values() for row in rows]
    assert len(manifests) == 2 and len(chashes) >= 4, "fixture must index two multi-chunk docs"
    return reader, manifests, chashes


def test_positions_route_matches_the_manifests(indexed):
    reader, manifests, chashes = indexed
    rows = reader.chash_positions(chashes)
    assert rows is not None
    for row in rows:
        manifest = manifests[row["doc_id"]]
        assert row["chunk_count"] == len(manifest)
        assert row["position"] == next(m.position for m in manifest if m.chash == row["chash"])
    assert {r["chash"] for r in rows} == set(chashes)


def test_search_attach_stamps_the_same_as_the_manifest_path_without_fetching_a_manifest(indexed, t2_service_env, monkeypatch):
    """The routes actually posted are recorded on a fresh client: the positions
    path posts /manifest/chash_positions and never /manifest/get_many."""
    import nexus.catalog.http_catalog_client as hcc  # noqa: PLC0415 — test-local import

    _reader, _manifests, chashes = indexed
    cat = hcc.HttpCatalogClient(tenant=t2_service_env)
    posted: list[str] = []
    real_post = cat._post
    monkeypatch.setattr(cat, "_post", lambda path, body=None, **kw: posted.append(path) or real_post(path, body, **kw))
    try:
        via_manifests = _results(chashes)
        _attach_doc_ids_from_catalog(via_manifests, _ManifestOnly(cat))
        assert "/manifest/get_many" in posted, posted
        posted.clear()

        via_positions = _results(chashes)
        _attach_doc_ids_from_catalog(via_positions, cat)
        assert posted == ["/manifest/chash_positions"], posted
    finally:
        cat.close()
    for a, b in zip(via_manifests, via_positions):
        for key in ("doc_id", "chunk_count", "chunk_index"):
            assert a.metadata.get(key) == b.metadata.get(key), (key, a.metadata, b.metadata)


def test_an_engine_without_the_route_falls_back_and_remembers_for_a_while(indexed, t2_service_env, monkeypatch):
    """A fresh client, not the factory's shared handle: the cached miss must
    not leak into other tests through the process-lifetime singleton."""
    import nexus.catalog.http_catalog_client as hcc  # noqa: PLC0415 — test-local import

    _reader, _manifests, chashes = indexed
    cat = hcc.HttpCatalogClient(tenant=t2_service_env)
    calls: list[str] = []
    real_post = cat._post

    def _post(path, body=None, **kw):
        calls.append(path)
        if path == "/manifest/chash_positions":
            request = httpx.Request("POST", "http://engine/v1/catalog" + path)
            raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
        return real_post(path, body, **kw)

    monkeypatch.setattr(cat, "_post", _post)
    clock = [1000.0]
    monkeypatch.setattr(hcc, "_monotonic", lambda: clock[0])
    try:
        assert cat.chash_positions(chashes) is None
        assert cat.chash_positions(chashes) is None
        assert calls.count("/manifest/chash_positions") == 1, "the 404 is remembered"
        clock[0] += hcc._CHASH_POSITIONS_RETRY_S + 1
        assert cat.chash_positions(chashes) is None
        assert calls.count("/manifest/chash_positions") == 2, "and probed again after the interval"

        results = _results(chashes)
        _attach_doc_ids_from_catalog(results, cat)
        assert all(r.metadata.get("doc_id") and r.metadata.get("chunk_count") for r in results)
    finally:
        cat.close()


def test_a_legacy_doc_id_the_route_did_not_return_leaves_the_manifest_path_to_it():
    class _Cat:
        def chash_positions(self, chashes):
            return [{"chash": "c1", "doc_id": "1.1.1", "position": 0, "chunk_count": 2}]

    results = _results(["c1"])
    results[0].metadata["doc_id"] = "9.9.9"  # a legacy chunk naming another doc
    assert _attach_from_chash_positions(results, ["c1"], _Cat()) is False
    assert "chunk_count" not in results[0].metadata


def test_a_present_but_broken_route_backs_off_briefly(indexed, t2_service_env, monkeypatch):
    """Critique of 6159510c1: a 500 or a malformed page got no backoff, so
    every search paid the failed call and the manifest fallback."""
    import nexus.catalog.http_catalog_client as hcc  # noqa: PLC0415 — test-local import

    _reader, _manifests, chashes = indexed
    cat = hcc.HttpCatalogClient(tenant=t2_service_env)
    calls: list[str] = []
    real_post = cat._post

    def _post(path, body=None, **kw):
        calls.append(path)
        if path == "/manifest/chash_positions":
            return {"rows": [], "count": 3}  # truncated
        return real_post(path, body, **kw)

    monkeypatch.setattr(cat, "_post", _post)
    clock = [1000.0]
    monkeypatch.setattr(hcc, "_monotonic", lambda: clock[0])
    try:
        with pytest.raises(RuntimeError):
            cat.chash_positions(chashes)
        assert cat.chash_positions(chashes) is None
        assert calls.count("/manifest/chash_positions") == 1
        clock[0] += hcc._CHASH_POSITIONS_FAILURE_RETRY_S + 1
        with pytest.raises(RuntimeError):
            cat.chash_positions(chashes)
        assert calls.count("/manifest/chash_positions") == 2
    finally:
        cat.close()


def test_a_chash_in_two_documents_resolves_to_the_first_doc_id():
    """Critique of 6159510c1: the engine orders rows by doc_id (as text), and
    the attach takes the first; the old manifest path picked from a set in
    arbitrary order."""
    class _Cat:
        def chash_positions(self, chashes):
            # The engine's ORDER BY doc_id is text order: "1.1.10" < "1.1.2".
            return [
                {"chash": "c1", "doc_id": "1.1.10", "position": 4, "chunk_count": 9},
                {"chash": "c1", "doc_id": "1.1.2", "position": 0, "chunk_count": 1},
            ]

    results = _results(["c1"])
    assert _attach_from_chash_positions(results, ["c1"], _Cat()) is True
    assert results[0].metadata["doc_id"] == "1.1.10"
    assert (results[0].metadata["chunk_count"], results[0].metadata["chunk_index"]) == (9, 4)
