# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpCentroidStore (RDR-156 bead nexus-t1hnc.5).

Uses an httpx.MockTransport fake of the /v1/taxonomy/centroids/* endpoints to
verify: correct HTTP calls, AssignResult mapping, chroma-envelope shape adaptation
(ids/embeddings/metadatas), and the Phase-1-gate error-translation contract
(HTTP 400 -> [] / None; transport/5xx -> raise).

Cross-language end-to-end is the integration test (-m integration).
"""
from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from nexus.db.t2.catalog_taxonomy import AssignResult
from nexus.db.t2.http_centroid_store import HttpCentroidStore

TOKEN = "fake-centroid-token"


# ── In-memory fake centroid service ────────────────────────────────────────────


class _FakeCentroidService:
    """Minimal in-memory implementation of the centroid endpoints."""

    def __init__(self) -> None:
        # (collection, topic_id) -> {embedding, label, doc_count}
        self.rows: dict[tuple[str, int], dict[str, Any]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path.replace("/v1/taxonomy/centroids", "")
        qs = parse_qs(urlparse(str(request.url)).query)
        body = json.loads(request.content) if request.content else {}

        if request.method == "POST" and path == "/upsert":
            for r in body["records"]:
                self.rows[(r["collection"], int(r["topic_id"]))] = {
                    "embedding": r["embedding"],
                    "label": r.get("label"),
                    "doc_count": r.get("doc_count"),
                }
            return _json(200, {"ok": True, "count": len(body["records"])})

        if request.method == "POST" and path == "/query":
            emb = body["embedding"]
            # Simulated dim-mismatch / 5xx sentinels (O2 contract tests).
            if body["collection"] == "dimmismatch":
                return _json(400, {"error": "dimension mismatch"})
            if body["collection"] == "boom":
                return _json(500, {"error": "internal"})
            cross = body.get("cross_collection", False)
            hits = []
            for (coll, tid), rec in self.rows.items():
                if cross and coll == body["collection"]:
                    continue
                if not cross and coll != body["collection"]:
                    continue
                hits.append((tid, _cosine_sim(emb, rec["embedding"])))
            hits.sort(key=lambda h: h[1], reverse=True)
            hits = hits[: body.get("n_results", 1)]
            return _json(200, [{"topic_id": t, "similarity": s} for t, s in hits])

        if request.method == "GET" and path == "/count":
            coll = qs.get("collection", [None])[0]
            n = sum(1 for (c, _t) in self.rows if coll is None or c == coll)
            return _json(200, {"count": n})

        if request.method == "GET" and path == "/dimension":
            if not self.rows:
                return _json(200, {"dimension": -1})
            dim = len(next(iter(self.rows.values()))["embedding"])
            return _json(200, {"dimension": dim})

        if request.method == "GET" and path in ("/by_collection", "/foreign"):
            coll = qs["collection"][0]
            out = []
            for (c, tid), rec in sorted(self.rows.items()):
                match = (c == coll) if path == "/by_collection" else (c != coll)
                if match:
                    out.append({
                        "topic_id": tid, "embedding": rec["embedding"],
                        "label": rec["label"], "collection": c,
                        "doc_count": rec["doc_count"],
                    })
            return _json(200, out)

        if request.method == "POST" and path == "/delete":
            coll, tids = body["collection"], set(body["topic_ids"])
            removed = [k for k in self.rows if k[0] == coll and k[1] in tids]
            for k in removed:
                del self.rows[k]
            return _json(200, {"deleted": len(removed)})

        if request.method == "POST" and path == "/purge":
            coll = body["collection"]
            removed = [k for k in self.rows if k[0] == coll]
            for k in removed:
                del self.rows[k]
            return _json(200, {"deleted": len(removed)})

        return _json(404, {"error": "not found"})


def _json(status: int, payload: Any) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@pytest.fixture
def store() -> HttpCentroidStore:
    fake = _FakeCentroidService()
    s = HttpCentroidStore(
        base_url="http://svc",
        _token=TOKEN,
        _transport=httpx.MockTransport(fake.handler),
    )
    # Expose the fake for assertions that need server-side state.
    s._fake = fake  # type: ignore[attr-defined]
    yield s
    s.close()


# ── Tests ───────────────────────────────────────────────────────────────────────


def test_upsert_count_dimension(store: HttpCentroidStore) -> None:
    store.upsert([
        {"collection": "knowledge__a", "topic_id": 1, "embedding": [1.0, 0.0], "label": "x", "doc_count": 5},
        {"collection": "knowledge__a", "topic_id": 2, "embedding": [0.0, 1.0], "label": "y", "doc_count": 3},
    ])
    assert store.count("knowledge__a") == 2
    assert store.count() == 2
    assert store.dimension() == 2


def test_upsert_empty_is_noop(store: HttpCentroidStore) -> None:
    store.upsert([])
    assert store.count() == 0
    assert store.dimension() == -1


def test_nearest_returns_assignresult(store: HttpCentroidStore) -> None:
    store.upsert([
        {"collection": "k__n", "topic_id": 10, "embedding": [1.0, 0.0], "label": "near", "doc_count": 1},
        {"collection": "k__n", "topic_id": 20, "embedding": [0.0, 1.0], "label": "far", "doc_count": 1},
    ])
    hit = store.nearest([1.0, 0.0], "k__n")
    assert isinstance(hit, AssignResult)
    assert hit.topic_id == 10
    assert hit.similarity == pytest.approx(1.0, abs=1e-6)


def test_ann_query_ordering(store: HttpCentroidStore) -> None:
    store.upsert([
        {"collection": "k__o", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        {"collection": "k__o", "topic_id": 2, "embedding": [0.6, 0.8], "label": "b", "doc_count": 1},
    ])
    hits = store.ann_query([1.0, 0.0], "k__o", n_results=2)
    assert [h.topic_id for h in hits] == [1, 2]
    assert hits[0].similarity > hits[1].similarity


def test_cross_collection_excludes_source(store: HttpCentroidStore) -> None:
    store.upsert([
        {"collection": "k__a", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        {"collection": "k__b", "topic_id": 2, "embedding": [1.0, 0.0], "label": "b", "doc_count": 1},
    ])
    hits = store.ann_query([1.0, 0.0], "k__a", cross_collection=True, n_results=5)
    assert [h.topic_id for h in hits] == [2]


def test_get_by_collection_envelope_shape(store: HttpCentroidStore) -> None:
    # nullable label/doc_count must survive as None in metadatas.
    store.upsert([
        {"collection": "k__e", "topic_id": 7, "embedding": [0.6, 0.8], "label": None, "doc_count": None},
    ])
    env = store.get_by_collection("k__e")
    assert env["ids"] == ["k__e:7"]
    assert env["embeddings"] == [[0.6, 0.8]]
    assert env["metadatas"] == [
        {"topic_id": 7, "label": None, "collection": "k__e", "doc_count": None}
    ]


def test_get_foreign_excludes_given(store: HttpCentroidStore) -> None:
    store.upsert([
        {"collection": "k__fa", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        {"collection": "d__fb", "topic_id": 2, "embedding": [0.0, 1.0], "label": "b", "doc_count": 1},
    ])
    env = store.get_foreign("k__fa")
    assert env["ids"] == ["d__fb:2"]
    assert {m["collection"] for m in env["metadatas"]} == {"d__fb"}


def test_delete_and_purge(store: HttpCentroidStore) -> None:
    store.upsert([
        {"collection": "k__d", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        {"collection": "k__d", "topic_id": 2, "embedding": [0.0, 1.0], "label": "b", "doc_count": 1},
    ])
    assert store.delete_ids("k__d", [1]) == 1
    assert store.count("k__d") == 1
    assert store.purge("k__d") == 1
    assert store.count("k__d") == 0


def test_delete_empty_is_noop(store: HttpCentroidStore) -> None:
    assert store.delete_ids("k__d", []) == 0


def test_o2_dim_mismatch_400_maps_to_none(store: HttpCentroidStore) -> None:
    # HTTP 400 (dim mismatch / bad request) -> ann_query [] -> nearest None.
    assert store.ann_query([1.0, 0.0], "dimmismatch") == []
    assert store.nearest([1.0, 0.0], "dimmismatch") is None


def test_o2_server_error_propagates_not_swallowed(store: HttpCentroidStore) -> None:
    # 5xx is RAISED, not silently None (fail-loud divergence from the oracle swallow).
    with pytest.raises(httpx.HTTPStatusError):
        store.nearest([1.0, 0.0], "boom")
