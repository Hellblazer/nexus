# SPDX-License-Identifier: AGPL-3.0-or-later
"""``HttpTokenStore.chunks_leaves``: the client of the engine's operator-only ``GET /v1/admin/chunks-leaves``
(nexus-i827c). The engine side (401 without a credential, 403 for a tenant or mint token, 200 for the root token) is
pinned by ``ChunksLeavesRouteTest``; this pins what the client does with each answer: a missing route (an older
engine) is ``None``, a refused credential is the token-admin auth error with no secret in it, and anything else is
loud."""
from __future__ import annotations

import httpx
import pytest

from nexus.db.t2.http_token_store import HttpTokenStore, TokenAdminAuthError

_TOKEN = "tok-root-never-in-a-message-0123456789ab"
_BASE = "http://127.0.0.1:9"


def _store(handler) -> tuple[HttpTokenStore, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    store = HttpTokenStore(base_url=_BASE, _token=_TOKEN)
    store._client.close()
    store._client = httpx.Client(
        base_url=_BASE, transport=httpx.MockTransport(wrapped),
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    return store, seen


def test_a_200_returns_the_body_and_sends_a_get_with_the_bearer() -> None:
    body = {"leaves_total": 2, "needing_attention": 1, "leaves": [{"leaf": "chunks_x", "problems": ["pinned"]}]}
    store, seen = _store(lambda r: httpx.Response(200, json=body))
    with store:
        assert store.chunks_leaves() == body
    (req,) = seen
    assert req.method == "GET" and req.url.path == "/v1/admin/chunks-leaves"
    assert req.headers["Authorization"] == f"Bearer {_TOKEN}"


def test_a_404_is_an_engine_that_predates_the_route_not_an_error() -> None:
    store, _ = _store(lambda r: httpx.Response(404, json={"error": "not found"}))
    with store:
        assert store.chunks_leaves() is None


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_credential_is_the_token_admin_auth_error_with_no_secret_in_it(status: int) -> None:
    store, _ = _store(lambda r: httpx.Response(status, json={"error": f"refused; you sent {_TOKEN}"}))
    with store, pytest.raises(TokenAdminAuthError) as err:
        store.chunks_leaves()
    assert err.value.response.status_code == status
    assert _TOKEN not in str(err.value) and not any(_TOKEN in line for line in err.value.lines)


def test_a_server_error_is_loud() -> None:
    store, _ = _store(lambda r: httpx.Response(500, json={"error": "boom"}))
    with store, pytest.raises(httpx.HTTPStatusError):
        store.chunks_leaves()


def test_a_body_that_is_not_an_object_is_a_value_error() -> None:
    store, _ = _store(lambda r: httpx.Response(200, json=["not", "an", "object"]))
    with store, pytest.raises(ValueError, match="not an object"):
        store.chunks_leaves()


def test_a_read_never_calls_the_production_write_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **kw):
        raise AssertionError("a GET read must not call the production write guard")

    monkeypatch.setattr("nexus.db.service_endpoint.guard_production_write", boom)
    store, _ = _store(lambda r: httpx.Response(200, json={"leaves": []}))
    with store:
        assert store.chunks_leaves() == {"leaves": []}
