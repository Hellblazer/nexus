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
