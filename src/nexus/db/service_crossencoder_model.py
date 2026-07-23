# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Provision the ms-marco-MiniLM cross-encoder ONNX for the Java service (RDR-188 P1.3).

The Java engine's local-mode reranker (``CrossEncoderReranker``) scores
``(query, document)`` pairs with ``cross-encoder/ms-marco-MiniLM-L-6-v2``
(~91 MB fp32 ONNX). Same topology invariant as the bge-768 flow
(:mod:`nexus.db.service_bge_model`): the CLI is the network-facing side and
writes the artifacts to a stable Java-read path; the local service only READS
the files and makes no outbound HTTP.

Source: HuggingFace ``resolve`` URLs at a PINNED revision, sha256-verified —
the exact artifact the retiring client-side ``LocalCrossEncoder``
(``cross_encoder.py``) has lazily pulled from the same repo since RDR-109, so
the download origin is status quo for this model. Unlike bge (416 MB,
nexus-5votw moved it to a self-hosted release asset after anonymous-HF 429
flakes), this file is 91 MB and has not exhibited the flake class; if it ever
does, mirror it into a ``ci-assets-*`` release tag the same way.

The destination MUST match the Java side's
``CrossEncoderReranker.DEFAULT_MODEL_PATH``
(``~/.cache/nexus/onnx_models/ms-marco-minilm-l6-v2/onnx/{model.onnx,tokenizer.json}``);
``tests/db/test_service_crossencoder_model.py`` cross-checks the two so they
cannot drift.

Failure posture: unlike bge (the service's ONLY embedder — init aborts), a
missing cross-encoder does not stop the engine from serving; the fused rerank
stage degrades LOUD per request (``rerank_degraded=true``) until the model is
provisioned. Init therefore surfaces a provisioning failure loudly but does
not abort the install; ``nx doctor`` keeps flagging the gap.
"""
from __future__ import annotations

import os
from pathlib import Path

import structlog

# Shared provisioning plumbing (atomic stream + retry/backoff + digest gate) —
# single implementation, both model flows (no drift).
from nexus.db.service_bge_model import Downloader, _file_ok, _httpx_stream, _verify_sha256

_log = structlog.get_logger(__name__)

#: The model repo — the SAME repo the retiring client ``LocalCrossEncoder``
#: pulls, so provenance is unchanged by the server-side move.
CROSSENCODER_REPO = "cross-encoder/ms-marco-MiniLM-L-6-v2"

#: Pinned repo revision (immutable content address). Bump deliberately, with
#: fresh digests, never track a moving branch.
_PINNED_REVISION = "c5ee24cb16019beea0893ab7796b1df96625c6b8"

#: Pinned sha256 of the artifacts at the pinned revision. A mismatch
#: (tampered/corrupt/truncated download) fails loud and removes the file.
_MODEL_SHA256 = "5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a"
_TOKENIZER_SHA256 = "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"

MODEL_FILENAME = "model.onnx"
TOKENIZER_FILENAME = "tokenizer.json"

#: Repo-relative source paths at the pinned revision (the ONNX lives under the
#: ``onnx/`` subfolder in this repo's canonical layout).
_MODEL_REPO_PATH = "onnx/model.onnx"
_TOKENIZER_REPO_PATH = "tokenizer.json"

#: Operator/test override for the destination dir. MUST match the Java side's
#: ``-Dnexus.crossencoder.modelPath`` parent when set there too.
_ENV_DIR = "NX_SERVICE_CROSSENCODER_DIR"

#: One-time download size (fp32 export; keeps init messaging honest).
SERVICE_CROSSENCODER_DOWNLOAD_HINT = "~91 MB"

#: Sanity floors: the fp32 model is ~91 MB, tokenizer.json ~700 KB. A file
#: below its floor reads as ABSENT so idempotency never locks in a truncated
#: artifact (same discipline as the bge floors). Module-level so tests can lower.
_MIN_MODEL_BYTES = 50_000_000
_MIN_TOKENIZER_BYTES = 100_000


def service_crossencoder_model_dir() -> Path:
    """Canonical dir the Java service reads the cross-encoder from.

    ``NX_SERVICE_CROSSENCODER_DIR`` overrides (operator/test); otherwise the
    XDG-ish default mirroring ``CrossEncoderReranker.DEFAULT_MODEL_PATH``.
    """
    env = os.environ.get(_ENV_DIR, "").strip()
    if env:
        return Path(env)
    return Path.home() / ".cache" / "nexus" / "onnx_models" / "ms-marco-minilm-l6-v2" / "onnx"


def service_crossencoder_model_present() -> bool:
    """True when a COMPLETE model + tokenizer are at the Java-read path
    (size floors applied, so truncated artifacts read as not-present)."""
    d = service_crossencoder_model_dir()
    return _file_ok(d / MODEL_FILENAME, _MIN_MODEL_BYTES) and _file_ok(
        d / TOKENIZER_FILENAME, _MIN_TOKENIZER_BYTES
    )


def fetch_service_crossencoder_onnx(
    *, force: bool = False, downloader: Downloader | None = None
) -> Path:
    """Fetch the cross-encoder ONNX + tokenizer to the Java-read path. Fail loud.

    Idempotent on complete files (size floors); the download path is
    digest-verified against the pinned revision. Raises ``RuntimeError`` with
    an actionable message on any network/IO/digest failure — the CALLER decides
    whether that is fatal (bge: yes, the service cannot boot; cross-encoder:
    no, rerank degrades loud until provisioned).

    @return the destination directory.
    """
    dest_dir = service_crossencoder_model_dir()
    model_dest = dest_dir / MODEL_FILENAME
    tok_dest = dest_dir / TOKENIZER_FILENAME

    if not force and _file_ok(model_dest, _MIN_MODEL_BYTES) and _file_ok(
        tok_dest, _MIN_TOKENIZER_BYTES
    ):
        _log.debug("service_crossencoder_already_present", dir=str(dest_dir))
        return dest_dir

    fetch = downloader or _httpx_stream
    dest_dir.mkdir(parents=True, exist_ok=True)

    base = f"https://huggingface.co/{CROSSENCODER_REPO}/resolve/{_PINNED_REVISION}"
    try:
        fetch(f"{base}/{_MODEL_REPO_PATH}", model_dest)
        _verify_sha256(model_dest, _MODEL_SHA256, MODEL_FILENAME)
        fetch(f"{base}/{_TOKENIZER_REPO_PATH}", tok_dest)
        _verify_sha256(tok_dest, _TOKENIZER_SHA256, TOKENIZER_FILENAME)
    except Exception as exc:
        # Don't leave a lone model.onnx: a half-provisioned dir would re-fetch
        # the 91 MB model on retry and reads as a failed install.
        if model_dest.is_file() and not tok_dest.is_file():
            model_dest.unlink(missing_ok=True)
        raise RuntimeError(
            f"failed to provision the ms-marco cross-encoder ONNX for the service "
            f"(download or verification failed): {exc}. Local-mode server-side rerank "
            f"stays degraded (loud, per request) until {model_dest} exists. Re-run "
            f"`nx init` when back online, or set {_ENV_DIR} to a directory holding "
            f"model.onnx + tokenizer.json from {CROSSENCODER_REPO}@{_PINNED_REVISION}."
        ) from exc

    if not (model_dest.is_file() and tok_dest.is_file()):
        raise RuntimeError(
            f"cross-encoder provisioning incomplete: expected {MODEL_FILENAME} and "
            f"{TOKENIZER_FILENAME} under {dest_dir}."
        )
    _log.info("service_crossencoder_provisioned", dir=str(dest_dir))
    return dest_dir
