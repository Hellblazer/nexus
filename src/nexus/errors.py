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
