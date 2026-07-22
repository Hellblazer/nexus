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
