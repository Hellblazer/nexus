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
    and _index_prose_file.

    In local mode, ``embed_fn`` is set (client-side ONNX/fastembed). In
    service mode, ``embed_fn`` is a no-op stub and the JVM embeds
    server-side. ``voyage_key`` / ``voyage_client`` are retained on the
    dataclass for call-site compatibility but are always empty/``None``:
    non-service, client-side Voyage embedding was retired (nexus-sghyo,
    Hal determination 2026-07-28 — the client does no embedding). Indexers
    that reach the branch guarded by these fields raise loud rather than
    construct a Voyage client.

    ``tuning`` provides configurable constants (chunk sizes, scoring weights,
    timeouts).  Defaults to the TuningConfig defaults when not supplied.
    """

    # T3 database and collection objects
    col: object             # ChromaDB Collection for the target collection
    db: object              # T3 database (for upsert_chunks_with_embeddings)

    # RETIRED (nexus-sghyo): always "" / None in shipping config. Kept on
    # the dataclass so existing call sites and tests do not need a
    # signature migration; the non-service branches they used to feed now
    # raise loud instead of consuming them.
    voyage_key: str = field(repr=False)  # raw API key — excluded from repr to prevent leaking
    voyage_client: object | None        # formerly a pre-constructed voyageai.Client (code path)

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
    # nexus-4jj40 round 5 (T2 critique [24618]): DECOUPLED from ``force``.
    # ``force`` alone bypasses ``check_staleness`` and re-chunks/re-sends an
    # unchanged file; the server's own existence-partition (RDR-181) still
    # skips the billed Voyage re-embed for a chash whose text is byte-
    # identical, refreshing ONLY the chunk's stored metadata (e.g.
    # ``section_type``) via a metadata-only UPDATE -- both the direct
    # upsert path (``PgVectorRepository.batchUpdateMetadata``) and the
    # combined-write path (``CombinedWriteService``) do this as of this
    # round. ``force_re_embed=True`` is the explicit opt-in for the OLD
    # behaviour (every chunk in the batch actually re-embeds), reserved for
    # a genuine embedding-model/content-divergence recompute -- coupling it
    # to ``force`` made a routine reclassification-only reindex pay full
    # Voyage cost for zero benefit (T2 [24618] Important finding).
    force_re_embed: bool = False
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

    # Cross-file chunk batcher (nexus-1ugqs, duoak 2C). When set, the
    # per-file indexers STAGE chunks via ``batcher.add(...)`` instead of
    # upserting directly, and defer their post-store hook chains to the
    # batcher's completion callback (the orchestrator fires them once the
    # file's chunks land in a successful flush). ``None`` is the legacy
    # one-upsert-per-file path. Service mode only — the batcher's flush
    # fn posts raw text for server-side embedding.
    batcher: "object | None" = field(default=None)

    def __post_init__(self) -> None:
        if self.hooks is None:
            from nexus.hook_registry import HookRegistry  # noqa: PLC0415 — circular-dep avoidance: hook_registry imports this module
            self.hooks = HookRegistry()


if TYPE_CHECKING:
    from nexus.stage_timers import StageTimers  # noqa: F401 — type-only
