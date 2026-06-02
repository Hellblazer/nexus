# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local embedding function for zero-config T3 (RDR-038).

Implements the ChromaDB ``EmbeddingFunction`` protocol using local models:
- Tier 0: bundled ``chromadb.utils.embedding_functions.ONNXMiniLM_L6_V2`` (384d)
- Tier 1: ``fastembed`` bge-base-en-v1.5 (768d) — requires ``pip install conexus[local]``

Auto-selection: tier 1 if fastembed is importable, else tier 0.
Explicit: ``LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")`` forces tier 0.
"""
from __future__ import annotations

import importlib
import threading
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

# Model metadata: name → dimensions
_MODEL_DIMS: dict[str, int] = {
    "all-MiniLM-L6-v2": 384,
    "BAAI/bge-base-en-v1.5": 768,
}

_TIER0_MODEL = "all-MiniLM-L6-v2"
_TIER1_MODEL = "BAAI/bge-base-en-v1.5"

# RDR-109 Phase 2: normalized tokens for the local embedding models.
# Used in conformant collection names (RDR-103 four-segment shape) and
# in chunk metadata so the embedder identity stops lying about which
# vectors are actually stored. Tokens must match the
# ``_CONFORMANT_COLLECTION_RE`` regex in ``corpus.py``:
# ``^[a-z][a-z0-9-]*$``.
_MODEL_TOKENS: dict[str, str] = {
    "all-MiniLM-L6-v2": "minilm-l6-v2-384",
    "BAAI/bge-base-en-v1.5": "bge-base-en-v15-768",
}


def local_model_token(model_name: str | None = None) -> str:
    """Return the RDR-109 normalized token for a local embedding model.

    ``model_name=None`` picks the active tier (tier 1 if fastembed is
    importable, else tier 0). Used by the write path
    (``effective_embedding_model_for_writes`` in ``corpus.py``) and by
    ``nx doctor`` to report what's actually embedding the collection.
    """
    if model_name is None:
        model_name = _TIER1_MODEL if _fastembed_available() else _TIER0_MODEL
    return _MODEL_TOKENS.get(model_name, "minilm-l6-v2-384")


LOCAL_EMBEDDING_TOKENS: frozenset[str] = frozenset(_MODEL_TOKENS.values())
"""Set of all valid local-mode embedding-model tokens. Used by
``CollectionName.parse`` and the bidirectional name-aware EF dispatch
in ``T3Database._embedding_fn`` to detect local-token names."""


def _fastembed_available() -> bool:
    """Return True if fastembed can be imported."""
    try:
        importlib.import_module("fastembed")
        return True
    except (ImportError, ModuleNotFoundError):
        return False


def _select_model_name() -> str:
    """Resolve the local embedding model when none is explicitly given.

    RDR-144 P3 (C): honour the ``local.embed_model`` choice persisted by
    ``nx init`` before falling back to the legacy fastembed-availability
    auto-select. Backward-compatible: absent the config key, behaviour is
    unchanged (tier-1 if fastembed importable, else tier-0).

    If the user chose bge-768 but the ``[local]`` extra is not installed,
    fall back to tier-0 with a structured WARNING (never silent — ``nx
    doctor`` surfaces it; P5a) rather than crashing on a missing import.
    """
    from nexus.config import local_embed_model_choice

    configured = local_embed_model_choice()
    if configured in _MODEL_DIMS:
        if configured == _TIER1_MODEL and not _fastembed_available():
            _log.warning(
                "local_embed_model_unavailable",
                chosen=configured,
                fallback=_TIER0_MODEL,
                hint="install conexus[local] (or run `nx init`) to use bge-768",
            )
            return _TIER0_MODEL
        return configured  # type: ignore[return-value]
    return _TIER1_MODEL if _fastembed_available() else _TIER0_MODEL


class LocalEmbeddingFunction:
    """ChromaDB-compatible embedding function using local ONNX models.

    Satisfies the ``chromadb.api.types.EmbeddingFunction`` protocol:
    ``__call__(input: Documents) -> Embeddings`` where ``Documents = list[str]``
    and ``Embeddings = list[list[float]]``.

    Also implements ``name()``, ``build_from_config()``, ``get_config()``, and
    ``is_legacy()`` required by ChromaDB >= 0.6 PersistentClient.
    """

    def __init__(self, model_name: str | None = None) -> None:
        if model_name is not None:
            # Explicit arg always wins (forced-tier callers, build_from_config).
            self._model_name = model_name
        else:
            self._model_name = _select_model_name()

        self._dimensions = _MODEL_DIMS.get(self._model_name, 384)
        self._ef: Any = None  # lazy init
        # Storage review S-2: guard lazy init so two concurrent callers
        # don't both download/load the fastembed model and discard one.
        self._ef_lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    # ── ChromaDB EmbeddingFunction protocol ──────────────────────────────

    @staticmethod
    def name() -> str:
        return "nexus_local"

    def is_legacy(self) -> bool:
        return False

    def get_config(self) -> dict[str, Any]:
        return {"model_name": self._model_name, "dimensions": self._dimensions}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "LocalEmbeddingFunction":
        return LocalEmbeddingFunction(model_name=config.get("model_name"))

    def embed_query(self, input: list[str]) -> list[list[float]]:
        """Embed query input (same as __call__ for local models)."""
        return self(input)

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list[str]:
        return ["cosine", "l2", "ip"]

    # ── Embedding ────────────────────────────────────────────────────────

    def _init_ef(self) -> None:
        """Lazy-initialise the underlying embedding function."""
        if self._model_name == _TIER0_MODEL:
            from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

            self._ef = ONNXMiniLM_L6_V2()
        else:
            # Tier 1: fastembed. Thread a stable, XDG-respecting cache_dir so
            # the bge-768 model is not re-downloaded to a volatile $TMPDIR on
            # every cold start (RDR-144 P1 / CRITICAL-1). The resolver is read
            # here — the sole EF-construction chokepoint — so launchd-spawned
            # daemon/MCP processes that never see the nx-init shell env still
            # land on the stable dir.
            from fastembed import TextEmbedding

            from nexus.config import fastembed_cache_dir

            cache_dir = fastembed_cache_dir()
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._ef = TextEmbedding(model_name=self._model_name, cache_dir=str(cache_dir))
        _log.debug("local_ef_initialized", model=self._model_name, dims=self._dimensions)

    def __call__(self, input: list[str]) -> list[list[float]]:
        """Embed a list of texts, returning a list of float vectors."""
        # Double-checked locking: the outer read is atomic under CPython's
        # GIL; the lock guards the init so only one thread loads the model.
        if self._ef is None:
            with self._ef_lock:
                if self._ef is None:
                    self._init_ef()

        if self._model_name == _TIER0_MODEL:
            # ONNXMiniLM_L6_V2 already returns list[list[float]]
            return self._ef(input)
        else:
            # fastembed TextEmbedding.embed() returns a generator of numpy
            # arrays. Return them as-is: chromadb >= 1.x (issue #1058) calls
            # ``.tolist()`` on each element itself during serialization
            # (convert_np_embeddings_to_list), so pre-converting to Python
            # lists here raised ``'list' object has no attribute 'tolist'`` and
            # broke ALL local-mode (bge) search. doc_indexer._local_embed
            # already normalizes np arrays via a hasattr('tolist') guard.
            return list(self._ef.embed(input))
