# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 (nexus-p2m1x.17, linda 2026-11-22): the seed helpers' raw-row fallback seeds a registry row only for
the embedding-profile refusal of a registration; every other registration failure raises."""

from __future__ import annotations

import httpx
import pytest

import tests._catalog_fixture_ops as ops
from nexus.corpus import EmbeddingProfileMismatchError


def _http_error(status: int, text: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://engine/v1/catalog/collections/upsert")
    return httpx.HTTPStatusError(text, request=request, response=httpx.Response(status, request=request))


_PROFILE_422 = _http_error(
    422, "HTTP 422: the embedding profile for content_type 'docs' is 'bge-base-en-v15-768'; refusing to register "
         "'docs__x__voyage-context-3__v1' with embedding_model 'voyage-context-3'")


@pytest.mark.parametrize("exc", [
    EmbeddingProfileMismatchError("docs", "voyage-context-3", "bge-base-en-v15-768"),
    _PROFILE_422,
], ids=["client-check", "engine-422"])
def test_the_profile_refusal_seeds_the_row(monkeypatch, exc):
    seeded: list[str] = []
    monkeypatch.setattr(ops, "_register_collection_row", seeded.append)

    def _refuse(name, *a, **k):
        raise exc

    assert ops.registered_or_seeded(_refuse)("docs__x__voyage-context-3__v1") is None
    assert seeded == ["docs__x__voyage-context-3__v1"]


@pytest.mark.parametrize("exc", [
    RuntimeError("catalog unreachable"),
    httpx.ConnectError("connection refused"),
    _http_error(422, "HTTP 422: collection name 'bad name' is not a valid label"),
    _http_error(500, "HTTP 500: internal server error"),
], ids=["runtime", "connect", "other-422", "server-500"])
def test_any_other_failure_raises_and_seeds_nothing(monkeypatch, exc):
    seeded: list[str] = []
    monkeypatch.setattr(ops, "_register_collection_row", seeded.append)

    def _fail(name, *a, **k):
        raise exc

    with pytest.raises(type(exc)):
        ops.registered_or_seeded(_fail)("docs__x__voyage-context-3__v1")
    assert seeded == [], "a non-profile failure must not fall back to a raw registry row"
