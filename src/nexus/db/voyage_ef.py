# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nexus-owned Voyage AI embedding function (RDR-155 P4b P3).

Replaces ``chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction`` so
the chromadb dependency can leave ``pyproject.toml``. The chroma class was a
thin wrapper over ``voyageai.Client``; ``voyageai>=0.2`` is already a
first-class nexus dependency (the indexer paths construct ``voyageai.Client``
directly), so this removes a dependency without removing capability.

Semantics are mirrored deliberately rather than improved: the ``embed()``
kwargs and the ``float32`` output dtype are a cross-language contract, gated
by ``tests/db/test_embed_parity.py`` against the Java engine's
``VoyageEmbedder``. Drifting either would turn that parity gate into a
dtype/shape failure instead of a real embedding-divergence signal.

Sibling of :mod:`nexus.db.minilm_direct` (the P0b tier-0 replacement) and
follows the same "chroma EF protocol, no chroma import" shape.
"""
from __future__ import annotations

import os
from typing import Any

#: Voyage's own env var. The chroma implementation also honoured
#: ``CHROMA_VOYAGE_API_KEY``; that alias dies with the dependency (nexus has
#: never set it -- ``get_credential("voyage_api_key")`` is the nexus path).
VOYAGE_API_KEY_ENV: str = "VOYAGE_API_KEY"


class VoyageEmbeddingFunction:
    """Chroma-EF-protocol Voyage embedder over ``voyageai.Client`` directly.

    Drop-in for ``chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction``
    at its surviving call sites.
    """

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str | None = None,
        input_type: str | None = None,
        truncation: bool = True,
    ) -> None:
        resolved = api_key or os.getenv(VOYAGE_API_KEY_ENV)
        if not resolved:
            # Fail loud: an EF with no credential otherwise surfaces as an
            # opaque API error at first embed, far from the cause.
            raise ValueError(
                f"{VOYAGE_API_KEY_ENV} is not set and no api_key was passed — "
                "cannot construct a Voyage embedding function"
            )
        import voyageai  # noqa: PLC0415 — deferred: heavy optional import, matches indexer call sites

        self.model_name = model_name
        self.input_type = input_type
        self.truncation = truncation
        self._client: Any = voyageai.Client(api_key=resolved)

    # ── chroma EF protocol ─────────────────────────────────────────────

    @staticmethod
    def name() -> str:
        """Collection metadata records this; it re-keys collections if changed."""
        return "voyageai"

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list[str]:
        return ["cosine", "l2", "ip"]

    def embed_query(self, input: list[str]) -> list[Any]:  # noqa: A002 — chroma EF protocol name
        return self(input)

    def __call__(self, input: list[str]) -> list[Any]:  # noqa: A002 — chroma EF protocol name
        import numpy as np  # noqa: PLC0415 — heavy dep deferred

        embeddings = self._client.embed(
            texts=input,
            model=self.model_name,
            input_type=self.input_type,
            truncation=self.truncation,
        )
        return [
            np.array(embedding, dtype=np.float32)
            for embedding in embeddings.embeddings
        ]
