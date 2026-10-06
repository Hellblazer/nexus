# SPDX-License-Identifier: AGPL-3.0-or-later
"""MiniLM embedder padded to the engine test substrate's 768-wide model."""
from __future__ import annotations

from nexus.db.local_ef import LocalEmbeddingFunction


class PaddedMiniLM(LocalEmbeddingFunction):
    """Real MiniLM vectors zero-padded to the substrate's 768-wide model.

    Every collection on this substrate registers under bge-base-en-v15-768 (the
    profile covers all content types, so a 384-wide model cannot be registered),
    and the engine requires a centroid's width to equal its collection model's
    dimension. Zero padding leaves cosine and Euclidean distances unchanged, so
    the clustering and assignment assertions still test the real embeddings.
    """

    _SUBSTRATE_DIM = 768

    def __call__(self, input: list[str]) -> list[list[float]]:
        return [
            [*vec, *([0.0] * (self._SUBSTRATE_DIM - len(vec)))]
            for vec in super().__call__(input)
        ]
