# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-155 P4b P0b: the nexus-owned direct MiniLM EF.

Differential parity against chroma's ``ONNXMiniLM_L6_V2`` while the
oracle is still installed (the P0a harness discipline applied to the
EF): same artifact, same preprocessing, near-identical vectors. After
chromadb leaves at P3 the parity class is skipped-by-absence and the
behavioral pins below become the permanent conformance suite.

The engine's ``OnnxEmbedder`` reads the SAME artifact from the SAME
cache path (``~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx``) —
client/engine EF parity is artifact-level by construction.
"""
from __future__ import annotations

import numpy as np
import pytest

from nexus.db.minilm_direct import (
    MiniLMDirectEmbeddingFunction,
)

_TEXTS = [
    "search engine architecture design patterns",
    "the quick brown fox jumps over the lazy dog",
    "PostgreSQL row-level security with tenant isolation",
]


@pytest.fixture(scope="module", autouse=True)
def _provision_artifact():
    """Self-provisioning, not ambient (review finding: a skipif here would
    silently vacuous-skip forever once chromadb stops populating the
    shared cache at P3). ensure_artifact() is idempotent; a genuinely
    offline box fails LOUD with the download error, never skip-passes
    the permanent conformance suite."""
    from nexus.db.minilm_direct import ensure_artifact

    ensure_artifact()




class TestBehavioralPins:
    def test_shape_dtype_and_norm(self) -> None:
        ef = MiniLMDirectEmbeddingFunction()
        out = ef(_TEXTS)
        assert isinstance(out, list) and len(out) == 3
        arr = np.asarray(out, dtype=np.float32)
        assert arr.shape == (3, 384)
        norms = np.linalg.norm(arr, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-5)  # L2-normalized

    def test_deterministic(self) -> None:
        ef = MiniLMDirectEmbeddingFunction()
        a = np.asarray(ef(_TEXTS[:1]))
        b = np.asarray(ef(_TEXTS[:1]))
        assert np.array_equal(a, b)

    def test_semantic_ordering(self) -> None:
        """Similar texts closer than dissimilar — the load-bearing test-EF
        property (ranking snapshots, cosine gates)."""
        ef = MiniLMDirectEmbeddingFunction()
        q = np.asarray(ef(["database search indexing"]))[0]
        docs = np.asarray(ef(_TEXTS))
        sims = docs @ q
        assert sims[0] > sims[1]  # search-architecture text beats fox

    def test_chroma_ef_protocol_surface(self) -> None:
        ef = MiniLMDirectEmbeddingFunction()
        assert callable(ef.embed_query)
        assert ef.name() == "onnx_mini_lm_l6_v2"
        assert (
            np.asarray(ef.embed_query(_TEXTS[:1]))
            == np.asarray(ef(_TEXTS[:1]))
        ).all()


class TestDifferentialParityAgainstChroma:
    """Deletes with the dependency at P3 (import guarded)."""

    def test_vectors_match_oracle(self) -> None:
        chroma_ef_mod = pytest.importorskip(
            "chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2"
        )
        oracle = chroma_ef_mod.ONNXMiniLM_L6_V2()
        ours = MiniLMDirectEmbeddingFunction()
        a = np.asarray(oracle(_TEXTS), dtype=np.float32)
        b = np.asarray(ours(_TEXTS), dtype=np.float32)
        assert a.shape == b.shape == (3, 384)
        assert np.allclose(a, b, atol=1e-6), (
            f"max divergence {np.abs(a - b).max()}"
        )


def test_a_test_that_moves_home_does_not_download_the_model(tmp_path, monkeypatch) -> None:
    """nexus-gd3b9: a fixture that points HOME at a tmp dir used to make the
    embedder resolve an empty cache under it and download the artifact from
    S3 mid-suite (an HTTP 503 failed a full run). The suite pins the cache
    to the operator's real one, so this must not touch the network."""
    from nexus.db import minilm_direct

    # Precondition, kept apart from the regression: the module fixture has
    # warmed the pinned cache. A cold cache reads as this failure, never as
    # the re-download the test exists to catch.
    warm = minilm_direct.artifact_dir()
    assert (warm / "model.onnx").is_file(), f"MiniLM cache not warm at {warm}"

    monkeypatch.setenv("HOME", str(tmp_path))

    def _no_download(*_a, **_kw):
        raise AssertionError("the model was downloaded again under a moved HOME")

    monkeypatch.setattr("httpx.stream", _no_download)
    assert minilm_direct.ensure_artifact() == minilm_direct.artifact_dir()
    assert not str(minilm_direct.artifact_dir()).startswith(str(tmp_path))
