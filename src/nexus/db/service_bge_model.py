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
fp32, ~416 MB) instead. The fastembed cache (``_warmup_bge`` in init.py) serves
the *Python* local embedder and is a different artifact at a different path.

The destination MUST match the Java side's ``Bge768Embedder.DEFAULT_MODEL_PATH``
(``~/.cache/nexus/onnx_models/bge-base-en-v1.5/onnx/{model.onnx,tokenizer.json}``);
``tests/db/test_service_bge_model.py`` cross-checks the two so they cannot drift.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import structlog

_log = structlog.get_logger(__name__)

#: Standard (un-fused) transformers.js bge export. NOT fastembed's optimized cache.
STANDARD_BGE_REPO = "Xenova/bge-base-en-v1.5"
_HF_RESOLVE = "https://huggingface.co/{repo}/resolve/main/{path}"
_MODEL_REPO_PATH = "onnx/model.onnx"
_TOKENIZER_REPO_PATH = "tokenizer.json"

MODEL_FILENAME = "model.onnx"
TOKENIZER_FILENAME = "tokenizer.json"

#: Operator/test override for the destination dir. MUST match the Java service's
#: ``-Dnexus.bge.modelPath`` parent when set there too.
_ENV_DIR = "NX_SERVICE_BGE_DIR"

#: One-time download size of the STANDARD fp32 export (NOT the ~140 MB fastembed
#: quantized model — that variant is rejected by the parity gate, RDR-160 CA-3).
SERVICE_BGE_DOWNLOAD_HINT = "~416 MB"

#: ``(url, dest)`` → streams ``url`` into ``dest``. Injectable for tests.
Downloader = Callable[[str, Path], None]


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
    """True when both the model and tokenizer are already at the Java-read path."""
    d = service_bge_model_dir()
    return (d / MODEL_FILENAME).is_file() and (d / TOKENIZER_FILENAME).is_file()


def _httpx_stream(url: str, dest: Path) -> None:
    """Stream ``url`` to ``dest`` atomically (``.part`` then rename)."""
    import httpx

    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
    os.replace(tmp, dest)


def fetch_service_bge_onnx(
    *, force: bool = False, downloader: Downloader | None = None
) -> Path:
    """Fetch the standard bge ONNX + tokenizer to the Java-read path. Fail loud.

    Idempotent: a no-op when both files already exist (unless ``force``). On any
    network/IO failure raises ``RuntimeError`` with an actionable message — the
    service cannot embed without this file, so there is no silent fallback
    (RDR-160). ``downloader`` is injectable for tests; production streams from the
    HuggingFace CDN via httpx.

    @return the destination directory.
    """
    dest_dir = service_bge_model_dir()
    model_dest = dest_dir / MODEL_FILENAME
    tok_dest = dest_dir / TOKENIZER_FILENAME

    if not force and model_dest.is_file() and tok_dest.is_file():
        _log.debug("service_bge_already_present", dir=str(dest_dir))
        return dest_dir

    fetch = downloader or _httpx_stream
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        fetch(_HF_RESOLVE.format(repo=STANDARD_BGE_REPO, path=_MODEL_REPO_PATH), model_dest)
        fetch(_HF_RESOLVE.format(repo=STANDARD_BGE_REPO, path=_TOKENIZER_REPO_PATH), tok_dest)
    except Exception as exc:
        raise RuntimeError(
            f"failed to provision the standard bge-768 ONNX for the service "
            f"(offline or download failed): {exc}. The Java service embeds local "
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
