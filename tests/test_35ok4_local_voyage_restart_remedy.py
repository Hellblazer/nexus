# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-35ok4 round 2 (code-review-expert IMPORTANT 2): the local
``local.embed_model``-just-changed-to-voyage restart race.

The client mints voyage-* collection names off static config the instant
``local.embed_model`` is set; the ALREADY-RUNNING engine only reads
``NX_VOYAGE_API_KEY`` at process spawn, so it 422s until restarted. This
suite pins :func:`nexus.db.http_vector_client._local_voyage_restart_remedy`
— the client-side interception point that reframes exactly that 422 shape
with an actionable restart hint — and its wiring into ``_post``/``_get``.
"""
from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

import pytest

from nexus.db.http_vector_client import (
    VectorServiceError,
    _get,
    _local_voyage_restart_remedy,
    _post,
)

# The EXACT sentinel substring EmbedderRouter.resolveEmbedderStrict emits
# (service/src/main/java/dev/nexus/service/vectors/EmbedderRouter.java)
# ONLY when the engine is in onnx-local mode and refuses a voyage-*
# collection's model segment — the restart-race shape this remedy targets.
_ENGINE_ONNX_LOCAL_REFUSAL = (
    "service (embedding mode onnx-local) has no embedder for model "
    "'voyage-code-3' — refusing to embed collection "
    "'code__myrepo__voyage-code-3__v1' with a different model. "
    "Available models: [bge-base-en-v15-768]. Voyage collections need "
    "NX_VOYAGE_API_KEY in the service environment (supervisor plumbs it "
    "from the nexus credential chain when set)."
)


# ── _local_voyage_restart_remedy (pure function) ─────────────────────────


def test_remedy_fires_on_engine_onnx_local_refusal() -> None:
    remedy = _local_voyage_restart_remedy(422, _ENGINE_ONNX_LOCAL_REFUSAL)
    assert remedy is not None
    assert "nx daemon service stop && nx daemon service start" in remedy
    assert "NX_VOYAGE_API_KEY" in remedy


def test_remedy_none_for_non_422_code() -> None:
    assert _local_voyage_restart_remedy(500, _ENGINE_ONNX_LOCAL_REFUSAL) is None


def test_remedy_none_for_unrelated_422() -> None:
    """A genuinely different 422 (e.g. a malformed collection name) must
    NOT be reframed with the voyage-restart hint — the sentinel substring
    match keeps this scoped to the engine's own onnx-local refusal."""
    assert (
        _local_voyage_restart_remedy(
            422, "Collection name 'x' must be 3–63 characters (got 1)",
        )
        is None
    )


def test_remedy_none_for_empty_message() -> None:
    assert _local_voyage_restart_remedy(422, "") is None


# ── wiring into _post / _get ──────────────────────────────────────────────


class _FakeFP:
    """Minimal file-like object HTTPError.read() delegates to."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass


def _http_error(code: int, message: str) -> urllib.error.HTTPError:
    body = json.dumps({"error": message}).encode()
    return urllib.error.HTTPError(
        "http://engine.internal/v1/vectors/upsert-chunks",
        code,
        "error",
        None,
        _FakeFP(body),
    )


def test_post_wraps_onnx_local_refusal_with_restart_remedy() -> None:
    err = _http_error(422, _ENGINE_ONNX_LOCAL_REFUSAL)
    with patch("nexus.db.http_vector_client._request", side_effect=err):
        with pytest.raises(VectorServiceError) as exc_info:
            _post("/v1/vectors/upsert-chunks", {})
    assert exc_info.value.code == 422
    assert "nx daemon service stop && nx daemon service start" in str(exc_info.value)


def test_get_wraps_onnx_local_refusal_with_restart_remedy() -> None:
    err = _http_error(422, _ENGINE_ONNX_LOCAL_REFUSAL)
    with patch("nexus.db.http_vector_client._request", side_effect=err):
        with pytest.raises(VectorServiceError) as exc_info:
            _get("/v1/vectors/some-collection/count")
    assert exc_info.value.code == 422
    assert "nx daemon service stop && nx daemon service start" in str(exc_info.value)


def test_post_unrelated_422_unchanged() -> None:
    """A genuinely different 422 keeps its original message — no false
    positive reframing."""
    err = _http_error(422, "Collection name 'x' must be 3–63 characters (got 1)")
    with patch("nexus.db.http_vector_client._request", side_effect=err):
        with pytest.raises(VectorServiceError) as exc_info:
            _post("/v1/vectors/upsert-chunks", {})
    assert "nx daemon service stop" not in str(exc_info.value)
