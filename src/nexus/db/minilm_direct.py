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
import os
import shutil
import tarfile
import tempfile
import threading
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

MODEL_NAME = "all-MiniLM-L6-v2"

#: Overrides the cache root, the directory that holds ``MODEL_NAME``. The unit
#: suite sets it at session start to the operator's real cache
#: (``tests/conftest.py``), because a test that points HOME at a tmp dir would
#: otherwise resolve an empty cache and download the model again.
CACHE_DIR_ENV = "NX_MINILM_CACHE_DIR"


def download_path() -> Path:
    """Chroma-compatible cache root (see module docstring — load-bearing
    for artifact reuse and engine parity; do not "clean up" to a
    nexus-named directory), resolved at CALL time.

    :data:`CACHE_DIR_ENV`, when set, replaces the HOME-derived root. Tests
    that move HOME did redirect this path: the scratch suite's ``t1``
    fixture sets HOME to its tmp dir, and its embedder downloaded the
    artifact from S3 on every run until an HTTP 503 failed a full suite.

    nexus-pfuns: was a module-level ``Path.home()`` constant, frozen at
    import. Resolving at call time keeps a test that patches
    ``Path.home()`` from being defeated by a frozen constant.
    """
    override = os.environ.get(CACHE_DIR_ENV, "").strip()
    root = Path(override) if override else Path.home() / ".cache" / "chroma" / "onnx_models"
    return root / MODEL_NAME


def artifact_dir() -> Path:
    """``onnx/`` subdirectory of :func:`download_path`, resolved at CALL
    time (same rationale)."""
    return download_path() / "onnx"

_ARCHIVE_FILENAME = "onnx.tar.gz"
_MODEL_DOWNLOAD_URL = (
    "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz"
)
_MODEL_SHA256 = "913d7300ceae3b2dbc2c50d1de4baacab4be7b9380491c27fab7418616a16ec3"

_MAX_TOKENS = 256


def _artifact_complete(artifact: Path) -> bool:
    return (artifact / "model.onnx").is_file() and (
        artifact / "tokenizer.json"
    ).is_file()


def ensure_artifact() -> Path:
    """Download + extract the MiniLM ONNX artifact if absent (idempotent).

    Fail-loud on sha256 mismatch — never runs an unverified model.
    Returns :func:`artifact_dir`.

    Safe under concurrent first use (nexus-ccre5): every caller streams into
    its OWN temp archive and extracts into its OWN temp directory, both inside
    :func:`download_path` so the final rename stays on one filesystem, then
    publishes the extracted ``onnx/`` with one atomic rename. A caller that
    loses the race finds a complete artifact already in place and treats that
    as success. The shared cache path is therefore never half-written: a
    reader sees either no ``onnx/`` or a complete one. Before this, all
    callers shared one ``onnx.tar.gz`` and one extraction target, and a peer
    truncating the archive failed another caller's ``extractall`` with
    ``EOFError`` (CI run 36962623595, eight xdist workers on a cold cache).
    """
    artifact = artifact_dir()
    if _artifact_complete(artifact):
        return artifact

    import httpx  # noqa: PLC0415 — download path only, keep import cheap

    download = download_path()
    download.mkdir(parents=True, exist_ok=True)
    fd, archive_name = tempfile.mkstemp(
        dir=download, prefix=f".{_ARCHIVE_FILENAME}.", suffix=".part"
    )
    archive = Path(archive_name)
    stage = Path(tempfile.mkdtemp(dir=download, prefix=".onnx.extract."))
    try:
        _log.info("minilm_artifact_download_start", url=_MODEL_DOWNLOAD_URL)
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as fh:
            with httpx.stream("GET", _MODEL_DOWNLOAD_URL, follow_redirects=True) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes(chunk_size=65536):
                    fh.write(chunk)
                    digest.update(chunk)
        if digest.hexdigest() != _MODEL_SHA256:
            raise RuntimeError(
                f"MiniLM artifact sha256 mismatch: got {digest.hexdigest()}, "
                f"expected {_MODEL_SHA256} — refusing to extract."
            )
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(stage, filter="data")
        staged = stage / artifact.name
        if not _artifact_complete(staged):
            raise RuntimeError(
                f"MiniLM archive did not contain {artifact.name}/model.onnx and "
                f"{artifact.name}/tokenizer.json"
            )
        _publish(staged, artifact)
    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)
    _log.info("minilm_artifact_ready", path=str(artifact))
    return artifact


def _publish(staged: Path, artifact: Path) -> None:
    """Atomically rename ``staged`` onto ``artifact``.

    A complete ``artifact`` that appears first (a peer won) is success. An
    incomplete one (a crashed pre-fix extraction) is cleared once and the
    rename retried.
    """
    for attempt in (1, 2):
        try:
            os.rename(staged, artifact)
            return
        except OSError:
            # rename onto a non-empty directory: ENOTEMPTY / EEXIST.
            if _artifact_complete(artifact):
                _log.info("minilm_artifact_peer_published", path=str(artifact))
                return
            if attempt == 2:
                raise
            shutil.rmtree(artifact, ignore_errors=True)


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
