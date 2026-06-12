# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-156 P3 (nexus-70r3c.12): HttpVectorClient stats surface.

``collection_stats()`` is the one-round-trip client for
``GET /v1/vectors/stats`` (the ``nexus.collection_vector_stats``
SECURITY INVOKER view — tombstone-filtered live counts).
``list_collections()`` is repointed onto it, restoring the
``{"name", "count"}`` T3Database parity shape whose absence was a live
``KeyError: 'count'`` in ``nx collection list`` on every service-mode box.

Deployment-skew contract: a pre-catalog-005 JAR has no /stats route →
404 → ``list_collections`` falls back to ``/collections`` + per-collection
``/count`` (raw counts) instead of breaking the surface.
"""
from __future__ import annotations

from typing import Any

import pytest

from nexus.db.http_vector_client import HttpVectorClient, VectorServiceError

STATS_PATH = "/v1/vectors/stats"
COLLECTIONS_PATH = "/v1/vectors/collections"


def _patch_get(monkeypatch, handler) -> list[str]:
    """Patch module-level _get, recording requested paths."""
    paths: list[str] = []

    def fake_get(path: str, *, tenant: str = "default") -> Any:
        paths.append(path)
        return handler(path)

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)
    return paths


class TestVectorServiceErrorCode:
    def test_code_attribute_set(self):
        assert VectorServiceError("boom", code=404).code == 404

    def test_code_defaults_to_none(self):
        assert VectorServiceError("boom").code is None


class TestCollectionStats:
    def test_returns_stats_rows_from_stats_endpoint(self, monkeypatch):
        # Neutral model tokens on purpose: the RDR-109 mode lint flags
        # voyage-* names in tests without a cloud_mode declaration, and
        # these mocked rows never touch an embedder.
        rows = [
            {"name": "code__a__model-x__v1", "dim": 1024, "count": 7,
             "last_write": "2026-06-11T00:00:00Z"},
            {"name": "knowledge__b__minilm-l6-v2-384__v1", "dim": 384, "count": 2,
             "last_write": "2026-06-10T00:00:00Z"},
        ]
        paths = _patch_get(monkeypatch, lambda p: rows)

        got = HttpVectorClient().collection_stats()

        assert got == rows
        assert paths == [STATS_PATH]

    def test_non_list_response_returns_empty(self, monkeypatch):
        _patch_get(monkeypatch, lambda p: {"error": "weird"})
        assert HttpVectorClient().collection_stats() == []

    def test_propagates_service_error(self, monkeypatch):
        def handler(path: str) -> Any:
            raise VectorServiceError("GET /stats → HTTP 503: down", code=503)

        _patch_get(monkeypatch, handler)
        with pytest.raises(VectorServiceError):
            HttpVectorClient().collection_stats()


class TestListCollectionsViaStats:
    def test_name_count_parity_shape_sorted(self, monkeypatch):
        rows = [
            {"name": "z__coll", "dim": 1024, "count": 5, "last_write": "x"},
            {"name": "a__coll", "dim": 384, "count": 3, "last_write": "y"},
        ]
        _patch_get(monkeypatch, lambda p: rows)

        got = HttpVectorClient().list_collections()

        # Exactly the T3Database parity shape, name ascending.
        assert got == [
            {"name": "a__coll", "count": 3},
            {"name": "z__coll", "count": 5},
        ]

    def test_multidim_collection_collapses_counts_summed(self, monkeypatch):
        rows = [
            {"name": "mixed__coll", "dim": 384, "count": 2, "last_write": "x"},
            {"name": "mixed__coll", "dim": 1024, "count": 3, "last_write": "y"},
        ]
        _patch_get(monkeypatch, lambda p: rows)

        got = HttpVectorClient().list_collections()

        assert got == [{"name": "mixed__coll", "count": 5}]

    def test_non_404_error_returns_empty_list(self, monkeypatch):
        def handler(path: str) -> Any:
            raise VectorServiceError("GET → HTTP 503: down", code=503)

        paths = _patch_get(monkeypatch, handler)

        assert HttpVectorClient().list_collections() == []
        # No fallback attempted on a non-404 failure.
        assert paths == [STATS_PATH]

    def test_transport_error_returns_empty_list(self, monkeypatch):
        def handler(path: str) -> Any:
            raise VectorServiceError("connection refused")  # code=None

        paths = _patch_get(monkeypatch, handler)

        assert HttpVectorClient().list_collections() == []
        assert paths == [STATS_PATH]


class TestListCollectionsSkewFallback:
    """Pre-catalog-005 JAR: /stats 404s → /collections + /count keeps working."""

    def test_404_falls_back_to_collections_plus_count(self, monkeypatch):
        counts = {"coll_one": 4, "coll_two": 9}

        def handler(path: str) -> Any:
            if path == STATS_PATH:
                raise VectorServiceError("GET /stats → HTTP 404", code=404)
            if path == COLLECTIONS_PATH:
                return [{"name": "coll_one"}, {"name": "coll_two"}]
            if path.startswith("/v1/vectors/count?collection="):
                name = path.rsplit("=", 1)[1]
                return {"count": counts[name]}
            raise AssertionError(f"unexpected path {path}")

        paths = _patch_get(monkeypatch, handler)

        got = HttpVectorClient().list_collections()

        assert got == [
            {"name": "coll_one", "count": 4},
            {"name": "coll_two", "count": 9},
        ]
        assert paths[0] == STATS_PATH
        assert paths[1] == COLLECTIONS_PATH
        assert len(paths) == 4  # stats, collections, two counts

    def test_fallback_count_failure_reports_minus_one(self, monkeypatch):
        def handler(path: str) -> Any:
            if path == STATS_PATH:
                raise VectorServiceError("GET /stats → HTTP 404", code=404)
            if path == COLLECTIONS_PATH:
                return [{"name": "coll_ok"}, {"name": "coll_bad"}]
            if "coll_ok" in path:
                return {"count": 1}
            raise VectorServiceError("GET /count → HTTP 500", code=500)

        _patch_get(monkeypatch, handler)

        got = HttpVectorClient().list_collections()

        # A failing per-collection count must NOT drop the collection.
        assert got == [
            {"name": "coll_ok", "count": 1},
            {"name": "coll_bad", "count": -1},
        ]

    def test_404_then_collections_also_failing_returns_empty(self, monkeypatch):
        def handler(path: str) -> Any:
            if path == STATS_PATH:
                raise VectorServiceError("GET /stats → HTTP 404", code=404)
            raise VectorServiceError("GET /collections → HTTP 503", code=503)

        _patch_get(monkeypatch, handler)

        assert HttpVectorClient().list_collections() == []


class TestCollectionExistsLiveSemantics:
    def test_exists_via_stats(self, monkeypatch):
        rows = [{"name": "live__coll", "dim": 384, "count": 1, "last_write": "x"}]
        _patch_get(monkeypatch, lambda p: rows)

        client = HttpVectorClient()
        assert client.collection_exists("live__coll") is True
        assert client.collection_exists("tombstoned__coll") is False
