# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-217 P2.1 (bead nexus-lqo4p.6) — ``HttpVectorClient.hybrid_search``.

The first client caller of ``POST /v1/vectors/hybrid-search``. The route has
existed since RDR-155 P3 and its only consumer was the cloud deployment, so
nothing in this repository could reach it (BUG-0148's coverage gap).

These tests drive the method into existence: the route it posts to, the body
it builds, the rows it returns verbatim, and — the part a body-only copy of
``search()`` would drop silently — the rerank envelope arriving from the
shared server-side tail. The exhaustive wire pin (seven fields and no more)
and the three side effects in isolation belong to bead .7, which extends this
file and adds the route-label sibling in
``tests/db/test_http_vector_client.py``.

Every test here fakes the MODULE-GLOBAL ``_post``, which is also the seam
bead .10's planted BUG-0148 fixture injects through. An implementation that
reached the network any other way would leave these tests passing over
behaviour they no longer reach.
"""
from __future__ import annotations

import pytest

from nexus.db import http_vector_client as hvc


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(hvc.HttpVectorClient, "__init__", lambda self: None)
    c = hvc.HttpVectorClient()
    c._tenant = "t"
    return c


def _patch_post(monkeypatch, response, captured):
    def fake_post(path, body, tenant=None):
        captured.append({"path": path, "body": body, "tenant": tenant})
        return response
    monkeypatch.setattr(hvc, "_post", fake_post)


ROWS = [
    {"id": "a", "content": "resolve_active_session_id", "distance": 0.31,
     "collection": "code__nexus-1-1", "metadata": {}, "retention": "full"},
]


def test_posts_to_the_hybrid_route_and_returns_rows_verbatim(client, monkeypatch):
    captured: list[dict] = []
    _patch_post(monkeypatch, ROWS, captured)

    rows = client.hybrid_search("resolve_active_session_id",
                                ["code__nexus-1-1"], n_results=5)

    assert captured[0]["path"] == "/v1/vectors/hybrid-search"
    assert captured[0]["tenant"] == "t"
    assert captured[0]["body"] == {
        "query": "resolve_active_session_id",
        "collections": ["code__nexus-1-1"],
        "n_results": 5,
    }
    # Verbatim: the route returns the same flat row shape /search returns and
    # this method adds nothing to it. In particular there is no fusion score
    # to add — the live route selects cosine distance only.
    assert rows == ROWS


def test_optional_fields_are_omitted_when_not_requested(client, monkeypatch):
    captured: list[dict] = []
    _patch_post(monkeypatch, ROWS, captured)

    client.hybrid_search("q", ["code__nexus-1-1"])

    body = captured[0]["body"]
    for absent in ("where", "include_source_uri", "rerank", "rerank_top_k"):
        assert absent not in body, f"{absent} must be omitted when not requested"


def test_where_and_source_uri_reach_the_body(client, monkeypatch):
    captured: list[dict] = []
    _patch_post(monkeypatch, ROWS, captured)

    client.hybrid_search("q", ["code__nexus-1-1"],
                         where={"lang": "python"}, include_source_uri=True)

    body = captured[0]["body"]
    assert body["where"] == {"lang": "python"}
    assert body["include_source_uri"] is True


def test_rerank_envelope_unpacked_exactly_as_search_unpacks_it(client, monkeypatch):
    """Both routes share ONE rerank tail server-side
    (``VectorHandler#sendSearchResult``), so the envelope this method receives
    is byte-identical to ``search()``'s. Assert the two agree rather than
    re-stating what the unpacking should produce: a copy that drifts from
    ``search()`` is the failure this pins.
    """
    envelope = {
        "results": [{"id": "a", "content": "x", "distance": 0.2,
                     "collection": "code__nexus-1-1", "rerank_score": 0.9}],
        "rerank_degraded": False,
        "rerank_model": "rerank-2.5",
    }

    _patch_post(monkeypatch, envelope, [])
    hybrid_meta: dict = {}
    hybrid_rows = client.hybrid_search("q", ["code__nexus-1-1"], rerank=True,
                                       rerank_top_k=3, rerank_meta_out=hybrid_meta)

    _patch_post(monkeypatch, envelope, [])
    search_meta: dict = {}
    search_rows = client.search("q", ["code__nexus-1-1"], rerank=True,
                                rerank_top_k=3, rerank_meta_out=search_meta)

    assert hybrid_rows == search_rows
    assert hybrid_meta == search_meta
    assert hybrid_meta["degraded"] is False
    assert hybrid_meta["model"] == "rerank-2.5"


def test_stale_engine_bare_array_reports_the_same_degrade(client, monkeypatch):
    """An engine predating the fused stage ignores ``rerank`` and returns a
    bare array. One-engine doctrine: report the degrade with the convergence
    remedy, never refuse, never stay silent.
    """
    _patch_post(monkeypatch, ROWS, [])
    meta: dict = {}
    rows = client.hybrid_search("q", ["code__nexus-1-1"], rerank=True,
                                rerank_meta_out=meta)

    assert rows == ROWS
    assert meta["degraded"] is True
    assert meta["stale_engine"] is True
    assert "nx upgrade" in meta["error"]


def test_rerank_top_k_is_only_sent_alongside_rerank(client, monkeypatch):
    """``rerank_top_k`` without ``rerank: true`` is a 400 from the engine
    (``sendSearchResult``: "set both or neither"). The client never builds
    that body — the field is set inside the rerank branch only.
    """
    captured: list[dict] = []
    _patch_post(monkeypatch, ROWS, captured)

    client.hybrid_search("q", ["code__nexus-1-1"], rerank_top_k=3)

    assert "rerank_top_k" not in captured[0]["body"]
    assert "rerank" not in captured[0]["body"]
