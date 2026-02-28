# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Four-store T3 factory functions (RDR-004).

Each function returns a :class:`~nexus.db.t3.T3Database` backed by a
``chromadb.PersistentClient`` at the configured filesystem path.

Store mapping:
  - ``t3_code()``      → ``chromadb.code_path``      (code__ collections)
  - ``t3_docs()``      → ``chromadb.docs_path``      (docs__ collections)
  - ``t3_rdr()``       → ``chromadb.rdr_path``       (rdr__ collections)
  - ``t3_knowledge()`` → ``chromadb.knowledge_path`` (knowledge__ collections)
"""
from __future__ import annotations

from pathlib import Path

import chromadb

from nexus.config import get_credential, load_config
from nexus.db.t3 import T3Database


def _persistent_t3(
    path_key: str,
    legacy_key: str | None = None,
    *,
    embeddings_required: bool = True,
) -> T3Database:
    """Open a T3Database backed by a local PersistentClient.

    Args:
        path_key:            Key under ``chromadb`` in config for this store's path.
        legacy_key:          Optional fallback key tried when ``path_key`` is absent/empty.
        embeddings_required: When True (default) require and use ``voyage_api_key``
                             for embeddings.  When False, open with
                             ``DefaultEmbeddingFunction`` — suitable for
                             metadata-only operations (e.g. frecency updates) that
                             never call the embedding API.

    Raises:
        RuntimeError: If neither ``path_key`` nor ``legacy_key`` resolves to a
            non-empty path, or (when embeddings_required=True) if
            ``voyage_api_key`` is not configured.
    """
    cfg = load_config()
    chromadb_cfg = cfg.get("chromadb", {})
    raw_path = chromadb_cfg.get(path_key) or (
        chromadb_cfg.get(legacy_key) if legacy_key else None
    )
    if not raw_path:
        raise RuntimeError(
            f"T3 store not configured: set chromadb.{path_key} in config"
        )
    path = str(Path(raw_path).expanduser())
    if embeddings_required:
        voyage_api_key = get_credential("voyage_api_key")
        if not voyage_api_key:
            raise RuntimeError(
                "voyage_api_key not configured — required for embeddings"
            )
        return T3Database(
            voyage_api_key=voyage_api_key,
            _client=chromadb.PersistentClient(path=path),
        )
    else:
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
        return T3Database(
            _client=chromadb.PersistentClient(path=path),
            _ef_override=DefaultEmbeddingFunction(),
        )


def t3_code() -> T3Database:
    """Return a T3Database for the code store."""
    return _persistent_t3("code_path")


def t3_docs() -> T3Database:
    """Return a T3Database for the docs store."""
    return _persistent_t3("docs_path")


def t3_rdr() -> T3Database:
    """Return a T3Database for the RDR store."""
    return _persistent_t3("rdr_path")


def t3_knowledge() -> T3Database:
    """Return a T3Database for the knowledge store.

    Falls back to the legacy ``chromadb.path`` key for users who configured
    the old single-store layout.
    """
    return _persistent_t3("knowledge_path", legacy_key="path")


def t3_code_local() -> T3Database:
    """Return a T3Database for the code store without requiring ``voyage_api_key``.

    Uses ``DefaultEmbeddingFunction`` — suitable for metadata-only operations
    such as frecency score updates that never call the embedding API.
    """
    return _persistent_t3("code_path", embeddings_required=False)


def t3_docs_local() -> T3Database:
    """Return a T3Database for the docs store without requiring ``voyage_api_key``.

    Uses ``DefaultEmbeddingFunction`` — suitable for metadata-only operations.
    """
    return _persistent_t3("docs_path", embeddings_required=False)


# Note: t3_rdr_local() and t3_knowledge_local() are intentionally absent.
# Frecency updates (the only metadata-only bulk operation) apply exclusively to
# file-based code__ and docs__ collections.  RDR and knowledge collections are
# written via normal embedding operations that already require voyage_api_key.
