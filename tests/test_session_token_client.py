# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-152 bead nexus-gmiaf.32.4 — client-side session-token wiring.

Covers the load-bearing client change: NX_T1_SESSION now carries the minted TOKEN (header)
while NX_T1_SESSION_ID carries the session id (body + flush-title), with a backward-
compatible bootstrap fallback. Server-side enforcement is covered by the Java
SessionTokenHandlerTest.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.db.http_scratch_store import HttpScratchStore, _HEADER_T1_SESSION
from nexus.db.t2.http_token_store import HttpTokenStore


# ── http_scratch_store: token (header) vs id (body) split ────────────────────

def test_minted_mode_splits_token_and_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_T1_SESSION", "TOKEN-minted-xyz")
    monkeypatch.setenv("NX_T1_SESSION_ID", "sess-abc")
    store = HttpScratchStore(base_url="http://127.0.0.1:1", _token="bearer")
    try:
        # Body + flush-title use the session id; the header carries the minted token.
        assert store.session_id == "sess-abc"
        assert store._headers[_HEADER_T1_SESSION] == "TOKEN-minted-xyz"
    finally:
        store.close()


def test_bootstrap_fallback_collapses_to_bare_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_T1_SESSION", "sess-only")
    monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
    store = HttpScratchStore(base_url="http://127.0.0.1:1", _token="bearer")
    try:
        # With no minted token, the bare id is both the body session_id and the header.
        assert store.session_id == "sess-only"
        assert store._headers[_HEADER_T1_SESSION] == "sess-only"
    finally:
        store.close()


def test_missing_session_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_T1_SESSION", raising=False)
    monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
    with pytest.raises(RuntimeError):
        HttpScratchStore(base_url="http://127.0.0.1:1", _token="bearer")


def test_explicit_session_id_arg_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_T1_SESSION", "TOKEN-xyz")
    monkeypatch.setenv("NX_T1_SESSION_ID", "env-id")
    store = HttpScratchStore(base_url="http://127.0.0.1:1", _token="bearer", session_id="arg-id")
    try:
        assert store.session_id == "arg-id"
        assert store._headers[_HEADER_T1_SESSION] == "TOKEN-xyz"  # token still from env
    finally:
        store.close()


# ── http_token_store: data-token preference (nexus-maf9l) ────────────────────


class _StubManager:
    """Stands in for the process DataTokenManager: armed or unarmed."""

    def __init__(self, token: str | None) -> None:
        self.token = token
        self.bearer_calls: list[tuple[str, str]] = []
        self.invalidations: list[tuple[str, str]] = []

    def bearer_for(self, base_url: str, tenant: str) -> str | None:
        self.bearer_calls.append((base_url, tenant))
        return self.token

    def invalidate(self, base_url: str, tenant: str) -> None:
        self.invalidations.append((base_url, tenant))


def _stub_manager(monkeypatch: pytest.MonkeyPatch, token: str | None) -> _StubManager:
    import nexus.db.data_token as data_token_mod

    stub = _StubManager(token)
    monkeypatch.setattr(data_token_mod, "get_data_token_manager", lambda: stub)
    return stub


def test_prefer_data_token_beats_the_static_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """nexus-maf9l: on an armed box the static token is mint-locked and the
    engine rejects it outside the mint surface — the session-lifecycle call
    sites must present the data token, exactly as every other store's
    ``bearer_for`` beats its static credential."""
    stub = _stub_manager(monkeypatch, "data-tok")
    store = HttpTokenStore(
        base_url="http://127.0.0.1:1", _token="mint-locked-static", prefer_data_token=True
    )
    try:
        assert store._auth_token == "data-tok"
        assert store._client.headers["Authorization"] == "Bearer data-tok"
        assert stub.bearer_calls == [("http://127.0.0.1:1", store._tenant)]
    finally:
        store.close()


def test_prefer_data_token_on_an_unarmed_box_keeps_the_static_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bearer_for returns None when minting is not configured: behavior is
    byte-identical to today's on every unarmed/local box."""
    _stub_manager(monkeypatch, None)
    store = HttpTokenStore(
        base_url="http://127.0.0.1:1", _token="static", prefer_data_token=True
    )
    try:
        assert store._auth_token == "static"
    finally:
        store.close()


def test_default_construction_never_touches_the_data_token_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin surface (nx tenant / service token verbs) must keep the
    static credential: the engine 403s mint- and data-scoped bearers on the
    ENTIRE admin surface, so silently preferring a data token there would
    break admin flows. Default False is load-bearing."""
    stub = _stub_manager(monkeypatch, "data-tok")
    store = HttpTokenStore(base_url="http://127.0.0.1:1", _token="root-token")
    try:
        assert store._auth_token == "root-token"
        assert stub.bearer_calls == []
    finally:
        store.close()


def test_401_with_a_data_token_invalidates_and_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invalidate-and-reresolve contract the other adopters carry: one
    401 means the cached data token went stale server-side; invalidate,
    re-resolve (a fresh mint), rebuild the client, retry ONCE."""
    import httpx

    stub = _stub_manager(monkeypatch, "data-tok-1")
    seen: list[tuple[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers["Authorization"]))
        if len(seen) == 1:
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(
            200, json={"session_token": "minted", "session_id": "s", "expires_in_seconds": 3600}
        )

    real_build = HttpTokenStore._build_client

    def _build_with_mock(self: HttpTokenStore) -> httpx.Client:
        client = real_build(self)
        transport = httpx.MockTransport(_handler)
        return httpx.Client(
            base_url=self._base_url, headers=client.headers, transport=transport
        )

    monkeypatch.setattr(HttpTokenStore, "_build_client", _build_with_mock)
    store = HttpTokenStore(
        base_url="http://127.0.0.1:1", _token="mint-locked-static", prefer_data_token=True
    )
    try:
        stub.token = "data-tok-2"  # what the re-mint hands back after invalidation
        out = store.start_session("s")
        assert out["session_token"] == "minted"
        assert stub.invalidations == [("http://127.0.0.1:1", store._tenant)]
        assert seen == [
            ("/v1/sessions/start", "Bearer data-tok-1"),
            ("/v1/sessions/start", "Bearer data-tok-2"),
        ]
    finally:
        store.close()


def test_401_with_the_static_token_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """No data token in play (unarmed): a 401 stays a loud HTTPStatusError,
    never a silent retry loop."""
    import httpx

    _stub_manager(monkeypatch, None)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    real_build = HttpTokenStore._build_client

    def _build_with_mock(self: HttpTokenStore) -> httpx.Client:
        client = real_build(self)
        return httpx.Client(
            base_url=self._base_url, headers=client.headers,
            transport=httpx.MockTransport(_handler),
        )

    monkeypatch.setattr(HttpTokenStore, "_build_client", _build_with_mock)
    store = HttpTokenStore(
        base_url="http://127.0.0.1:1", _token="static", prefer_data_token=True
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            store.start_session("s")
    finally:
        store.close()


# ── http_token_store: session start/close path construction ──────────────────

def test_start_and_close_session_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    store = HttpTokenStore(base_url="http://127.0.0.1:1", _token="bearer")

    def _fake_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((path, body))
        if path.endswith("/start"):
            return {"session_token": "minted", "session_id": body["session_id"],
                    "expires_in_seconds": 86400}
        return {"closed": 1}

    monkeypatch.setattr(store, "_post", _fake_post)

    started = store.start_session("sess-1", ttl_seconds=3600)
    assert started["session_token"] == "minted"
    closed = store.close_session("sess-1")
    assert closed["closed"] == 1
    store.close()

    assert calls == [
        ("/v1/sessions/start", {"session_id": "sess-1", "ttl_seconds": 3600}),
        ("/v1/sessions/close", {"session_id": "sess-1"}),
    ]
