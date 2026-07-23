# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-188 P2.1 (nexus-9o6y2.8) — search_cross_corpus server-rerank plumb.

The fan-out asks a capability-marked backend to rerank per collection,
carries ``rerank_score`` into ``SearchResult.metadata``, and collects
per-collection degrade state into ``rerank_meta_out``. Legacy backends
(no ``supports_server_rerank`` marker) are never asked.
"""
from __future__ import annotations

from nexus.search_engine import search_cross_corpus


class _FakeT3:
    """Minimal backend double; ``supports_server_rerank`` set per test."""

    def __init__(self, rows_by_collection, meta_by_collection=None, capable=True):
        self._rows = rows_by_collection
        self._meta = meta_by_collection or {}
        self.calls: list[dict] = []
        if capable:
            self.supports_server_rerank = True

    def search(self, query, collection_names, n_results=10, where=None, **kwargs):
        col = collection_names[0]
        self.calls.append({"col": col, **kwargs})
        meta_out = kwargs.get("rerank_meta_out")
        if kwargs.get("rerank") and meta_out is not None and col in self._meta:
            meta_out.update(self._meta[col])
        return list(self._rows.get(col, []))


def _row(id_, col, distance, score=None):
    r = {"id": id_, "content": f"c {id_}", "distance": distance, "collection": col}
    if score is not None:
        r["rerank_score"] = score
    return r


def test_rerank_requested_per_collection_and_scores_reach_metadata():
    t3 = _FakeT3(
        {"knowledge__a": [_row("k1", "knowledge__a", 0.1, score=0.8)],
         "rdr__b": [_row("r1", "rdr__b", 0.2, score=0.3)]},
    )
    meta: dict = {}
    out = search_cross_corpus(
        "q", ["knowledge__a", "rdr__b"], n_results=5, t3=t3,
        cluster_by=None, rerank=True, rerank_meta_out=meta,
    )

    assert all(c["rerank"] is True for c in t3.calls)
    scores = {r.id: r.metadata.get("rerank_score") for r in out}
    assert scores == {"k1": 0.8, "r1": 0.3}


def test_degrade_meta_collected_per_collection():
    t3 = _FakeT3(
        {"knowledge__a": [_row("k1", "knowledge__a", 0.1)],
         "rdr__b": [_row("r1", "rdr__b", 0.2, score=0.4)]},
        meta_by_collection={
            "knowledge__a": {"degraded": True, "error": "boom"},
            "rdr__b": {"degraded": False, "error": None, "model": "rerank-2.5"},
        },
    )
    meta: dict = {}
    search_cross_corpus(
        "q", ["knowledge__a", "rdr__b"], n_results=5, t3=t3,
        cluster_by=None, rerank=True, rerank_meta_out=meta,
    )

    assert meta["knowledge__a"]["degraded"] is True
    assert meta["rdr__b"]["degraded"] is False


def test_legacy_backend_never_asked_to_rerank():
    t3 = _FakeT3({"knowledge__a": [_row("k1", "knowledge__a", 0.1)]}, capable=False)
    meta: dict = {}
    out = search_cross_corpus(
        "q", ["knowledge__a", "rdr__b"], n_results=5, t3=t3,
        cluster_by=None, rerank=True, rerank_meta_out=meta,
    )

    assert all("rerank" not in c for c in t3.calls)
    assert meta == {}
    assert [r.id for r in out] == ["k1"]


def test_rerank_off_is_the_default_no_kwargs_leak():
    t3 = _FakeT3({"knowledge__a": [_row("k1", "knowledge__a", 0.1)]})
    search_cross_corpus("q", ["knowledge__a"], n_results=5, t3=t3, cluster_by=None)

    assert all("rerank" not in c for c in t3.calls)
