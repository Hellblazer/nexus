# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Provision the STANDARD bge-768 ONNX for the Java service (RDR-160 P3.1).

The Java nexus-service local-mode embedder ({@code Bge768Embedder}) loads a
STANDARD (un-fused) bge-base-en-v1.5 ONNX export. The CLI is the network-facing
side: it fetches the model and writes it to a stable, Java-loadable path; the
service only READS the file (topology invariant — the local Java service makes
no outbound HTTP).

CRITICAL (CA-1 / RF-160-1): this is NOT fastembed's cached
``model_optimized.onnx``. That export uses the fused MS contrib op
``SkipLayerNormalization`` which onnxruntime-java 1.20.0 cannot execute. We fetch
the standard transformers.js export (``Xenova/bge-base-en-v1.5`` ``onnx/model.onnx``,
fp32, ~416 MB) instead. The fastembed cache (the Python local-embedder warmup
path) serves the *Python* local embedder and is a different artifact at a
different path.

The destination MUST match the Java side's ``Bge768Embedder.DEFAULT_MODEL_PATH``
(``~/.cache/nexus/onnx_models/bge-base-en-v1.5/onnx/{model.onnx,tokenizer.json}``);
``tests/db/test_service_bge_model.py`` cross-checks the two so they cannot drift.

Source (nexus-5votw): the SELF-HOSTED GitHub release asset ``ci-assets-bge-768-v1``
— the same immutable assets CI's ``.github/actions/prime-bge-onnx`` consumes —
downloaded with retry/backoff and verified against pinned sha256 digests. NOT
anonymous HuggingFace: HF rate-limits anonymous bulk pulls (HTTP 429), which
failed user installs the same way it flaked CI. No fallback origin — a failed
download fails loud (feedback_no_silent_fallbacks_for_correctness).

ACCEPTED RISK: digest verification runs on the download path only. Files already
on disk are trusted via the size floors (re-hashing 416 MB on every boot is not
worth it), so a pre-5votw HuggingFace-sourced install or post-install disk
corruption above the floor is not detected; ``force=True`` re-fetches + re-verifies.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Callable

import structlog

_log = structlog.get_logger(__name__)

#: Upstream provenance of the hosted export: the standard (un-fused)
#: transformers.js ``Xenova/bge-base-en-v1.5`` ``onnx/model.onnx`` — NOT
#: fastembed's optimized cache. Kept for documentation; downloads come from the
#: self-hosted release asset below, never HuggingFace.
STANDARD_BGE_REPO = "Xenova/bge-base-en-v1.5"

#: Self-hosted GitHub release tag hosting model.onnx + tokenizer.json
#: (nexus-5votw). Same tag CI's prime-bge-onnx action consumes; the test suite
#: cross-checks the two (plus the digests) so they cannot drift. If the model
#: export is ever re-cut: publish a new asset tag, bump it in BOTH places, and
#: update the digests.
BGE_ASSET_TAG = "ci-assets-bge-768-v1"
#: Same repo releases base the engine binary installs from
#: (``nexus.daemon.binary_install._RELEASE_DOWNLOAD_BASE``); guarded by test.
_ASSET_DOWNLOAD_BASE = "https://github.com/Hellblazer/nexus/releases/download"

#: Pinned sha256 of the immutable release assets — the same values the CI
#: action verifies. A mismatch (tampered/corrupt/truncated download) fails loud.
_MODEL_SHA256 = "9bc579acdba21c253c62a9bf866891355a63ffa3442b52c8a37d75b2ccb91848"
_TOKENIZER_SHA256 = "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"

MODEL_FILENAME = "model.onnx"
TOKENIZER_FILENAME = "tokenizer.json"

#: Operator/test override for the destination dir. MUST match the Java service's
#: ``-Dnexus.bge.modelPath`` parent when set there too.
_ENV_DIR = "NX_SERVICE_BGE_DIR"

#: One-time download size of the STANDARD fp32 export (NOT the ~140 MB fastembed
#: quantized model — that variant is rejected by the parity gate, RDR-160 CA-3).
SERVICE_BGE_DOWNLOAD_HINT = "~416 MB"

#: Sanity floors for "this file is the real artifact, not a truncated download or
#: the ~140 MB quantized/fused substitute". The standard fp32 model is ~416 MB;
#: 200 MB cleanly separates it from a quantized (~140 MB) or truncated file. A
#: file below the floor is treated as ABSENT, so idempotency never locks in a
#: corrupt artifact that would later fail the service's ONNX load opaquely
#: (RDR-160 CA-1/CA-3). Module-level so tests can lower them.
_MIN_MODEL_BYTES = 200_000_000
_MIN_TOKENIZER_BYTES = 100_000

#: ``(url, dest)`` → streams ``url`` into ``dest``. MUST be atomic: write to a
#: temp path and rename on success, leaving no partial file at ``dest`` on
#: failure (the default :func:`_httpx_stream` does this). Injectable for tests.
Downloader = Callable[[str, Path], None]


def _file_ok(path: Path, min_bytes: int) -> bool:
    """True when *path* exists and is at least *min_bytes* (not truncated)."""
    try:
        return path.is_file() and path.stat().st_size >= min_bytes
    except OSError:
        return False


def service_bge_model_dir() -> Path:
    """Canonical dir the Java service reads its bge model + tokenizer from.

    ``NX_SERVICE_BGE_DIR`` overrides (operator/test); otherwise the XDG-ish
    default that mirrors ``Bge768Embedder.DEFAULT_MODEL_PATH``.
    """
    env = os.environ.get(_ENV_DIR, "").strip()
    if env:
        return Path(env)
    return Path.home() / ".cache" / "nexus" / "onnx_models" / "bge-base-en-v1.5" / "onnx"


def service_bge_model_present() -> bool:
    """True when a COMPLETE model + tokenizer are at the Java-read path.

    Applies the size floors, so a truncated download or a wrong (e.g. quantized)
    substitute reads as not-present and is re-fetched rather than silently served.
    """
    d = service_bge_model_dir()
    return _file_ok(d / MODEL_FILENAME, _MIN_MODEL_BYTES) and _file_ok(
        d / TOKENIZER_FILENAME, _MIN_TOKENIZER_BYTES
    )


#: Total attempts for the default downloader, and the sleeps between them.
_RETRY_ATTEMPTS = 5
_RETRY_BACKOFF_S: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0)
#: Injectable for tests — never patch ``time.sleep`` globally.
_retry_sleep: Callable[[float], None] = time.sleep


def _stream_once(url: str, dest: Path) -> None:
    """Stream ``url`` to ``dest`` atomically (``.part`` then rename), one attempt.

    On any failure the ``.part`` file is removed, so a stalled/aborted download
    never accumulates 400 MB of litter and never leaves a partial at ``dest``.
    """
    import httpx  # noqa: PLC0415 — deferred import — heavy ONNX/model dep loaded lazily

    tmp = dest.with_suffix(dest.suffix + ".part")
    # read=300: generous per-chunk gap so a throttled 416 MB transfer survives
    # CDN backpressure, but a genuinely stalled connection (TCP alive, zero
    # throughput) surfaces as a retryable ReadTimeout instead of hanging the
    # install forever — the retry loop above exists precisely to absorb it.
    timeout = httpx.Timeout(timeout=None, connect=30.0, read=300.0)
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    fh.write(chunk)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _httpx_stream(url: str, dest: Path) -> None:
    """Default downloader: :func:`_stream_once` with retry/backoff on transient
    failures (transport errors, HTTP 429/5xx). Non-transient HTTP errors (e.g.
    404 for a missing asset) raise immediately — retrying cannot fix them.
    """
    import httpx  # noqa: PLC0415 — deferred import — heavy ONNX/model dep loaded lazily

    last: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            _stream_once(url, dest)
            return
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
                "bge_download_retry", url=url, attempt=attempt + 1, delay_s=delay,
                error=str(last),
            )
            _retry_sleep(delay)
    assert last is not None  # loop ran at least once without returning
    raise last


def _verify_sha256(path: Path, expected: str, label: str) -> None:
    """Digest-check *path* against *expected*; on mismatch remove it and raise.

    Removing the file matters: a bad artifact left in place would pass the size
    floors and be served to the Java ONNX load, failing opaquely later.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    actual = h.hexdigest()
    if actual != expected:
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"sha256 mismatch for {label}: expected {expected}, got {actual} "
            f"(corrupt or tampered download from {BGE_ASSET_TAG})"
        )


def fetch_service_bge_onnx(
    *, force: bool = False, downloader: Downloader | None = None
) -> Path:
    """Fetch the standard bge ONNX + tokenizer to the Java-read path. Fail loud.

    Idempotent: a no-op when both files already exist (unless ``force``) —
    presence is judged by the size floors, not a re-digest, so an existing 416 MB
    install is not re-hashed on every boot; the download path digest-verifies.
    On any network/IO/digest failure raises ``RuntimeError`` with an actionable
    message — the service cannot embed without this file, so there is no silent
    fallback (RDR-160). ``downloader`` is injectable for tests; production
    streams from the self-hosted GitHub release asset (nexus-5votw) with
    retry/backoff via httpx.

    @return the destination directory.
    """
    dest_dir = service_bge_model_dir()
    model_dest = dest_dir / MODEL_FILENAME
    tok_dest = dest_dir / TOKENIZER_FILENAME

    # Idempotency keys on COMPLETE files (size floors), so a truncated or wrong
    # (e.g. quantized/fused) artifact left by a prior run is corrected, not
    # locked in — the latter would only surface as an opaque ONNX load failure.
    if not force and _file_ok(model_dest, _MIN_MODEL_BYTES) and _file_ok(
        tok_dest, _MIN_TOKENIZER_BYTES
    ):
        _log.debug("service_bge_already_present", dir=str(dest_dir))
        return dest_dir

    fetch = downloader or _httpx_stream
    dest_dir.mkdir(parents=True, exist_ok=True)

    base = f"{_ASSET_DOWNLOAD_BASE}/{BGE_ASSET_TAG}"
    try:
        fetch(f"{base}/{MODEL_FILENAME}", model_dest)
        _verify_sha256(model_dest, _MODEL_SHA256, MODEL_FILENAME)
        fetch(f"{base}/{TOKENIZER_FILENAME}", tok_dest)
        _verify_sha256(tok_dest, _TOKENIZER_SHA256, TOKENIZER_FILENAME)
    except Exception as exc:
        # Don't leave a lone model.onnx (model ok, tokenizer failed): the size-
        # floor idempotency would otherwise re-fetch the 416 MB model needlessly,
        # and a half-provisioned dir reads as a failed install.
        if model_dest.is_file() and not tok_dest.is_file():
            model_dest.unlink(missing_ok=True)
        raise RuntimeError(
            f"failed to provision the standard bge-768 ONNX for the service "
            f"(download or verification failed): {exc}. The Java service embeds local "
            f"collections with bge-768 and will not boot without "
            f"{model_dest}. Re-run `nx init --service` when back online, or set "
            f"{_ENV_DIR} to a directory holding model.onnx + tokenizer.json "
            f"(STANDARD fp32 export — NOT fastembed's model_optimized.onnx)."
        ) from exc

    if not (model_dest.is_file() and tok_dest.is_file()):
        raise RuntimeError(
            f"bge-768 provisioning incomplete: expected {MODEL_FILENAME} and "
            f"{TOKENIZER_FILENAME} under {dest_dir}."
        )
    _log.info("service_bge_provisioned", dir=str(dest_dir))
    return dest_dir
