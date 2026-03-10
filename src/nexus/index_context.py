# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""IndexContext dataclass: shared indexing parameters replacing 12-parameter function signatures."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.config import TuningConfig


@dataclass
class IndexContext:
    """Shared parameters for per-file indexing functions.

    Replaces the 12-parameter function signatures of the old _index_code_file
    and _index_prose_file.  Carries both ``voyage_key`` (raw API key, used by
    prose/PDF paths that call doc_indexer._embed_with_fallback internally) and
    ``voyage_client`` (pre-constructed voyageai.Client, used by the code path
    that calls voyage_client.embed directly).

    ``tuning`` provides configurable constants (chunk sizes, scoring weights,
    timeouts).  Defaults to the TuningConfig defaults when not supplied.
    """

    # T3 database and collection objects
    col: object             # ChromaDB Collection for the target collection
    db: object              # T3 database (for upsert_chunks_with_embeddings)

    # Voyage AI — code path uses voyage_client; prose/PDF paths use voyage_key
    voyage_key: str = field(repr=False)  # raw API key — single source of truth; excluded from repr to prevent leaking
    voyage_client: object | None        # pre-constructed voyageai.Client (code path)

    # Indexing scope
    repo_path: Path
    corpus: str             # collection name (e.g. "code__myrepo")
    embedding_model: str

    # Per-file metadata
    git_meta: dict
    now_iso: str
    score: float = 0.0

    # Override parameters
    chunk_lines: int | None = None
    force: bool = False
    timeout: float = 120.0

    # Optional tuning config; resolved lazily to avoid circular imports
    tuning: "TuningConfig | None" = field(default=None)
