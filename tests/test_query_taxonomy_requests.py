# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The MCP ``query`` tool must not spend taxonomy round trips on labels.

nexus-tnwm2. Measured on the managed cloud: one ``query`` took ~4 s and made
27 HTTP requests, 7 of them ``GET /v1/taxonomy/topics/by_id``, one per topic
in the result window. ``query`` called ``search_cross_corpus`` without a
``cluster_by``, so it inherited the default ``"semantic"``: topic grouping,
which looks up every topic's label. ``query`` never reads the label (it
groups by document and orders by ``hybrid_score``), so the lookups were pure
cost. The engine has no batched by-id topic route, so the fix is to not ask.

These tests drive the real tool and the real ``HttpTaxonomyStore`` over a
recording ``httpx.MockTransport`` and count requests by path.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
from nexus.mcp import core

_N_TOPICS = 7
_COLLECTION = "knowledge__tnwm2__minilm-l6-v2__v1"


class _FakeT3:
    """Canned T3: one row per topic, plus embeddings for the contradiction check."""

    _voyage_client = "fake"

    def search(self, query, collection_names, n_results=10, where=None, **_kw):
        return [
            {"id": f"chunk-{i}", "content": f"text {i}", "distance": 0.1 + 0.01 * i,
             "title": f"doc {i}", "chunk_text_hash": f"{i:064d}"}
            for i in range(_N_TOPICS)
        ]

    def get_embeddings(self, collection_name, ids):
        rng = np.random.default_rng(7)
        return rng.random((len(ids), 4), dtype=np.float32)


class _RecordingEngine:
    """Answers the taxonomy routes a query touches and records every request."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/taxonomy")
        self.paths.append(path)
        if path == "/assignments/for_docs":
            ids = json.loads(request.content)["doc_ids"]
            # Every chunk lands in its own topic: the worst case for a per-topic loop.
            return httpx.Response(200, json=[
                {"doc_id": d, "topic_id": 100 + int(d.rsplit("-", 1)[1])} for d in ids
            ])
        if path == "/links/pairs":
            return httpx.Response(200, json=[])
        if path == "/topics/by_id":
            tid = int(request.url.params["id"])
            return httpx.Response(200, json={"id": tid, "label": f"topic {tid}"})
        return httpx.Response(404, json={"error": f"unexpected {path}"})


@pytest.fixture
def wired(monkeypatch):
    engine = _RecordingEngine()
    store = HttpTaxonomyStore(
        base_url="http://engine.invalid",
        _token="t",
        client=httpx.Client(transport=httpx.MockTransport(engine.handler)),
    )

    monkeypatch.setattr(core, "_get_t3", lambda: _FakeT3())
    monkeypatch.setattr(core, "_get_catalog", lambda **_kw: None)
    monkeypatch.setattr(core, "_search_taxonomy", lambda: store)
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, _t3, **_kw: [_COLLECTION])
    # Hybrid scoring asks the T3 collection registry whether a result's
    # collection is code (a cached /v1/vectors/stats read): keep it offline.
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", lambda name: None)
    monkeypatch.setattr("nexus.config.load_config", lambda **_kw: {"search": {}})
    monkeypatch.setattr("nexus.search_engine.load_config", lambda **_kw: {"search": {}})
    return engine


class _CountingCatalog:
    """The catalog surface a plain-corpus ``query`` touches, counting calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def docs_and_manifests_for_chashes(self, chashes):
        self.calls.append("docs_and_manifests_for_chashes")
        docs = {c: [f"1.1.{int(c)}"] for c in chashes}
        manifests = {f"1.1.{int(c)}": [SimpleNamespace(chash=c, position=0)] for c in chashes}
        return docs, manifests

    def docs_for_chashes(self, chashes):
        self.calls.append("docs_for_chashes")
        return {c: [f"1.1.{int(c)}"] for c in chashes}

    def get_manifests(self, doc_ids):
        self.calls.append("get_manifests")
        return {d: [object(), object()] for d in doc_ids}

    def get_manifest(self, doc_id):
        self.calls.append("get_manifest")
        return [object(), object()]

    def resolve_many(self, doc_ids):
        self.calls.append("resolve_many")
        return {}


def test_query_makes_no_per_topic_requests(wired: _RecordingEngine) -> None:
    out = core.query("anything", structured=True, limit=10)

    assert isinstance(out, dict) and len(out["ids"]) == _N_TOPICS, out
    by_id = [p for p in wired.paths if p == "/topics/by_id"]
    assert by_id == [], f"{len(by_id)} per-topic by_id requests: {wired.paths}"


def test_query_still_applies_the_topic_boost_with_two_taxonomy_requests(
    wired: _RecordingEngine,
) -> None:
    """Dropping the labels must not drop the boost: assignments and link pairs
    are still read, once each, however many topics the window holds."""
    core.query("anything", structured=True, limit=10)

    assert sorted(wired.paths) == ["/assignments/for_docs", "/links/pairs"], wired.paths


def test_query_reads_document_manifests_in_one_batch(wired: _RecordingEngine, monkeypatch) -> None:
    """Chunk counts for the result documents come from ONE get_manifests call,
    not one get_manifest per document (7 documents here)."""
    cat = _CountingCatalog()
    monkeypatch.setattr(core, "_get_catalog", lambda **_kw: cat)

    out = core.query("anything", limit=10)

    assert "[2 chunks]" in out, out
    assert "get_manifest" not in cat.calls, cat.calls
    assert cat.calls.count("get_manifests") == 1, cat.calls
