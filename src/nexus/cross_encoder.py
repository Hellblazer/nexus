# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local cross-encoder substrate for RDR-109 Phase 3.

Provides ``LocalCrossEncoder`` — an ONNX-runtime cross-encoder for
(query, document) salience scoring without PyTorch. Used by
:mod:`nexus.scoring` for the local-mode rerank path and reserved for
RDR-109 Phase 4 (salience calibration, nexus-2wc1).

Stack:

- ``onnxruntime``    runtime (already a core dep via chromadb's bundled
  ONNX MiniLM EF).
- ``tokenizers``     HuggingFace Rust tokenizer (already a core dep).
- ``huggingface_hub``  pulls the ONNX + tokenizer.json artifacts on
  first call. Already a core dep via chromadb / docling.

Default model: ``cross-encoder/ms-marco-MiniLM-L-6-v2`` (~80MB). The
download is lazy; importing this module does not touch the network.

RDR-109 Phase 3, step 1 resolved fastembed FIRST: fastembed 0.8.0 has
no Reranker / CrossEncoder class (only TextEmbedding, SparseTextEmbedding,
ImageEmbedding, late-interaction). Falling back to onnxruntime-direct
keeps RDR-038 F-03 intact (no PyTorch) and reuses deps already pulled
by ``LocalEmbeddingFunction``.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

# Default model: small (~80MB), well-known, well-trained on MS-MARCO.
# Override via ``LocalCrossEncoder(model_id=...)`` for experimentation.
_DEFAULT_MODEL_ID = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# HF hub filenames under the model repo. ``onnx/model.onnx`` is the
# canonical layout for the ``optimum``-exported variants.
_MODEL_FILENAME_CANDIDATES = ("model.onnx", "onnx/model.onnx")
_TOKENIZER_FILENAME = "tokenizer.json"


class LocalCrossEncoder:
    """ONNX cross-encoder. Lazy model download + lazy session init.

    Thread-safe init via a per-instance lock so concurrent ``score()``
    callers don't race on the first hub download.
    """

    def __init__(self, model_id: str = _DEFAULT_MODEL_ID, *, cache_dir: Path | None = None) -> None:
        self._model_id = model_id
        self._cache_dir = cache_dir
        self._session: Any = None
        self._tokenizer: Any = None
        self._init_lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return self._model_id

    def _resolve_model_path(self) -> Path:
        """Locate ``model.onnx`` for *model_id* on the HF hub. Tries the
        flat layout first, then the ``onnx/`` subfolder.

        Raises ``RuntimeError`` if neither path exists in the repo.
        """
        from huggingface_hub import hf_hub_download
        from huggingface_hub.errors import EntryNotFoundError

        last_exc: Exception | None = None
        for candidate in _MODEL_FILENAME_CANDIDATES:
            try:
                return Path(
                    hf_hub_download(
                        repo_id=self._model_id,
                        filename=candidate,
                        cache_dir=str(self._cache_dir) if self._cache_dir else None,
                    )
                )
            except EntryNotFoundError as exc:
                last_exc = exc
                continue
        raise RuntimeError(
            f"LocalCrossEncoder: no ONNX model file found for "
            f"{self._model_id!r}. Tried {_MODEL_FILENAME_CANDIDATES}."
        ) from last_exc

    def _resolve_tokenizer_path(self) -> Path:
        from huggingface_hub import hf_hub_download

        return Path(
            hf_hub_download(
                repo_id=self._model_id,
                filename=_TOKENIZER_FILENAME,
                cache_dir=str(self._cache_dir) if self._cache_dir else None,
            )
        )

    def _init(self) -> None:
        """Lazy session + tokenizer initialisation.

        Imports onnxruntime / tokenizers inside the function so the
        module is cheap to import (matches ``LocalEmbeddingFunction``
        in ``nexus.db.local_ef``).
        """
        if self._session is not None:
            return
        with self._init_lock:
            if self._session is not None:
                return
            import onnxruntime as ort
            from tokenizers import Tokenizer

            model_path = self._resolve_model_path()
            tokenizer_path = self._resolve_tokenizer_path()
            self._session = ort.InferenceSession(
                str(model_path),
                providers=["CPUExecutionProvider"],
            )
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
            _log.debug(
                "local_cross_encoder_initialised",
                model=self._model_id,
                providers=self._session.get_providers(),
            )

    def score(self, query: str, documents: list[str]) -> list[float]:
        """Return one relevance score per document for *query*.

        Higher score = more relevant. Scores are the raw logit from the
        cross-encoder head (typical range roughly -10 to +10 for MS-MARCO
        trained models); callers that need probabilities should apply
        their own sigmoid.

        Empty ``documents`` returns ``[]`` without initialising the
        session (cheap no-op).
        """
        if not documents:
            return []
        self._init()
        assert self._tokenizer is not None
        assert self._session is not None

        # ``tokenizers`` encode_batch returns one ``Encoding`` per item.
        # Pair encoding: tuple of (query, doc) per row.
        encodings = self._tokenizer.encode_batch(
            [(query, doc) for doc in documents]
        )
        # Build padded numpy arrays for onnxruntime.
        import numpy as np

        max_len = max(len(e.ids) for e in encodings)
        input_ids = np.zeros((len(encodings), max_len), dtype=np.int64)
        attention_mask = np.zeros((len(encodings), max_len), dtype=np.int64)
        token_type_ids = np.zeros((len(encodings), max_len), dtype=np.int64)
        for i, enc in enumerate(encodings):
            n = len(enc.ids)
            input_ids[i, :n] = enc.ids
            attention_mask[i, :n] = enc.attention_mask
            token_type_ids[i, :n] = enc.type_ids

        feeds: dict[str, Any] = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        # token_type_ids is required by BERT-style cross-encoders but
        # not by every architecture; only feed it if the session
        # expects it.
        expected_inputs = {inp.name for inp in self._session.get_inputs()}
        if "token_type_ids" in expected_inputs:
            feeds["token_type_ids"] = token_type_ids

        outputs = self._session.run(None, feeds)
        # MS-MARCO cross-encoder exports as a 2D ``logits`` tensor
        # shaped (batch, 1) or (batch,). Flatten and return as floats.
        logits = np.asarray(outputs[0]).reshape(-1)
        return [float(v) for v in logits]


_singleton_lock = threading.Lock()
_singleton: LocalCrossEncoder | None = None


def get_local_cross_encoder(model_id: str = _DEFAULT_MODEL_ID) -> LocalCrossEncoder:
    """Return a process-wide cached :class:`LocalCrossEncoder`.

    Single-singleton-per-model is the common case; tests that need a
    fresh instance construct one directly.
    """
    global _singleton
    if _singleton is not None and _singleton.model_id == model_id:
        return _singleton
    with _singleton_lock:
        if _singleton is None or _singleton.model_id != model_id:
            _singleton = LocalCrossEncoder(model_id=model_id)
    return _singleton


def _reset_singleton() -> None:
    """Test hook: drop the cached singleton."""
    global _singleton
    with _singleton_lock:
        _singleton = None


def cross_encoder_available() -> bool:
    """Return True if the local cross-encoder substrate can plausibly
    initialise. Checks importability of ``onnxruntime``, ``tokenizers``,
    and ``huggingface_hub``; does NOT download the model.

    Used by ``nx doctor`` to differentiate ``not installed`` from
    ``model not downloaded yet``.
    """
    for mod in ("onnxruntime", "tokenizers", "huggingface_hub"):
        try:
            __import__(mod)
        except ImportError:
            return False
    return True
