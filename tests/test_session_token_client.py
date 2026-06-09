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
