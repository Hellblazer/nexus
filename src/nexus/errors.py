# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Nexus exception hierarchy."""
from __future__ import annotations


class NexusError(Exception):
    """Base exception for all Nexus errors."""


class T3ConnectionError(NexusError):
    """Failed to connect to or use the T3 ChromaDB cloud backend."""


class IndexingError(NexusError):
    """Error during document indexing pipeline."""


class CredentialsMissingError(NexusError):
    """A required API key or credential is absent."""


class CollectionNotFoundError(NexusError):
    """The requested vector collection does not exist.

    RDR-155 P4b (P0c, nexus-g37fr plan v3): the substrate-neutral successor
    to ``chromadb.errors.NotFoundError`` as the missing-collection contract
    between :class:`~nexus.db.http_vector_client.HttpVectorClient` (raiser)
    and the indexer/purge/reidentify/backfill catchers.
    """


def collection_not_found_errors() -> tuple[type[BaseException], ...]:
    """The exception types that mean "collection does not exist".

    Transition contract (RDR-155 P4b P0c): during the deletion window the
    chroma-backed TEST substrate (``T3Database`` over ``EphemeralClient``)
    still raises ``chromadb.errors.NotFoundError`` natively, so catchers
    must tolerate both types. The chroma member drops out AUTOMATICALLY
    when the dependency leaves the tree at P3 (the deferred import fails
    and the tuple collapses to the nexus-native type) — catchers need no
    edit at removal time.
    """
    try:
        from chromadb.errors import NotFoundError as _chroma_not_found  # noqa: PLC0415 — transition-window optional dep; absence is the designed P3 end state
    except ImportError:
        return (CollectionNotFoundError,)
    return (CollectionNotFoundError, _chroma_not_found)


class EmbeddingModelMismatch(NexusError):
    """Export embedding model is incompatible with the target collection's model.

    Importing an export produced with one embedding model into a collection
    that uses a different model would silently corrupt search quality
    (cross-model cosine similarity ≈ 0.05, i.e. random noise).
    """


class FormatVersionError(NexusError):
    """Export file format version is newer than this version of Nexus supports.

    Upgrade Nexus to import this file.
    """


class EmbeddingDimensionMismatch(NexusError):
    """The export's declared embedding model doesn't match the actual
    vector dimensionality found in the file (GH #1370 D2).

    Pre-migration ``.nxexp`` exports can carry a WRONG ``embedding_model``
    header label: legacy two-segment collection names route through
    ``voyage_model_for_collection``'s prefix-based guess, which silently
    mislabels local-mode (bge/minilm) exports as Voyage models. This
    check catches the resulting dimension contradiction before it
    reaches the vector store, instead of failing with an opaque
    downstream error.

    Attributes:
        declared_model: The model name that was checked (the export
            header's value, or the ``--assume-model`` override).
        declared_dims: Expected dimensionality for ``declared_model``.
        actual_dims: Dimensionality actually found in the export's vectors.
        collection: Target collection name.
        assumed: True if ``declared_model`` came from ``--assume-model``.
    """

    def __init__(
        self,
        *,
        declared_model: str,
        declared_dims: int,
        actual_dims: int,
        collection: str,
        assumed: bool = False,
    ) -> None:
        self.declared_model = declared_model
        self.declared_dims = declared_dims
        self.actual_dims = actual_dims
        self.collection = collection
        self.assumed = assumed
        if assumed:
            label = f"assumed model {declared_model!r}"
            hint = "the --assume-model override is wrong -- pick a different model"
        else:
            label = f"header claims {declared_model!r}"
            hint = (
                "the header label is wrong (a known defect of pre-migration "
                "exports, GH #1370); re-run with --assume-model <model>"
            )
        super().__init__(
            f"{label} ({declared_dims}-dim) but vectors are {actual_dims}-dim "
            f"for collection {collection!r} -- {hint}."
        )


class PutOversizedError(NexusError):
    """A ``put``-path write was refused because the document exceeds the
    ChromaDB Cloud per-document byte cap.

    The indexer pipeline tolerates oversized inputs via defense-in-depth
    drop-and-warn (a chunker that produced an oversized record is the
    real bug; the pipeline keeps running). The ``put`` path has no
    chunker upstream, so dropping silently would leave the caller
    believing the write succeeded while producing a catalog ghost
    (no row in ChromaDB despite a registered ``doc_id``). See
    GitHub #244 and bead ``nexus-akof``.

    Attributes:
        doc_id: The computed doc_id that did not make it to ChromaDB.
        doc_bytes: Actual size of the serialized document in bytes.
        max_bytes: The ChromaDB Cloud cap (``QUOTAS.MAX_DOCUMENT_BYTES``).
        collection: Target collection name for clearer diagnostics.
    """

    def __init__(
        self,
        *,
        doc_id: str,
        doc_bytes: int,
        max_bytes: int,
        collection: str,
    ) -> None:
        self.doc_id = doc_id
        self.doc_bytes = doc_bytes
        self.max_bytes = max_bytes
        self.collection = collection
        super().__init__(
            f"document {doc_id!r} is {doc_bytes} bytes, exceeds "
            f"{max_bytes}-byte ChromaDB cap for collection {collection!r}. "
            f"Shrink the content or chunk it before calling put()."
        )
