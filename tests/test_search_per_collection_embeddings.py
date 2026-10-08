# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-92q1p: ``include_embeddings`` on ``POST /v1/vectors/search-per-collection``.

The contradiction check and semantic clustering need each result's stored vector. Before this the
search fetched them with one ``/v1/vectors/get-embeddings`` request per collection in the result
pool (14 to 22 of them, about 1.2 s each on the managed service). With the route returning them on
the rows, the search makes none.

The real ``HttpVectorClient`` and ``search_cross_corpus`` run against a fake transport (the module
``_request`` every ``_post`` funnels through), as ``tests/test_search_per_collection_route.py`` does;
the engine half is pinned by ``VectorHandlerSearchPerCollectionTest`` (Java). The fake engine here
stores one deterministic vector per ``(collection, id)`` and serves it both ways, so a run that
reads the vectors from the rows and a run that fetches them by id see the SAME vectors and must
produce the same flags and clusters.
"""
from __future__ import annotations

import base64
import zlib

import numpy as np
import pytest

import nexus.db.http_vector_client as hvc
import nexus.search_engine as se
from nexus.search_engine import search_cross_corpus

from tests.test_search_per_collection_route import (  # type: ignore[import-not-found]
    _BGE,
    _ROUTE,
    _FakeEngine,
    _cols,
)

_GET_EMBEDDINGS = "/v1/vectors/get-embeddings"
_DIM = 8


def _vector(col: str, rid: str) -> np.ndarray:
    """Ids ``a*`` of one collection sit close together, ``b*`` far from them, so the
    contradiction check (same collection, distance under 0.3, different source_agent) has pairs to
    flag and the clusterer has two groups to find. A small id-dependent offset keeps rows distinct."""
    base = np.zeros(_DIM, dtype=np.float32)
    base[0 if rid.startswith("a") else 1] = 1.0
    rng = np.random.default_rng(zlib.crc32(f"{col}|{rid}".encode()))
    return (base + 0.02 * rng.standard_normal(_DIM)).astype(np.float32)


class _EmbeddingEngine(_FakeEngine):
    """The route fake, plus stored vectors: served on rows when ``serve_embeddings`` and the request
    asked, and by ``get-embeddings`` always."""

    def __init__(self, monkeypatch, data, *, serve_embeddings: bool = True, **kw) -> None:
        super().__init__(monkeypatch, data, **kw)
        self.serve_embeddings = serve_embeddings
        #: ids for which a row carries no embedding_b64 even though others do
        self.omit: set[str] = set()
        #: ids whose embedding_b64 is not decodable
        self.corrupt: set[str] = set()
        monkeypatch.setattr(
            "nexus.search_engine.load_config",
            lambda: {"search": {"contradiction_check": True}},
        )

    def get_embedding_calls(self) -> list[dict]:
        return [b for p, b in self.calls if p == _GET_EMBEDDINGS]

    @staticmethod
    def _row(col: str, rid: str, dist: float) -> dict:
        row = _FakeEngine._row(col, rid, dist)
        row["source_agent"] = "agent-x" if int(rid[1:]) % 2 == 0 else "agent-y"
        return row

    def _request(self, method, path, *, tenant, timeout, body):
        if path == _GET_EMBEDDINGS:
            self.calls.append((path, body))
            col = body["collection"]
            return {"ids": body["ids"],
                    "embeddings": [_vector(col, i).tolist() for i in body["ids"]]}
        return super()._request(method, path, tenant=tenant, timeout=timeout, body=body)

    def _serve_route(self, body: dict) -> dict:
        envelope = super()._serve_route(body)
        if self.serve_embeddings and body.get("include_embeddings"):
            for row in envelope["results"]:
                if row["id"] in self.omit:
                    continue
                if row["id"] in self.corrupt:
                    row["embedding_b64"] = "!!not base64!!"
                    continue
                row["embedding_b64"] = base64.b64encode(
                    _vector(row["collection"], row["id"]).astype("<f4").tobytes()
                ).decode()
            envelope["embedding_encoding"] = "f32-le-b64"
            envelope["embedding_dim"] = _DIM
        return envelope


def _data(cols: list[str]) -> dict[str, list[tuple[str, float]]]:
    # a0..a3 (near each other) and b0..b1, per collection; distances ascending.
    return {c: [("a0", 0.10), ("a1", 0.11), ("a2", 0.12), ("a3", 0.13), ("b0", 0.30), ("b1", 0.31)]
            for c in cols}


def _search(engine: _EmbeddingEngine, cols: list[str], **kw):
    kw.setdefault("threshold_override", float("inf"))
    kw.setdefault("cluster_by", "semantic")
    return search_cross_corpus("q", cols, 20, engine.client, **kw)


def _signature(results) -> list[tuple]:
    """What the two features add to the results: order, contradiction flags, cluster labels."""
    return [
        (r.collection, r.id, bool(r.metadata.get("_contradiction_flag")), r.metadata.get("_cluster_label"))
        for r in results
    ]


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    monkeypatch.setattr(se, "_mixed_model_groups", {})
    monkeypatch.setattr(hvc, "_per_collection_absent_warned", False)
    monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV, raising=False)


class TestTheRowsCarryTheVectors:
    def test_the_search_makes_no_get_embeddings_call_and_asks_the_route_for_them(self, monkeypatch):
        cols = _cols("code", _BGE, 3)
        engine = _EmbeddingEngine(monkeypatch, _data(cols))
        results = _search(engine, cols)

        assert engine.route_calls(), "the route was used"
        assert all(b.get("include_embeddings") is True for b in engine.route_calls())
        assert engine.get_embedding_calls() == [], "zero get-embeddings calls"
        assert len(results) == 18

    def test_same_flags_and_clusters_as_the_fetch_by_id_path(self, monkeypatch):
        cols = _cols("code", _BGE, 3)
        with_rows = _search(_EmbeddingEngine(monkeypatch, _data(cols), serve_embeddings=True), cols)
        fetch_engine = _EmbeddingEngine(monkeypatch, _data(cols), serve_embeddings=False)
        by_fetch = _search(fetch_engine, cols)

        assert len(fetch_engine.get_embedding_calls()) == 3, "control: one fetch per collection"
        assert _signature(with_rows) == _signature(by_fetch)
        # Non-vacuous: the fixture really flags rows and really labels clusters.
        assert any(flag for _, _, flag, _ in _signature(with_rows))
        assert {label for _, _, _, label in _signature(with_rows)} - {None}

    def test_the_vector_is_not_left_on_the_result_metadata(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _EmbeddingEngine(monkeypatch, _data(cols))
        for r in _search(engine, cols):
            assert "embedding_b64" not in r.metadata
            assert all(not isinstance(v, (bytes, bytearray)) for v in r.metadata.values())

    def test_the_field_is_not_requested_when_nothing_needs_the_vectors(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _EmbeddingEngine(monkeypatch, _data(cols))
        monkeypatch.setattr(
            "nexus.search_engine.load_config",
            lambda: {"search": {"contradiction_check": False}},
        )
        _search(engine, cols, cluster_by=None)
        assert all("include_embeddings" not in b for b in engine.route_calls())
        assert engine.get_embedding_calls() == []


class TestAnEngineThatDoesNotReturnThem:
    def test_the_absence_of_the_echo_falls_back_to_get_embeddings(self, monkeypatch):
        cols = _cols("code", _BGE, 3)
        engine = _EmbeddingEngine(monkeypatch, _data(cols), serve_embeddings=False)
        results = _search(engine, cols)
        assert all(b.get("include_embeddings") is True for b in engine.route_calls()), "it still asks"
        assert len(engine.get_embedding_calls()) == 3
        assert any(flag for _, _, flag, _ in _signature(results))

    def test_a_row_without_a_vector_is_fetched_alone_and_the_others_are_not(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        # One engine at a time: constructing a fake engine installs it as the module's transport.
        reference = _search(_EmbeddingEngine(monkeypatch, _data(cols), serve_embeddings=False), cols)
        engine = _EmbeddingEngine(monkeypatch, _data(cols))
        engine.omit = {"a1"}
        engine.corrupt = {"b1"}
        results = _search(engine, cols)

        asked = engine.get_embedding_calls()
        assert sorted(b["collection"] for b in asked) == sorted(cols)
        for b in asked:
            assert sorted(b["ids"]) == ["a1", "b1"], "only the rows that carried no vector"
        assert _signature(results) == _signature(reference)


class TestTheClientSide:
    def _envelope(self, rows, **extra):
        return {"results": rows, "per_collection": [], "per_collection_k": 5, "limit": 5, **extra}

    def test_decodes_and_removes_embedding_b64(self):
        vec = np.arange(4, dtype="<f4")
        row = {"id": "x", "distance": 0.1, "embedding_b64": base64.b64encode(vec.tobytes()).decode()}
        env = self._envelope([row])
        hvc._take_embeddings(env, {"embedding_encoding": "f32-le-b64", "embedding_dim": 4})
        assert "embedding_b64" not in env["results"][0]
        assert env["embedding_dim"] == 4
        assert np.frombuffer(env["result_embeddings"][0], dtype="<f4").tolist() == [0.0, 1.0, 2.0, 3.0]

    @pytest.mark.parametrize("payload", [
        {},                                                                  # an engine that predates the field
        {"embedding_encoding": "f16-le-b64", "embedding_dim": 4},            # an encoding we do not read
        {"embedding_encoding": "f32-le-b64"},                                # no width
        {"embedding_encoding": "f32-le-b64", "embedding_dim": 0},
        {"embedding_encoding": "f32-le-b64", "embedding_dim": True},
    ])
    def test_without_a_usable_echo_the_vectors_are_dropped_and_no_key_is_added(self, payload):
        env = self._envelope([{"id": "x", "distance": 0.1, "embedding_b64": "AAAA"}])
        hvc._take_embeddings(env, payload)
        assert "embedding_b64" not in env["results"][0]
        assert "result_embeddings" not in env and "embedding_dim" not in env

    def test_a_row_of_the_wrong_length_or_bad_base64_gets_none(self):
        good = base64.b64encode(np.zeros(4, dtype="<f4").tobytes()).decode()
        short = base64.b64encode(np.zeros(3, dtype="<f4").tobytes()).decode()
        rows = [{"id": str(i), "distance": 0.1, "embedding_b64": b64}
                for i, b64 in enumerate([good, short, "@@@", None])]
        env = self._envelope(rows)
        hvc._take_embeddings(env, {"embedding_encoding": "f32-le-b64", "embedding_dim": 4})
        assert [e is not None for e in env["result_embeddings"]] == [True, False, False, False]
