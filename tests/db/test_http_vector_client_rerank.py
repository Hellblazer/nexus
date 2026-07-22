# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-188 P2.1 (nexus-9o6y2.8) — HttpVectorClient rerank envelope handling.

``search(rerank=True)`` sends the P1.2 fused-stage fields and unpacks the
object envelope into rows + ``rerank_meta_out``. A pre-rerank engine ignores
the unknown field and returns a bare array: reported as a stale-engine
degrade with the convergence remedy (one-engine doctrine — never a refusal,
never silence).
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
        captured.append({"path": path, "body": body})
        return response
    monkeypatch.setattr(hvc, "_post", fake_post)


def test_capability_marker_present():
    assert hvc.HttpVectorClient.supports_server_rerank is True


def test_rerank_fields_sent_and_envelope_unpacked(client, monkeypatch):
    captured: list[dict] = []
    _patch_post(monkeypatch, {
        "results": [{"id": "a", "content": "x", "distance": 0.2,
                     "collection": "knowledge__t", "rerank_score": 0.9}],
        "rerank_degraded": False,
        "rerank_model": "rerank-2.5",
    }, captured)

    meta: dict = {}
    rows = client.search("q", ["knowledge__t"], n_results=5,
                         rerank=True, rerank_top_k=3, rerank_meta_out=meta)

    body = captured[0]["body"]
    assert body["rerank"] is True
    assert body["rerank_top_k"] == 3
    assert rows[0]["rerank_score"] == 0.9
    assert meta == {"degraded": False, "error": None, "model": "rerank-2.5"}


def test_degraded_envelope_reported(client, monkeypatch):
    _patch_post(monkeypatch, {
        "results": [{"id": "a", "content": "x", "distance": 0.2,
                     "collection": "knowledge__t"}],
        "rerank_degraded": True,
        "rerank_error": "Voyage AI rerank failed: HTTP 500",
    }, [])

    meta: dict = {}
    rows = client.search("q", ["knowledge__t"], rerank=True, rerank_meta_out=meta)

    assert meta["degraded"] is True
    assert "HTTP 500" in meta["error"]
    assert rows and "rerank_score" not in rows[0]


def test_stale_engine_bare_array_reports_convergence_degrade(client, monkeypatch):
    # Engine predates the fused stage: unknown field ignored, bare array back.
    _patch_post(monkeypatch, [
        {"id": "a", "content": "x", "distance": 0.2, "collection": "knowledge__t"},
    ], [])

    meta: dict = {}
    rows = client.search("q", ["knowledge__t"], rerank=True, rerank_meta_out=meta)

    assert rows[0]["id"] == "a"
    assert meta["degraded"] is True
    assert meta["stale_engine"] is True
    assert "nx upgrade" in meta["error"]


def test_no_rerank_request_body_and_return_unchanged(client, monkeypatch):
    captured: list[dict] = []
    _patch_post(monkeypatch, [
        {"id": "a", "content": "x", "distance": 0.2, "collection": "knowledge__t"},
    ], captured)

    rows = client.search("q", ["knowledge__t"])

    assert "rerank" not in captured[0]["body"]
    assert "rerank_top_k" not in captured[0]["body"]
    assert rows[0]["id"] == "a"
