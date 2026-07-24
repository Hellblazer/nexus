# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Direct ONNX MiniLM-L6-v2 embedding function — the nexus-owned
successor to chroma's bundled ``ONNXMiniLM_L6_V2`` (RDR-155 P4b P0b,
Hal decision 3: no client EF may depend on chromadb).

Byte-parity contract (pinned differentially by
``tests/db/test_minilm_direct.py`` while the chroma oracle is still
installed): same artifact, same preprocessing — ``tokenizer.json`` via
the ``tokenizers`` Rust tokenizer with truncation+padding to 256, int64
inputs with zeroed ``token_type_ids``, attention-weighted mean pooling,
L2 normalization, float32.

Artifact compatibility is deliberate on BOTH axes:

* **Cache path**: ``~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx``
  — the exact directory existing installs already have populated (no
  re-download at upgrade) and the exact path the Java engine's
  ``OnnxEmbedder`` reads (client/engine parity is artifact-level).
* **Download**: the same chroma-S3 tarball + sha256 the engine's CI
  fetches directly — no chromadb code involved.

Runtime deps: ``onnxruntime`` + ``tokenizers`` — first-class deps as of
P0b (previously transitive via chromadb), shared with
:mod:`nexus.cross_encoder`.
"""
from __future__ import annotations

import hashlib
import tarfile
import threading
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

MODEL_NAME = "all-MiniLM-L6-v2"

#: Chroma-compatible cache layout (see module docstring — load-bearing
#: for artifact reuse and engine parity; do not "clean up" to a
#: nexus-named directory).
DOWNLOAD_PATH: Path = Path.home() / ".cache" / "chroma" / "onnx_models" / MODEL_NAME
ARTIFACT_DIR: Path = DOWNLOAD_PATH / "onnx"

_ARCHIVE_FILENAME = "onnx.tar.gz"
_MODEL_DOWNLOAD_URL = (
    "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz"
)
_MODEL_SHA256 = "913d7300ceae3b2dbc2c50d1de4baacab4be7b9380491c27fab7418616a16ec3"

_MAX_TOKENS = 256


def ensure_artifact() -> Path:
    """Download + extract the MiniLM ONNX artifact if absent (idempotent).

    Fail-loud on sha256 mismatch — never runs an unverified model.
    Returns :data:`ARTIFACT_DIR`.
    """
    if (ARTIFACT_DIR / "model.onnx").is_file() and (
        ARTIFACT_DIR / "tokenizer.json"
    ).is_file():
        return ARTIFACT_DIR

    import httpx  # noqa: PLC0415 — download path only, keep import cheap

    DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)
    archive = DOWNLOAD_PATH / _ARCHIVE_FILENAME
    _log.info("minilm_artifact_download_start", url=_MODEL_DOWNLOAD_URL)
    digest = hashlib.sha256()
    with httpx.stream("GET", _MODEL_DOWNLOAD_URL, follow_redirects=True) as resp:
        resp.raise_for_status()
        with archive.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=65536):
                fh.write(chunk)
                digest.update(chunk)
    if digest.hexdigest() != _MODEL_SHA256:
        archive.unlink(missing_ok=True)
        raise RuntimeError(
            f"MiniLM artifact sha256 mismatch: got {digest.hexdigest()}, "
            f"expected {_MODEL_SHA256} — refusing to extract."
        )
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(DOWNLOAD_PATH, filter="data")
    _log.info("minilm_artifact_ready", path=str(ARTIFACT_DIR))
    return ARTIFACT_DIR


class MiniLMDirectEmbeddingFunction:
    """Chroma-EF-protocol MiniLM embedder over onnxruntime directly.

    Drop-in for ``chromadb.utils.embedding_functions.ONNXMiniLM_L6_V2``
    at every surviving call site (``LocalEmbeddingFunction`` tier-0
    branch routes here as of P0b). Lazy session init, thread-safe.
    """

    def __init__(self) -> None:
        self._session: Any = None
        self._tokenizer: Any = None
        self._lock = threading.Lock()

    # ── chroma EF protocol ─────────────────────────────────────────────

    @staticmethod
    def name() -> str:
        return "onnx_mini_lm_l6_v2"

    @staticmethod
    def is_legacy() -> bool:
        return True

    def embed_query(self, input: list[str]) -> list[list[float]]:  # noqa: A002 — chroma EF protocol name
        return self(input)

    # ── init + forward ─────────────────────────────────────────────────

    def _ensure_ready(self) -> None:
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:
                return
            import onnxruntime  # noqa: PLC0415 — heavy dep deferred to first use
            from tokenizers import Tokenizer  # noqa: PLC0415 — heavy dep deferred to first use

            artifact = ensure_artifact()
            tokenizer = Tokenizer.from_file(str(artifact / "tokenizer.json"))
            # sentence-transformers uses 256 despite the HF config's 128 —
            # mirrored from the chroma oracle for output parity.
            tokenizer.enable_truncation(max_length=_MAX_TOKENS)
            tokenizer.enable_padding(pad_id=0, pad_token="[PAD]", length=_MAX_TOKENS)
            so = onnxruntime.SessionOptions()
            self._session = onnxruntime.InferenceSession(
                str(artifact / "model.onnx"), sess_options=so,
                providers=["CPUExecutionProvider"],
            )
            self._tokenizer = tokenizer

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002 — chroma EF protocol name
        import numpy as np  # noqa: PLC0415 — heavy dep deferred

        self._ensure_ready()
        out: list[list[float]] = []
        for start in range(0, len(input), 32):
            batch = input[start:start + 32]
            encoded = [self._tokenizer.encode(d) for d in batch]
            for e in encoded:
                if len(e.ids) > _MAX_TOKENS:
                    raise ValueError(
                        f"Document length {len(e.ids)} is greater than the "
                        f"max tokens {_MAX_TOKENS}"
                    )
            input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
            attention_mask = np.array(
                [e.attention_mask for e in encoded], dtype=np.int64
            )
            onnx_input = {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": np.zeros_like(input_ids),
            }
            last_hidden = self._session.run(None, onnx_input)[0]
            mask = np.broadcast_to(
                np.expand_dims(attention_mask, -1), last_hidden.shape
            )
            summed = np.sum(last_hidden * mask, 1)
            counts = np.clip(mask.sum(1), a_min=1e-9, a_max=None)
            emb = summed / counts
            norms = np.linalg.norm(emb, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1e-12, norms)
            emb = (emb / norms).astype(np.float32)
            out.extend(v.tolist() for v in emb)
        return out
