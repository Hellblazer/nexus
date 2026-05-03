# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-103 Phase 1: ``CollectionName`` value object.

The collection name is a four-segment tuple
``(content_type, owner_id, embedding_model, model_version)``
rendered to ``<content_type>__<owner_id>__<embedding_model>__v<n>``.

The catalog renders ``CollectionName`` instances; the indexer asks the
catalog for a collection rather than constructing one and asking the
catalog to record it. ``CollectionName.parse`` is strict (pinned
decision #4): legacy 2-segment names and non-canonical embedding models
raise ``ValueError`` and remain string-only identifiers per RDR-101's
grandfathering invariant.
"""
from __future__ import annotations

from dataclasses import dataclass

from nexus.corpus import (
    CANONICAL_EMBEDDING_MODELS,
    CONTENT_TYPES,
    parse_conformant_collection_name,
)


@dataclass(frozen=True, slots=True)
class CollectionName:
    """The canonical collection-name tuple."""

    content_type: str
    owner_id: str
    embedding_model: str
    model_version: int

    def render(self) -> str:
        """Render to the physical T3 collection name."""
        return (
            f"{self.content_type}__{self.owner_id}"
            f"__{self.embedding_model}__v{self.model_version}"
        )

    @classmethod
    def parse(cls, name: str) -> CollectionName:
        """Parse a conformant T3 collection name into a ``CollectionName``.

        Raises ``ValueError`` for legacy 2-segment names, fallback names,
        non-canonical embedding models, or any other shape that does not
        match the RDR-103 schema. Generic callers receiving arbitrary
        physical-collection strings must gate with
        ``nexus.corpus.is_conformant_collection_name`` before calling
        this constructor.

        The conformance regex (``_CONFORMANT_COLLECTION_RE`` in
        ``nexus.corpus``) is derived from ``CONTENT_TYPES``, so the
        content_type group already enforces the closed set. The redundant
        check below catches a future divergence (e.g. someone widens the
        regex without updating ``CONTENT_TYPES``) without weakening today's
        guarantees.
        """
        parsed = parse_conformant_collection_name(name)
        content_type = parsed["content_type"]
        if content_type not in CONTENT_TYPES:
            raise ValueError(
                f"Collection name {name!r} has unknown content_type "
                f"{content_type!r}; expected one of {CONTENT_TYPES}"
            )
        embedding_model = parsed["embedding_model"]
        if embedding_model not in CANONICAL_EMBEDDING_MODELS:
            raise ValueError(
                f"Collection name {name!r} has non-canonical embedding_model "
                f"{embedding_model!r}; expected one of "
                f"{sorted(CANONICAL_EMBEDDING_MODELS)}"
            )
        return cls(
            content_type=content_type,
            owner_id=parsed["owner_id"],
            embedding_model=embedding_model,
            model_version=int(parsed["model_version"][1:]),
        )
