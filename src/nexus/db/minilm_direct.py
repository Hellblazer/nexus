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
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

import structlog

from nexus._locking import lock_file, unlock_file

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

#: Serializes first-use provisioning across threads and processes. Lives inside
#: :func:`download_path`, is never deleted (unlinking a lock file races the next
#: opener), and is not part of the artifact the engine reads (``onnx/``).
_LOCK_FILENAME = ".ensure.lock"

#: Temps and the pre-nexus-ccre5 shared archive older than this are leftovers of
#: a killed download (a CI cancel, SIGKILL) and are swept when a download starts.
_STALE_TEMP_AGE_S = 24 * 60 * 60

#: Transient-HTTP retry, same shape as ``service_bge_model._httpx_stream``:
#: transport errors and HTTP 429/5xx retry with backoff; any other status raises.
_RETRY_ATTEMPTS = 5
_RETRY_BACKOFF_S: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0)
_retry_sleep: Callable[[float], None] = time.sleep


class _DigestMismatch(RuntimeError):
    """The downloaded bytes do not hash to :data:`_MODEL_SHA256`. Never retried
    and never papered over by a peer's artifact: fail loud."""


def _artifact_complete(artifact: Path) -> bool:
    return (artifact / "model.onnx").is_file() and (
        artifact / "tokenizer.json"
    ).is_file()


def ensure_artifact() -> Path:
    """Download + extract the MiniLM ONNX artifact if absent (idempotent).

    Fail-loud on sha256 mismatch — never runs an unverified model.
    Returns :func:`artifact_dir`.

    Safe under concurrent first use (nexus-ccre5). Before it, all callers shared
    one ``onnx.tar.gz`` and one extraction target, and a peer truncating the
    archive failed another caller's ``extractall`` with ``EOFError`` (CI run
    36962623595, eight xdist workers on a cold cache). Now:

    * A complete artifact is returned without taking any lock (the fast path).
    * Otherwise the caller takes an exclusive file lock in :func:`download_path`,
      re-checks completeness (a waiter takes the holder's result and downloads
      nothing, so eight cold workers make one S3 request, not eight), sweeps
      stale temps, then streams into its OWN temp archive and extracts into its
      OWN temp directory, both inside :func:`download_path` so the final rename
      stays on one filesystem, and publishes ``onnx/`` with one rename.
      Leftover cleanup of an incomplete ``onnx/`` (a pre-fix crash) happens in
      that same critical section, so it can never delete a peer's fresh publish.
    * A transient HTTP failure (429/5xx, transport error) retries with backoff.
      Any other download failure first re-checks for a complete artifact a
      non-locking peer (an older nexus) published meanwhile and returns it.
    """
    artifact = artifact_dir()
    if _artifact_complete(artifact):
        return artifact

    download = download_path()
    download.mkdir(parents=True, exist_ok=True)
    with (download / _LOCK_FILENAME).open("a+") as lock_fh:
        lock_file(lock_fh, blocking=True)
        try:
            if _artifact_complete(artifact):
                _log.info("minilm_artifact_peer_published", path=str(artifact))
                return artifact
            _sweep_stale_temps(download)
            _download_and_publish(artifact, download)
        finally:
            unlock_file(lock_fh)
    _log.info("minilm_artifact_ready", path=str(artifact))
    return artifact


def _sweep_stale_temps(download: Path) -> None:
    """Remove temp archives, extract dirs and the legacy shared archive that a
    killed download left behind, when older than :data:`_STALE_TEMP_AGE_S`.

    Called under the lock, only when a download is about to start. Best effort:
    a failure to remove one entry never blocks provisioning.
    """
    cutoff = time.time() - _STALE_TEMP_AGE_S
    patterns = (f".{_ARCHIVE_FILENAME}.*.part", ".onnx.extract.*", _ARCHIVE_FILENAME)
    for pattern in patterns:
        for path in download.glob(pattern):
            try:
                if path.lstat().st_mtime >= cutoff:
                    continue
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
                _log.info("minilm_stale_temp_swept", path=str(path))
            except OSError:
                _log.warning("minilm_stale_temp_sweep_failed", path=str(path))


def _stream_archive(fh: IO[bytes]) -> str:
    """One download attempt into ``fh`` (rewound and truncated first); returns
    the hex sha256 of what was written."""
    import httpx  # noqa: PLC0415 — download path only, keep import cheap

    fh.seek(0)
    fh.truncate()
    digest = hashlib.sha256()
    with httpx.stream("GET", _MODEL_DOWNLOAD_URL, follow_redirects=True) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes(chunk_size=65536):
            fh.write(chunk)
            digest.update(chunk)
    fh.flush()
    return digest.hexdigest()


def _download_with_retry(fh: IO[bytes]) -> str:
    """:func:`_stream_archive` with retry/backoff on transient failures."""
    import httpx  # noqa: PLC0415 — download path only, keep import cheap

    last: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return _stream_archive(fh)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code != 429 and code < 500:
                raise
            last = exc
        except httpx.TransportError as exc:
            last = exc
        if attempt < _RETRY_ATTEMPTS - 1:
            delay = _RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)]
            _log.warning(
                "minilm_download_retry", attempt=attempt + 1, delay_s=delay,
                error=str(last),
            )
            _retry_sleep(delay)
    assert last is not None  # the loop ran at least once without returning
    raise last


def _download_and_publish(artifact: Path, download: Path) -> None:
    """Stream, verify, extract and publish. Caller holds the provisioning lock."""
    # Both temps are created inside the try so a failure creating the second
    # cannot leak the first (nexus-ccre5 review S3).
    archive_fh = tempfile.NamedTemporaryFile(  # noqa: SIM115 — closed in finally
        "wb", dir=download, prefix=f".{_ARCHIVE_FILENAME}.", suffix=".part",
        delete=False,
    )
    archive = Path(archive_fh.name)
    stage: Path | None = None
    try:
        stage = Path(tempfile.mkdtemp(dir=download, prefix=".onnx.extract."))
        _log.info("minilm_artifact_download_start", url=_MODEL_DOWNLOAD_URL)
        got = _download_with_retry(archive_fh)
        if got != _MODEL_SHA256:
            raise _DigestMismatch(
                f"MiniLM artifact sha256 mismatch: got {got}, "
                f"expected {_MODEL_SHA256} — refusing to extract."
            )
        archive_fh.close()
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(stage, filter="data")
        staged = stage / artifact.name
        if not _artifact_complete(staged):
            raise RuntimeError(
                f"MiniLM archive did not contain {artifact.name}/model.onnx and "
                f"{artifact.name}/tokenizer.json"
            )
        _publish(staged, artifact)
    except _DigestMismatch:
        raise
    except Exception:
        # A peer that does not take the lock (an older nexus) may have published
        # while this download was failing: that artifact is as good as ours.
        if _artifact_complete(artifact):
            _log.info("minilm_artifact_peer_published", path=str(artifact))
            return
        raise
    finally:
        archive_fh.close()
        archive.unlink(missing_ok=True)
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


def _publish(staged: Path, artifact: Path) -> None:
    """Rename ``staged`` onto ``artifact``. Caller holds the provisioning lock.

    A complete ``artifact`` that appears first (an older, non-locking peer won)
    is success. An incomplete one (a crashed pre-fix extraction) is cleared once
    and the rename retried. Both halves run inside the lock, after the caller
    re-checked completeness, so no new-code peer can publish between the check
    and the ``rmtree``.
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
