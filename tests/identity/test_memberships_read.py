# SPDX-License-Identifier: AGPL-3.0-or-later
"""``IdentityClient.memberships`` (RDR-252 Step 4 part A, bead nexus-igea9.7): the read the aura load stage makes
once at server start, against RDR-253 Step 1's ``GET /v1/identity/<id>/memberships``. A mock transport stands in for
the engine; the route itself is the engine's (``IdentityHandler``)."""
from __future__ import annotations

import httpx
import pytest

from nexus.identity.bind_client import IdentityApiError, IdentityClient, IdentityUnavailableError


def _client(handler) -> IdentityClient:
    return IdentityClient("http://engine.test", "tok", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_memberships_reads_the_route_and_returns_the_rows_in_join_order() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"identity": "id-a", "memberships": [
            {"identity": "id-a", "group": "push-train", "role": "leader", "joined_at": "2026-11-17T08:00:00Z"},
            {"identity": "id-a", "group": "review-pair", "role": "member", "joined_at": "2026-11-18T08:00:00Z"},
        ]})

    got = _client(handler).memberships("id-a")
    assert [r["group"] for r in got] == ["push-train", "review-pair"]
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v1/identity/id-a/memberships")]
    assert seen[0].headers["Authorization"] == "Bearer tok"


def test_an_identity_with_no_memberships_is_an_empty_list() -> None:
    assert _client(lambda _r: httpx.Response(200, json={"identity": "id-a", "memberships": []})).memberships("id-a") == []


def test_an_unknown_identity_is_the_engines_refusal() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "unknown_identity", "message": "no identity id-z"})

    with pytest.raises(IdentityApiError) as err:
        _client(handler).memberships("id-z")
    assert err.value.code == "unknown_identity"


def test_an_engine_that_cannot_be_reached_is_unavailable() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(IdentityUnavailableError):
        _client(handler).memberships("id-a")


def test_a_2xx_answer_that_is_not_a_json_object_is_an_error_never_no_memberships() -> None:
    for body in ("<html>portal</html>", "[]", "null"):
        with pytest.raises(IdentityApiError):
            _client(lambda _r, b=body: httpx.Response(200, text=b)).memberships("id-a")


def test_an_answer_with_no_memberships_list_is_an_error_not_an_empty_list() -> None:
    for payload in ({"identity": "id-a"}, {"memberships": "x"}, {"memberships": None}):
        with pytest.raises(IdentityApiError):
            _client(lambda _r, p=payload: httpx.Response(200, json=p)).memberships("id-a")


def test_the_identity_is_quoted_into_the_path() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"memberships": []})

    _client(handler).memberships("a/b c")
    assert seen[0].url.raw_path.decode().split("?")[0] == "/v1/identity/a%2Fb%20c/memberships"
