# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-109 Phase 3: local cross-encoder substrate + mode-aware rerank.

Tests cover the public surface without hitting the network or running
ONNX inference (the model download is ~80MB and the inference is
upstream-tested). The dispatch and fallback paths are the things that
can regress.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.cross_encoder import (
    LocalCrossEncoder,
    _reset_singleton,
    cross_encoder_available,
    get_local_cross_encoder,
)
from nexus.types import SearchResult


@pytest.fixture(autouse=True)
def _reset() -> None:
    _reset_singleton()
    yield
    _reset_singleton()


# ── Surface ──────────────────────────────────────────────────────────


def test_singleton_is_cached_per_model() -> None:
    a = get_local_cross_encoder("model-a")
    b = get_local_cross_encoder("model-a")
    assert a is b


def test_singleton_swaps_when_model_id_changes() -> None:
    a = get_local_cross_encoder("model-a")
    b = get_local_cross_encoder("model-b")
    assert a is not b
    assert b.model_id == "model-b"


def test_cross_encoder_available_true_when_deps_present() -> None:
    # onnxruntime / tokenizers / huggingface_hub are core deps already
    # (chromadb's bundled ONNX MiniLM path pulls them).
    assert cross_encoder_available() is True


def test_cross_encoder_available_false_when_dep_missing(monkeypatch) -> None:
    import builtins
    real_import = builtins.__import__

    def _fail(name, *args, **kwargs):
        if name == "onnxruntime":
            raise ImportError("simulated missing dep")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail)
    assert cross_encoder_available() is False


def test_score_returns_empty_for_empty_documents() -> None:
    # Empty documents must short-circuit before touching the session,
    # so the test does not need to mock the model download.
    ce = LocalCrossEncoder()
    assert ce.score("query", []) == []


# ── Mode-aware rerank dispatch ───────────────────────────────────────


def _make_result(i: int, content: str) -> SearchResult:
    return SearchResult(
        id=f"r{i}",
        content=content,
        distance=0.0,
        collection="docs__owner__voyage-context-3__v1",
        metadata={},
        hybrid_score=0.0,
    )


def test_rerank_routes_to_local_in_local_mode(monkeypatch) -> None:
    monkeypatch.setenv("NX_LOCAL", "1")
    from nexus import scoring

    results = [_make_result(i, f"doc {i}") for i in range(3)]

    calls: dict[str, object] = {}

    def fake_score(self, query, documents):
        calls["query"] = query
        calls["docs"] = list(documents)
        return [0.1, 0.9, 0.5]  # doc-1 wins

    with patch.object(LocalCrossEncoder, "score", fake_score):
        out = scoring.rerank_results(results, query="q")

    assert [r.id for r in out] == ["r1", "r2", "r0"]
    assert calls["query"] == "q"
    assert calls["docs"] == ["doc 0", "doc 1", "doc 2"]


def test_rerank_local_failure_falls_back_to_original_order(monkeypatch) -> None:
    monkeypatch.setenv("NX_LOCAL", "1")
    from nexus import scoring

    results = [_make_result(i, f"doc {i}") for i in range(3)]

    def boom(self, query, documents):
        raise RuntimeError("simulated model load failure")

    with patch.object(LocalCrossEncoder, "score", boom):
        out = scoring.rerank_results(results, query="q", top_k=2)

    assert [r.id for r in out] == ["r0", "r1"]


def test_rerank_routes_to_cloud_in_cloud_mode(cloud_mode, monkeypatch) -> None:
    """Cloud mode preserves the existing Voyage path."""
    from nexus import scoring

    results = [_make_result(i, f"doc {i}") for i in range(3)]

    class _RerankItem:
        def __init__(self, index: int, relevance_score: float) -> None:
            self.index = index
            self.relevance_score = relevance_score

    class _RerankResp:
        def __init__(self, items) -> None:
            self.results = items

    captured: dict[str, object] = {}

    def fake_rerank(query, documents, model, top_k):
        captured["query"] = query
        captured["documents"] = list(documents)
        captured["model"] = model
        return _RerankResp([
            _RerankItem(2, 0.9),
            _RerankItem(0, 0.5),
            _RerankItem(1, 0.1),
        ])

    class _FakeClient:
        def rerank(self, **kw):
            return fake_rerank(**kw)

    from unittest.mock import MagicMock as _MM
    stub_t3 = _MM()
    stub_t3._voyage_client = _FakeClient()

    out = scoring.rerank_results(results, query="q", t3=stub_t3)
    assert [r.id for r in out] == ["r2", "r0", "r1"]
    assert captured["model"] == "rerank-2.5"


def test_rerank_returns_empty_when_input_empty(monkeypatch) -> None:
    from nexus import scoring

    monkeypatch.setenv("NX_LOCAL", "1")
    assert scoring.rerank_results([], "q") == []
