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
    """The requested ChromaDB collection does not exist."""


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
