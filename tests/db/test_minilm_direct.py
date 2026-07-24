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
    ARTIFACT_DIR,
    MiniLMDirectEmbeddingFunction,
)

_TEXTS = [
    "search engine architecture design patterns",
    "the quick brown fox jumps over the lazy dog",
    "PostgreSQL row-level security with tenant isolation",
]


def _artifact_present() -> bool:
    return (ARTIFACT_DIR / "model.onnx").is_file() and (
        ARTIFACT_DIR / "tokenizer.json"
    ).is_file()


requires_artifact = pytest.mark.skipif(
    not _artifact_present(),
    reason="MiniLM ONNX artifact not cached (ensure_artifact() downloads it; "
    "CI provisions via the chroma-S3 fetch, same as service-ci)",
)


@requires_artifact
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


@requires_artifact
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
