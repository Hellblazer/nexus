# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""IndexContext dataclass: shared indexing parameters replacing 12-parameter function signatures."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.config import TuningConfig
    from nexus.hook_registry import HookRegistry
    from nexus.indexer_utils import StalenessCache


@dataclass
class IndexContext:
    """Shared parameters for per-file indexing functions.

    Replaces the 12-parameter function signatures of the old _index_code_file
    and _index_prose_file.  Carries both ``voyage_key`` (raw API key, used by
    prose/PDF paths that call doc_indexer._embed_with_fallback internally) and
    ``voyage_client`` (pre-constructed voyageai.Client, used by the code path
    that calls voyage_client.embed directly).

    In local mode, ``embed_fn`` is set and ``voyage_client``/``voyage_key``
    are empty.  Indexers check ``embed_fn`` first: when present, it replaces
    all Voyage AI embedding calls.

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

    # Local mode embedding function: (texts: list[str]) -> list[list[float]]
    # When set, replaces Voyage AI embedding in code_indexer and prose_indexer.
    embed_fn: Callable[[list[str]], list[list[float]]] | None = field(default=None)

    # Catalog Document.doc_id resolver (RDR-101 Phase 3 PR δ Stage B).
    # When set, indexers call ``doc_id_resolver(file_path)`` and pass the
    # returned tumbler string into ``make_chunk_metadata``'s ``doc_id``
    # argument so freshly-written T3 chunks carry the catalog
    # cross-reference at chunk-write time. The orchestrator builds this
    # closure from the pre-index catalog registration map; ``None`` is
    # the legacy / no-catalog path (chunks ship without ``doc_id``,
    # which ``metadata_schema.normalize`` Step 4c then drops).
    doc_id_resolver: Callable[[Path], str] | None = field(default=None)

    # Per-stage intra-file timing bucket (nexus-7niu, vatx Gap 4b).
    # Populated only when the operator passes ``nx index repo --debug-timing``.
    # Silent when ``None`` — the per-file indexer skips the timing blocks
    # and pays zero overhead. Not shared across files; the orchestrator
    # builds a fresh instance per file and appends to the caller-side
    # collector so end-of-run aggregation is deterministic.
    stage_timers: "StageTimers | None" = field(default=None)

    # Pre-computed staleness map for *col*. When supplied, the per-file
    # ``check_staleness`` becomes a dict lookup instead of a ChromaDB
    # roundtrip — turning a no-op ``nx index repo`` (everything already
    # current) from O(N) round-trips into a single paginated sweep.
    # ``None`` is the legacy / fall-through path.
    staleness_cache: "StalenessCache | None" = field(default=None)

    # Post-store HookRegistry threaded down from the entry point so the
    # per-file indexer fires the single / batch / document chains via the
    # explicit instance rather than reaching into module-level globals.
    # ``None`` is the contract signal that the caller did not wire a
    # registry; ``__post_init__`` materialises a fresh empty
    # ``HookRegistry`` so callers downstream can always assume the field
    # is populated without an Optional check. Entry points wire
    # load-bearing default consumers via
    # :func:`nexus.hook_registry.install_default_hooks`.
    hooks: "HookRegistry | None" = field(default=None)

    def __post_init__(self) -> None:
        if self.hooks is None:
            from nexus.hook_registry import HookRegistry
            self.hooks = HookRegistry()


if TYPE_CHECKING:
    from nexus.stage_timers import StageTimers  # noqa: F401 — type-only
